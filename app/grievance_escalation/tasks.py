"""Celery tasks: submitting a drafted grievance to SCORES, and
Requirement 3's periodic status poll. Follows
`app.regulatory_filing.tasks`'s exact shape (one per-item task plus one
periodic sweep task that lists pending items and fans out `.delay()`
calls) -- same "one asyncio.run per task invocation" convention as
every other Celery task in this codebase touching async collaborators.
"""
from __future__ import annotations

import asyncio
import logging

from app.config import get_settings
from app.execution.celery_app import celery_app
from app.execution.dependencies import get_redis_pool
from app.grievance_escalation.dashboard import notify_grievance_filed, notify_grievance_status_changed
from app.grievance_escalation.queue import GrievanceQueue
from app.grievance_escalation.scores_client import ScoresApiClient, ScoresApiError
from app.resilience.retry_policy import is_transient

logger = logging.getLogger(__name__)


@celery_app.task(name="app.grievance_escalation.tasks.submit_grievance_task", bind=True)
def submit_grievance_task(self, grievance_id: str) -> None:
    del self
    asyncio.run(_submit_one(grievance_id))


async def _submit_one(grievance_id: str) -> None:
    settings = get_settings()
    redis_client = get_redis_pool()
    queue = GrievanceQueue(redis_client, settings.grievance_escalation_key_prefix)
    client = ScoresApiClient(settings)

    record = await queue.get(grievance_id)
    if record is None:
        logger.warning("submit_grievance_task: grievance %s no longer exists; skipping.", grievance_id)
        return

    record = await queue.mark_submitting(grievance_id)
    try:
        response = await client.submit_grievance(record.request)
    except Exception as exc:  # noqa: BLE001 - classified below via is_transient, exactly like app.regulatory_filing.tasks
        if is_transient(exc) and record.attempt_count < record.max_retries:
            await queue.mark_retry(grievance_id, str(exc))
            logger.warning("Transient failure submitting grievance %s (attempt %d/%d): %s", grievance_id, record.attempt_count, record.max_retries, exc)
        else:
            await queue.mark_submission_failed(grievance_id, str(exc))
            logger.error("Grievance %s submission failed permanently (attempt %d/%d): %s", grievance_id, record.attempt_count, record.max_retries, exc)
        return

    record = await queue.mark_submitted(grievance_id, response.scores_reference_number)
    logger.info("Grievance %s submitted to SCORES: reference=%s", grievance_id, response.scores_reference_number)
    try:
        await notify_grievance_filed(record, redis_client, settings)
    except Exception:  # noqa: BLE001 - a dashboard-notification failure must never mask a successful submission
        logger.exception("Failed to push grievance-filed dashboard notification for %s.", grievance_id)


@celery_app.task(name="app.grievance_escalation.tasks.submit_pending_grievances_task")
def submit_pending_grievances_task() -> int:
    return asyncio.run(_submit_pending())


async def _submit_pending() -> int:
    settings = get_settings()
    queue = GrievanceQueue(get_redis_pool(), settings.grievance_escalation_key_prefix)
    pending = await queue.list_pending_submission()
    for record in pending:
        submit_grievance_task.delay(record.grievance_id)
    return len(pending)


@celery_app.task(name="app.grievance_escalation.tasks.poll_grievance_status_task", bind=True, max_retries=3, default_retry_delay=60)
def poll_grievance_status_task(self, grievance_id: str) -> None:
    del self
    asyncio.run(poll_grievance_status_now(grievance_id))


async def poll_grievance_status_now(grievance_id: str) -> None:
    """Requirement 3: polls SCORES for this grievance's current status
    and, ONLY on a genuine change, updates the queue record and pushes
    a dashboard notification -- an unchanged status polled again is a
    quiet no-op, so the dashboard/incident history reflects actual
    state transitions, not every polling tick."""
    settings = get_settings()
    redis_client = get_redis_pool()
    queue = GrievanceQueue(redis_client, settings.grievance_escalation_key_prefix)
    client = ScoresApiClient(settings)

    record = await queue.get(grievance_id)
    if record is None or record.scores_reference_number is None:
        logger.warning("poll_grievance_status_task: grievance %s missing or has no SCORES reference; skipping.", grievance_id)
        return

    try:
        status_response = await client.get_grievance_status(record.scores_reference_number)
    except ScoresApiError:
        logger.exception("Failed to poll SCORES status for grievance %s (reference=%s); will retry on the next sweep.", grievance_id, record.scores_reference_number)
        return

    if status_response.status == record.scores_status and status_response.resolution_summary == record.resolution_summary:
        return  # no change -- quiet no-op, see this function's docstring

    updated = await queue.update_status(grievance_id, status_response.status, status_response.resolution_summary)
    logger.info("Grievance %s status changed: %s -> %s", grievance_id, record.scores_status, updated.scores_status)
    try:
        await notify_grievance_status_changed(updated, redis_client, settings)
    except Exception:  # noqa: BLE001
        logger.exception("Failed to push grievance-status-changed dashboard notification for %s.", grievance_id)


@celery_app.task(name="app.grievance_escalation.tasks.poll_pending_grievances_task")
def poll_pending_grievances_task() -> int:
    return asyncio.run(_poll_pending())


async def _poll_pending() -> int:
    """The periodic sweep (Requirement 3) -- lists every OPEN (submitted,
    not yet resolved/rejected) grievance and fans out one poll task per
    grievance, mirroring app.incident.tasks.sweep_overdue_escalations_task's
    "periodic re-derive, don't trust push-only delivery" shape."""
    settings = get_settings()
    queue = GrievanceQueue(get_redis_pool(), settings.grievance_escalation_key_prefix)
    open_grievances = await queue.list_open()
    for record in open_grievances:
        poll_grievance_status_task.delay(record.grievance_id)
    return len(open_grievances)
