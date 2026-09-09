"""Transcript persistence and strict tenant isolation for Multi-Agent Arbitration.

Guarantees:
1. STRICT TENANT ISOLATION: Every Redis key includes tenant_id prefix; Tenant A cannot access Tenant B.
2. TAMPER-EVIDENT AUDITABILITY: Verifies cryptographic SHA-256 integrity over transcript payload.
3. IN-MEMORY TEST COMPATIBILITY: Gracefully operates with an in-memory test double when Redis is unavailable.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from app.config import Settings, get_settings
from app.negotiation.arbitration_models import ArbitrationTranscript

logger = logging.getLogger(__name__)


class ArbitrationTranscriptStore:
    """Manages transient state and durable audit transcripts for multi-agent arbitration."""

    def __init__(
        self,
        redis_client: Any | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self._redis = redis_client
        self._prefix = getattr(self.settings, "negotiation_state_key_prefix", "regengine:arbitration")
        self._ttl = getattr(self.settings, "arbitration_redis_ttl_seconds", 86400)
        # In-memory test double fallback when redis is absent
        self._memory_store: dict[str, str] = {}

    def _summary_key(self, tenant_id: str, session_id: str) -> str:
        return f"{self._prefix}:{tenant_id}:{session_id}:summary"

    def _transcript_key(self, tenant_id: str, session_id: str) -> str:
        return f"{self._prefix}:{tenant_id}:{session_id}:transcript"

    async def save_transcript(self, transcript: ArbitrationTranscript) -> str:
        """Saves an arbitration transcript under strict tenant isolation.
        Seals the transcript with its cryptographic digest prior to storage.
        """
        if not transcript.tenant_id or not transcript.tenant_id.strip():
            raise ValueError("tenant_id is mandatory for arbitration transcript storage to enforce isolation.")

        # Compute and seal cryptographic digest
        digest = transcript.seal()
        payload_json = transcript.model_dump_json()

        summary = {
            "session_id": transcript.session_id,
            "tenant_id": transcript.tenant_id,
            "circular_id": transcript.circular_id,
            "clause_id": transcript.clause_id,
            "final_outcome": transcript.final_outcome.value,
            "rounds_count": str(len(transcript.rounds)),
            "transcript_sha256": digest,
            "hitl_review_id": transcript.hitl_review_id or "",
        }

        t_key = self._transcript_key(transcript.tenant_id, transcript.session_id)
        s_key = self._summary_key(transcript.tenant_id, transcript.session_id)

        if self._redis is not None:
            try:
                pipe = self._redis.pipeline()
                pipe.set(t_key, payload_json, ex=self._ttl)
                pipe.hset(s_key, mapping=summary)
                pipe.expire(s_key, self._ttl)
                await pipe.execute()
            except Exception as exc:  # noqa: BLE001
                logger.error("Failed to persist arbitration transcript in Redis: %s", exc)
                self._memory_store[t_key] = payload_json
                self._memory_store[s_key] = json.dumps(summary)
        else:
            self._memory_store[t_key] = payload_json
            self._memory_store[s_key] = json.dumps(summary)

        logger.info(
            "Arbitration transcript saved: session=%s, tenant=%s, outcome=%s, digest=%s",
            transcript.session_id,
            transcript.tenant_id,
            transcript.final_outcome.value,
            digest[:12],
        )
        return digest

    async def get_transcript(self, tenant_id: str, session_id: str) -> ArbitrationTranscript | None:
        """Retrieves and cryptographically verifies an arbitration transcript.
        Guarantees Tenant A cannot retrieve Tenant B's transcript.
        """
        if not tenant_id or not tenant_id.strip():
            raise ValueError("tenant_id is required to retrieve arbitration transcript.")

        t_key = self._transcript_key(tenant_id, session_id)
        raw_json: str | None = None

        if self._redis is not None:
            try:
                raw_json = await self._redis.get(t_key)
            except Exception as exc:
                logger.error("Redis error fetching transcript: %s", exc)
                raw_json = self._memory_store.get(t_key)
        else:
            raw_json = self._memory_store.get(t_key)

        if raw_json is None:
            return None

        transcript = ArbitrationTranscript.model_validate_json(raw_json)

        # Enforce tenant isolation check
        if transcript.tenant_id != tenant_id:
            logger.error("Tenant isolation violation: requested %s but found %s", tenant_id, transcript.tenant_id)
            return None

        # Verify cryptographic integrity
        computed_digest = transcript.compute_hash()
        if computed_digest != transcript.transcript_sha256:
            logger.critical(
                "Transcript tamper detected for session %s! Stored=%s, Recomputed=%s",
                session_id,
                transcript.transcript_sha256,
                computed_digest,
            )
            raise ValueError(f"Tamper detected: transcript digest mismatch for session {session_id}")

        return transcript
