"""Shared helper for routing a failure to the DLQ from inside a Celery
task body. Celery tasks in this codebase run outside an asyncio event
loop (see app.ingestion.tasks's module docstring), so every task that
needs to touch async app code -- including this one -- opens exactly one
event loop per call via `asyncio.run`. Factored out here once rather than
reimplemented per task module (app.compiler.tasks, app.agents.tasks,
app.vectorstore.tasks, app.ingestion.tasks all use this same function) so
the "how do we get a DeadLetterQueue instance from inside a sync Celery
task" plumbing can't drift between them.
"""
from __future__ import annotations

import asyncio

import redis.asyncio as aioredis

from app.config import get_settings
from app.resilience.dead_letter_queue import DeadLetterQueue
from app.resilience.models import DLQEntry, FailureCategory


def route_to_dlq_sync(
    *,
    category: FailureCategory,
    task_name: str,
    payload: dict,
    exc: BaseException,
    original_task_id: str | None = None,
    attempt_count: int = 1,
) -> DLQEntry:
    """Synchronous entrypoint for a Celery task's exception handler.
    Opens its own short-lived Redis connection rather than sharing a
    process-wide pool -- this runs on the failure path, potentially after
    other connections in the process are already in a bad state, and a
    dedicated connection keeps DLQ delivery independent of whatever just
    went wrong."""

    async def _send() -> DLQEntry:
        settings = get_settings()
        redis_client = aioredis.from_url(settings.redis_url, decode_responses=True)
        dlq = DeadLetterQueue(redis_client, settings.dlq_key_prefix)
        try:
            return await dlq.send(
                category=category,
                task_name=task_name,
                payload=payload,
                exc=exc,
                original_task_id=original_task_id,
                attempt_count=attempt_count,
            )
        finally:
            await redis_client.aclose()

    return asyncio.run(_send())
