"""Requirement 3's scheduled worker: drains `FilingQueue`'s pending set
on an interval (beat-scheduled, see app.execution.celery_app), submits
each via its configured channel, and on final retry exhaustion raises a
CRITICAL breach event (app.incident) -- reusing this codebase's existing
retry-transience classifier and breach-notification pipeline rather
than building parallel ones.
"""
from __future__ import annotations

import asyncio
import logging

import redis.asyncio as aioredis

from app.config import get_settings
from app.execution.celery_app import celery_app
from app.incident.publisher import raise_breach_event
from app.incident.trigger_matrix import filing_submission_failed_event
from app.regulatory_filing.submission import FilingQueue, FilingStatus, SubmissionError, get_submitter
from app.resilience.retry_policy import is_transient

logger = logging.getLogger(__name__)


async def _submit_one(filing_id: str) -> None:
    settings = get_settings()
    redis_client = aioredis.from_url(settings.redis_url, decode_responses=True)
    try:
        queue = FilingQueue(redis_client, settings.regulatory_filing_key_prefix)
        record = await queue.get(filing_id)
        if record is None:
            logger.warning("submit_filing_task: filing %s not found in the queue (already resolved?).", filing_id)
            return
        if record.status == FilingStatus.ACKNOWLEDGED:
            return  # already done -- a duplicate sweep/dispatch is a no-op, not an error

        record = await queue.mark_submitting(filing_id)
        submitter = get_submitter(record, settings)

        try:
            ack = await submitter.submit(record)
        except Exception as exc:  # noqa: BLE001 - classified below via is_transient, exactly like every other Celery task's exception handling in this codebase
            error_text = f"{type(exc).__name__}: {exc}"
            if record.attempt_count >= record.max_retries or not is_transient(exc):
                await queue.mark_failed(filing_id, error_text)
                logger.error(
                    "Filing %s exhausted its retry budget (attempt %d/%d, transient=%s): %s",
                    filing_id, record.attempt_count, record.max_retries, is_transient(exc), error_text,
                )
                event = filing_submission_failed_event(
                    filing_id=filing_id,
                    filing_type=record.filing_type.value,
                    destination=f"{record.channel.value}:{record.target.value}",
                    attempt_count=record.attempt_count,
                    last_error=error_text,
                )
                await raise_breach_event(event, redis_client, settings)
            else:
                await queue.mark_retry(filing_id, error_text)
                logger.warning("Filing %s submission attempt %d/%d failed (will retry): %s", filing_id, record.attempt_count, record.max_retries, error_text)
            return

        await queue.mark_acknowledged(filing_id, ack)
        logger.info("Filing %s acknowledged by %s: reference=%s", filing_id, record.target.value, ack.acknowledgment_reference)
    finally:
        await redis_client.aclose()


@celery_app.task(name="app.regulatory_filing.tasks.submit_filing_task", bind=True)
def submit_filing_task(self, filing_id: str) -> None:
    """One filing, one attempt. Failure classification and retry
    bookkeeping happen inside `_submit_one` against `FilingQueue`
    itself (not Celery's own `self.retry`) so a filing's attempt
    history/status survives a Celery result-backend TTL and is directly
    inspectable via the queue -- the same reasoning
    app.resilience.dead_letter_queue's module docstring gives for why
    that queue is the durable record, not Celery's own task result."""
    if not get_settings().regulatory_filing_enabled:
        logger.info("regulatory_filing_enabled is False; skipping submit_filing_task for filing_id=%s.", filing_id)
        return
    asyncio.run(_submit_one(filing_id))


async def _sweep_pending() -> list[str]:
    settings = get_settings()
    redis_client = aioredis.from_url(settings.redis_url, decode_responses=True)
    try:
        queue = FilingQueue(redis_client, settings.regulatory_filing_key_prefix)
        pending = await queue.list_pending()
        # SUBMITTING filings are excluded -- a worker already has them
        # in flight; re-dispatching here would double-submit a filing
        # whose previous attempt simply hasn't finished (or crashed)
        # yet rather than genuinely failed, which _submit_one's own
        # exception handling is what's supposed to detect.
        return [r.filing_id for r in pending if r.status == FilingStatus.PENDING]
    finally:
        await redis_client.aclose()


@celery_app.task(name="app.regulatory_filing.tasks.submit_pending_filings_task")
def submit_pending_filings_task() -> int:
    """Beat-scheduled sweep (app.execution.celery_app's
    `regulatory_filing_submit_interval_seconds`) -- the durable trigger
    for both a brand-new filing and a previously-failed-but-not-yet-
    exhausted retry, mirroring app.incident.tasks.sweep_overdue_escalations_task's
    "pub/sub dispatch is the fast path, a periodic poll is the safety
    net that bounds the worst case" pattern: a filing enqueued while a
    worker was down still gets submitted on the next sweep, not lost."""
    if not get_settings().regulatory_filing_enabled:
        return 0
    filing_ids = asyncio.run(_sweep_pending())
    for filing_id in filing_ids:
        submit_filing_task.delay(filing_id)
    return len(filing_ids)
