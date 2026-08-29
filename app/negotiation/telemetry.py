"""Requirement 3 -- Telemetry & Reasoning Log: durably records the
entire multi-agent dialogue for audit -- every round's agent verdicts,
the weighted vote tally, and (when the arbiter fires) the full conflict
reasoning tree -- so a SEBI examiner or internal compliance officer can
reconstruct exactly why a transaction was allowed, denied, or escalated
without re-running the negotiation.

Mirrors app.agents.graph.state_store.GraphExecutionStateStore's shape
(a hash-per-run plus an append-only list of per-step JSON records) --
the established convention in this codebase for durable, cross-process
negotiation/execution state, rather than inventing a third pattern.

Two Redis keys per negotiation:
  <prefix>:negotiation:<negotiation_id>   Hash of top-level summary (status,
                                           final_decision, round_count,
                                           updated_at) -- cheap status check.
  <prefix>:rounds:<negotiation_id>        Ordered list of one JSON record
                                           per negotiation round (full
                                           agent verdicts + tally), plus a
                                           final record for the arbiter's
                                           resolution/reasoning tree when
                                           it fires -- the complete transcript.
"""
from __future__ import annotations

import datetime as dt
import json
import logging

import redis.asyncio as redis

from app.negotiation.models import NegotiationResult, NegotiationRound

logger = logging.getLogger(__name__)


class NegotiationTranscriptStore:
    def __init__(self, redis_client: redis.Redis, key_prefix: str, ttl_seconds: int) -> None:
        self._redis = redis_client
        self._prefix = key_prefix
        self._ttl = ttl_seconds

    def _summary_key(self, negotiation_id: str) -> str:
        return f"{self._prefix}:negotiation:{negotiation_id}"

    def _rounds_key(self, negotiation_id: str) -> str:
        return f"{self._prefix}:rounds:{negotiation_id}"

    async def record_round(self, negotiation_id: str, round_: NegotiationRound) -> None:
        """Called at the end of EVERY negotiation round -- the single
        choke point every round passes through (app.negotiation.consensus.ConsensusEngine.run
        calls this via the orchestrator after each round completes), so
        a round can never execute without its full verdict dialogue
        being durably recorded, matching GraphExecutionStateStore's
        "one instrumentation point, not scattered call sites" convention."""
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        try:
            pipe = self._redis.pipeline()
            pipe.rpush(self._rounds_key(negotiation_id), round_.model_dump_json())
            pipe.expire(self._rounds_key(negotiation_id), self._ttl)
            pipe.hset(self._summary_key(negotiation_id), mapping={
                "last_round": str(round_.round_number),
                "last_leading_decision": round_.tally.leading_decision.value,
                "last_leading_share": str(round_.tally.leading_share),
                "consensus_reached": str(round_.consensus_reached),
                "updated_at": now,
            })
            pipe.expire(self._summary_key(negotiation_id), self._ttl)
            await pipe.execute()
        except Exception:  # noqa: BLE001 - a telemetry failure must never abort the negotiation itself
            logger.exception("Failed to record negotiation round: negotiation_id=%s round=%s", negotiation_id, round_.round_number)

    async def record_outcome(self, result: NegotiationResult) -> None:
        """Called once, when the negotiation concludes (consensus,
        arbitration, or HITL escalation) -- appends a final transcript
        record distinct from the per-round records so a reader can tell
        "how we got here" (rounds) apart from "where we ended up"
        (outcome) without re-deriving one from the other."""
        try:
            pipe = self._redis.pipeline()
            pipe.rpush(self._rounds_key(result.negotiation_id), json.dumps({
                "record_type": "outcome",
                "status": result.status.value,
                "final_decision": result.final_decision.value,
                "arbiter_resolution": result.arbiter_resolution.model_dump(mode="json") if result.arbiter_resolution else None,
                "reasoning_tree": result.reasoning_tree.model_dump(mode="json") if result.reasoning_tree else None,
                "hitl_case_id": result.hitl_case_id,
                "completed_at": result.completed_at.isoformat() if result.completed_at else None,
            }))
            pipe.expire(self._rounds_key(result.negotiation_id), self._ttl)
            pipe.hset(self._summary_key(result.negotiation_id), mapping={
                "transaction_id": result.transaction_id,
                "status": result.status.value,
                "final_decision": result.final_decision.value,
                "round_count": str(len(result.rounds)),
                "hitl_case_id": result.hitl_case_id or "",
                "completed_at": result.completed_at.isoformat() if result.completed_at else "",
            })
            pipe.expire(self._summary_key(result.negotiation_id), self._ttl)
            await pipe.execute()
        except Exception:  # noqa: BLE001
            logger.exception("Failed to record negotiation outcome: negotiation_id=%s", result.negotiation_id)

    async def get_summary(self, negotiation_id: str) -> dict[str, str] | None:
        raw = await self._redis.hgetall(self._summary_key(negotiation_id))
        return raw or None

    async def get_transcript(self, negotiation_id: str) -> list[dict]:
        raw_records = await self._redis.lrange(self._rounds_key(negotiation_id), 0, -1)
        return [json.loads(r) for r in raw_records]
