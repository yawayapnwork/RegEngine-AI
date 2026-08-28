"""Change detection: tells a brand-new circular apart from one already
ingested, and a genuinely amended master circular (same URL, different PDF
bytes) apart from a no-op re-poll.

State lives in Redis (same instance as the policy registry / HITL queue) so
detection survives worker restarts and is shared across however many
ingestion workers are polling concurrently.
"""
from __future__ import annotations

import hashlib

import redis.asyncio as aioredis

from app.ingestion.models import ChangeKind, DiscoveredDocument


class SeenDocumentStore:
    """Redis hash of `source_url -> content_sha256` for every circular this
    service has already pushed through the Phase 1 pipeline."""

    def __init__(self, redis_client: aioredis.Redis, key_prefix: str) -> None:
        self._redis = redis_client
        self._hash_key = f"{key_prefix}:seen_documents"

    @staticmethod
    def hash_content(content: bytes) -> str:
        return hashlib.sha256(content).hexdigest()

    async def classify(self, discovered: DiscoveredDocument, content: bytes) -> tuple[ChangeKind, str]:
        """Compares freshly downloaded PDF bytes against the last-seen hash
        for this URL and returns the change classification plus the new hash.
        Does NOT record the result — call `record` after the document has
        been successfully handed to the Phase 1 pipeline, so a crash
        mid-processing causes a harmless re-poll rather than a silently
        dropped update.
        """
        new_hash = self.hash_content(content)
        previous_hash = await self._redis.hget(self._hash_key, discovered.source_url)

        if previous_hash is None:
            return ChangeKind.NEW_DOCUMENT, new_hash
        if previous_hash != new_hash:
            return ChangeKind.CONTENT_AMENDED, new_hash
        return ChangeKind.UNCHANGED, new_hash

    async def record(self, source_url: str, content_sha256: str) -> None:
        await self._redis.hset(self._hash_key, source_url, content_sha256)

    async def known_urls(self) -> set[str]:
        keys = await self._redis.hkeys(self._hash_key)
        return set(keys)
