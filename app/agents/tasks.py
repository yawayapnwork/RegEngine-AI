"""Celery wrapper around app.agents.pipeline.extract_and_audit_clause,
giving the LLM extraction/audit pass background-job treatment: bounded
retry-with-backoff-and-jitter for a transient Hugging Face Inference API failure (rate
limit, timeout, connection drop), and immediate DLQ routing (never
retried) for anything else -- a clause the model genuinely cannot
structure will fail exactly the same way on the fifth attempt as the
first, and burning four more LLM calls just delays a human finding out.

The existing async `extract_and_audit_clause` function remains the
interactive/synchronous call path (used directly by
extract_and_audit_circular's bounded-concurrency gather); this task is
for background/batch extraction workflows where per-clause failures
should degrade to "one entry in the DLQ", not take down the whole batch.
"""
from __future__ import annotations

import asyncio
import logging

from app.agents.pipeline import extract_and_audit_clause
from app.config import get_settings
from app.execution.celery_app import celery_app
from app.models import ClauseChunk
from app.resilience.celery_helpers import route_to_dlq_sync
from app.resilience.models import FailureCategory
from app.resilience.retry_policy import is_transient

logger = logging.getLogger(__name__)


@celery_app.task(
    name="app.agents.tasks.extract_and_audit_clause_task",
    bind=True,
    max_retries=None,  # enforced manually below via settings.retry_max_attempts_pipeline; see the retry() call
    retry_backoff=True,
    retry_backoff_max=300,
    retry_jitter=True,
)
def extract_and_audit_clause_task(self, chunk_dict: dict, sibling_chunks: list[dict] | None = None) -> dict:
    """`chunk_dict` is `ClauseChunk.model_dump(mode="json")`. Returns
    `AuditedComplianceRule.model_dump(mode="json")` on success."""
    settings = get_settings()
    chunk = ClauseChunk.model_validate(chunk_dict)

    try:
        audited = asyncio.run(extract_and_audit_clause(chunk, sibling_chunks, settings))
    except Exception as exc:  # noqa: BLE001 - classify every failure explicitly below; nothing falls through un-routed
        attempt = self.request.retries + 1
        if is_transient(exc) and attempt < settings.retry_max_attempts_pipeline:
            logger.warning(
                "Transient failure extracting clause '%s' (attempt %d/%d): %s -- retrying with backoff.",
                chunk.chunk_id, attempt, settings.retry_max_attempts_pipeline, exc,
            )
            raise self.retry(exc=exc) from exc

        logger.error(
            "LLM extraction failed for clause '%s' after %d attempt(s) (transient=%s): %s",
            chunk.chunk_id, attempt, is_transient(exc), exc,
        )
        route_to_dlq_sync(
            category=FailureCategory.LLM_EXTRACTION,
            task_name="app.agents.tasks.extract_and_audit_clause_task",
            payload={"chunk_dict": chunk_dict, "sibling_chunks": sibling_chunks},
            exc=exc,
            original_task_id=self.request.id,
            attempt_count=attempt,
        )
        raise

    return audited.model_dump(mode="json")
