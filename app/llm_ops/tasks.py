"""Celery task: periodic sweep of expired semantic-cache Qdrant points.

Registered on `regengine_llm_ops` queue and scheduled hourly via Celery
beat (see app.execution.celery_app) -- Qdrant has no native per-point TTL
(unlike the Redis exact-cache layer, which expires via SETEX on its own),
so this is the only thing that ever removes a stale semantic-cache entry.
"""
from __future__ import annotations

import asyncio
import logging

from app.execution.celery_app import celery_app
from app.config import get_settings
from app.llm_ops.semantic_cache import SemanticPromptCache

logger = logging.getLogger(__name__)


@celery_app.task(name="app.llm_ops.tasks.purge_expired_cache_entries_task")
def purge_expired_cache_entries_task() -> None:
    settings = get_settings()
    cache = SemanticPromptCache(settings)
    try:
        asyncio.run(cache.purge_expired())
    finally:
        asyncio.run(cache.close())
