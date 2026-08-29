"""Two-layer semantic prompt cache for LLM-backed compliance tasks.

Layer 1 (EXACT, Redis): SHA-256 of the normalized input text -> cached
JSON response. O(1) lookup, handles the extremely common case of a
byte-identical clause reappearing (e.g. a circular re-uploaded, or the
same boilerplate clause repeated across many circulars).

Layer 2 (SEMANTIC, Qdrant): dense-embedding cosine similarity search
against previously-cached (text, response) pairs, for near-duplicate text
that differs only in whitespace/formatting/minor wording but means the
same thing. Only consulted on an exact-cache miss, since it costs an
embedding call + a vector search versus Layer 1's single Redis GET.

The similarity threshold (`settings.llm_cache_similarity_threshold`,
default 0.97) is set deliberately high: this cache sits in front of
*legal* text, where two clauses that are 90% similar can still differ in
the one number or "shall"/"may" that changes the obligation entirely.
Optimizing for cache-hit-rate at the expense of that precision would
directly risk producing a wrong compliance determination -- correctness
dominates cost savings here, not the other way around.

A semantic hit is also backfilled into the Redis exact-cache under the
querying text's own hash, so a literal repeat of that near-duplicate text
resolves via Layer 1 next time without a second vector search.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time
import uuid
from typing import Any

from app.config import Settings
from app.llm_ops.models import CacheLayer, CacheLookupResult

logger = logging.getLogger(__name__)

_WHITESPACE = re.compile(r"\s+")
_CACHE_POINT_NAMESPACE = uuid.UUID("9c2b6e8a-4f2a-4d0f-9a1a-7b7e6c9a2d1f")


def normalize_text(text: str) -> str:
    """Collapses whitespace only -- deliberately NOT lowercased or
    punctuation-stripped, since legal text's capitalization and
    punctuation (e.g. "Shall" vs "shall", "20%" vs "20 %") can be
    substantive, not noise."""
    return _WHITESPACE.sub(" ", text).strip()


def _exact_cache_key(prefix: str, task_type: str, text: str) -> str:
    digest = hashlib.sha256(normalize_text(text).encode("utf-8")).hexdigest()
    return f"{prefix}:exact:{task_type}:{digest}"


def _point_id_for(task_type: str, text: str) -> str:
    digest = hashlib.sha256(f"{task_type}:{normalize_text(text)}".encode("utf-8")).hexdigest()
    return str(uuid.uuid5(_CACHE_POINT_NAMESPACE, digest))


class SemanticPromptCache:
    """Stateless wrapper around Redis + Qdrant clients (both lazily
    constructed and reused across calls). One instance is safe to share
    across requests within a process."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._redis = None
        self._qdrant = None

    async def _get_redis(self):
        if self._redis is None:
            import redis.asyncio as redis

            self._redis = redis.from_url(self._settings.redis_url, decode_responses=True)
        return self._redis

    async def _get_qdrant(self):
        if self._qdrant is None:
            from qdrant_client import AsyncQdrantClient

            self._qdrant = AsyncQdrantClient(
                url=self._settings.qdrant_url,
                api_key=self._settings.qdrant_api_key,
                timeout=self._settings.qdrant_timeout_seconds,
            )
            await self._ensure_collection(self._qdrant)
        return self._qdrant

    async def _ensure_collection(self, client) -> None:
        from qdrant_client import models

        collection = self._settings.llm_cache_qdrant_collection
        if not await client.collection_exists(collection):
            await client.create_collection(
                collection_name=collection,
                vectors_config=models.VectorParams(size=self._settings.embedding_dim, distance=models.Distance.COSINE),
            )
            await client.create_payload_index(
                collection_name=collection, field_name="task_type", field_schema=models.PayloadSchemaType.KEYWORD
            )
            await client.create_payload_index(
                collection_name=collection, field_name="expires_at", field_schema=models.PayloadSchemaType.FLOAT
            )

    async def get(self, text: str, task_type: str) -> CacheLookupResult:
        """Checks EXACT then SEMANTIC layers, in that order. Returns
        `hit=False` on a full miss -- the caller is then responsible for
        invoking the LLM and calling `put()` with the result."""
        redis_client = await self._get_redis()
        exact_key = _exact_cache_key(self._settings.llm_cache_redis_key_prefix, task_type, text)

        raw = await redis_client.get(exact_key)
        if raw is not None:
            return CacheLookupResult(hit=True, layer=CacheLayer.EXACT, similarity=1.0, cached_response=json.loads(raw), cache_key=exact_key)

        semantic_hit = await self._semantic_lookup(text, task_type)
        if semantic_hit is not None:
            # Backfill Layer 1 so a literal repeat of this exact text next
            # time resolves without a vector search.
            await redis_client.setex(exact_key, self._settings.llm_cache_ttl_seconds, json.dumps(semantic_hit.cached_response))
            return semantic_hit

        return CacheLookupResult(hit=False, cache_key=exact_key)

    async def _semantic_lookup(self, text: str, task_type: str) -> CacheLookupResult | None:
        from qdrant_client import models

        from app.vectorstore.embeddings import embed_texts

        [vector] = await embed_texts([text], self._settings)
        client = await self._get_qdrant()
        now = time.time()

        results = await client.query_points(
            collection_name=self._settings.llm_cache_qdrant_collection,
            query=vector,
            limit=1,
            query_filter=models.Filter(
                must=[
                    models.FieldCondition(key="task_type", match=models.MatchValue(value=task_type)),
                    models.FieldCondition(key="expires_at", range=models.Range(gt=now)),
                ]
            ),
        )
        points = results.points
        if not points:
            return None

        top = points[0]
        if top.score < self._settings.llm_cache_similarity_threshold:
            return None

        logger.info("Semantic cache HIT (task_type=%s, similarity=%.4f)", task_type, top.score)
        return CacheLookupResult(hit=True, layer=CacheLayer.SEMANTIC, similarity=top.score, cached_response=top.payload["response"])

    async def put(self, text: str, task_type: str, response: dict[str, Any]) -> None:
        """Stores the LLM's (validated) response into both cache layers."""
        redis_client = await self._get_redis()
        exact_key = _exact_cache_key(self._settings.llm_cache_redis_key_prefix, task_type, text)
        await redis_client.setex(exact_key, self._settings.llm_cache_ttl_seconds, json.dumps(response))

        from qdrant_client import models

        from app.vectorstore.embeddings import embed_texts

        [vector] = await embed_texts([text], self._settings)
        client = await self._get_qdrant()
        await client.upsert(
            collection_name=self._settings.llm_cache_qdrant_collection,
            points=[
                models.PointStruct(
                    id=_point_id_for(task_type, text),
                    vector=vector,
                    payload={
                        "task_type": task_type,
                        "response": response,
                        "cached_at": time.time(),
                        "expires_at": time.time() + self._settings.llm_cache_ttl_seconds,
                    },
                )
            ],
            wait=False,  # cache writes are best-effort; never block the request on cache durability
        )

    async def purge_expired(self) -> int:
        """Deletes Qdrant points past their TTL. Qdrant has no native
        per-point TTL (unlike Redis's SETEX), so this must be run
        periodically -- see `app.llm_ops.tasks.purge_expired_cache_entries_task`,
        scheduled hourly via Celery beat. Redis entries expire on their
        own via SETEX and need no equivalent sweep."""
        from qdrant_client import models

        client = await self._get_qdrant()
        result = await client.delete(
            collection_name=self._settings.llm_cache_qdrant_collection,
            points_selector=models.FilterSelector(
                filter=models.Filter(
                    must=[models.FieldCondition(key="expires_at", range=models.Range(lt=time.time()))]
                )
            ),
        )
        logger.info("Purged expired semantic cache entries: %s", result.status)
        return 1

    async def close(self) -> None:
        if self._redis is not None:
            await self._redis.aclose()
        if self._qdrant is not None:
            await self._qdrant.close()
