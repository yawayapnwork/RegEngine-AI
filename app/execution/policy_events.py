"""Pub/Sub event contract for policy lifecycle changes -- approved by a
compliance officer, auto-recompiled/amended, or revoked -- and the
publisher used to broadcast them on Redis channel `regengine:policy_events`.

Redis Pub/Sub, deliberately, not Streams or the audit ledger: this is a
"wake up and reconcile" signal for in-memory state (OPA's loaded policies,
every process's `PolicyCache`), not a durable record anything replays.
The tamper-evident audit ledger (app.ledger) already IS the durable,
replayable record of compliance-relevant history, for an entirely
different purpose (proving what decision was made against what policy,
not keeping caches warm). Conflating the two would make the ledger's hash
chain carry cache-invalidation noise it was never designed to bear.

Pub/Sub's at-most-once delivery is consequently an accepted, deliberate
trade-off here, not an oversight -- `PolicyCache`'s TTL safety net (see
that module's docstring) bounds the blast radius of a dropped message to
a few seconds of staleness, which is an acceptable cost for the
simplicity this buys.
"""
from __future__ import annotations

import datetime as dt
from enum import Enum

import redis.asyncio as redis
from pydantic import BaseModel, Field

POLICY_EVENTS_CHANNEL = "regengine:policy_events"


class PolicyEventType(str, Enum):
    APPROVED = "approved"  # HITL-approved: newly active, must be published to OPA
    AMENDED = "amended"  # re-versioned (new rule_version) without going through HITL -- a clean auto-recompile
    REVOKED = "revoked"  # deactivated/withdrawn -- remove from OPA and every registry/cache


class PolicyEvent(BaseModel):
    """The wire format published on POLICY_EVENTS_CHANNEL. Self-contained
    (carries the Rego source itself, not just an id to go look one up) so
    a subscriber can hot-reload OPA without a round-trip back to Postgres
    on the critical path of "officer clicks approve" -> "OPA has it"."""

    event_type: PolicyEventType
    rule_id: str
    rule_version: int
    package: str
    entity_types: list[str] = Field(
        default_factory=lambda: ["*"],
        description='Which TransactionPayload.entity_type values this policy applies to; "*" = every transaction.',
    )
    rego_code: str | None = Field(None, description="Required for APPROVED/AMENDED; omitted (irrelevant) for REVOKED.")
    compiled_rule_id: int = Field(..., description="app.db.models.CompiledRule.id this event concerns.")
    approved_by: str | None = Field(None, description="Compliance officer subject, set only for APPROVED.")
    emitted_at: dt.datetime = Field(default_factory=lambda: dt.datetime.now(dt.timezone.utc))


class PolicyEventPublisher:
    def __init__(self, redis_client: redis.Redis) -> None:
        self._redis = redis_client

    async def publish(self, event: PolicyEvent) -> int:
        """Returns Redis PUBLISH's own return value: the number of
        subscribers that received the message at the instant it was
        published. Zero is not treated as an error here (e.g. mid-deploy,
        with no subscriber momentarily connected) -- see this module's
        docstring on why at-most-once delivery is an accepted trade-off;
        it is returned purely for caller-side logging/observability."""
        return await self._redis.publish(POLICY_EVENTS_CHANNEL, event.model_dump_json())
