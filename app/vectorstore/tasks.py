"""Celery wrapper around app.vectorstore.qdrant_store.index_chunks, giving
vector DB ingestion the exponential-backoff-with-jitter retry policy
requirement 2 asks for: a Qdrant connection blip or a transient timeout
is retried several times before anything reaches the DLQ; a permanent
failure (embedding backend rejecting a chunk's text, a schema mismatch on
the collection) is not retried at all.

Distinct from app.api.routes' synchronous `POST /v1/circulars/index` --
that path is for "index this now, tell the caller the result immediately"
(a human waiting on a PDF upload); this task is for background/batch
indexing (e.g. the SEBI ingestion pipeline re-indexing an amended
circular) where a transient Qdrant outage should be absorbed with retries
rather than surfaced as a failed HTTP request.
"""
from __future__ import annotations

import asyncio
import logging

from app.config import get_settings
from app.execution.celery_app import celery_app
from app.models import ClauseChunk
from app.resilience.celery_helpers import route_to_dlq_sync
from app.resilience.models import FailureCategory
from app.resilience.retry_policy import is_transient
from app.vectorstore.qdrant_store import index_chunks

logger = logging.getLogger(__name__)


@celery_app.task(
    name="app.vectorstore.tasks.index_chunks_task",
    bind=True,
    max_retries=None,  # enforced manually below via settings.retry_max_attempts_network; see the retry() call
    retry_backoff=True,
    retry_backoff_max=300,
    retry_jitter=True,
)
def index_chunks_task(self, chunk_dicts: list[dict], *, recreate_collection: bool = False) -> dict:
    """`chunk_dicts` is `[ClauseChunk.model_dump(mode="json"), ...]`.
    Returns `IndexResponse.model_dump(mode="json")` on success."""
    settings = get_settings()
    chunks = [ClauseChunk.model_validate(c) for c in chunk_dicts]

    try:
        response = asyncio.run(index_chunks(chunks, settings, recreate_collection=recreate_collection))
    except Exception as exc:  # noqa: BLE001 - classify every failure explicitly below (transient vs. DLQ)
        attempt = self.request.retries + 1
        # Network-facing work (this is the requirement-2 "vector DB
        # ingestion" retry case) gets the more generous network attempt
        # budget, not the tighter pipeline one -- a Qdrant outage that
        # outlasts three quick retries is still very plausibly transient
        # at attempt four or five.
        if is_transient(exc) and attempt < settings.retry_max_attempts_network:
            logger.warning(
                "Transient failure indexing %d chunk(s) (attempt %d/%d): %s -- retrying with backoff.",
                len(chunks), attempt, settings.retry_max_attempts_network, exc,
            )
            raise self.retry(exc=exc) from exc

        logger.error("Vector DB ingestion failed for %d chunk(s) after %d attempt(s): %s", len(chunks), attempt, exc)
        route_to_dlq_sync(
            category=FailureCategory.VECTOR_INGESTION,
            task_name="app.vectorstore.tasks.index_chunks_task",
            payload={"chunk_dicts": chunk_dicts, "recreate_collection": recreate_collection},
            exc=exc,
            original_task_id=self.request.id,
            attempt_count=attempt,
        )
        raise

    return response.model_dump(mode="json")
