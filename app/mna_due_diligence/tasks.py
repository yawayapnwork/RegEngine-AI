"""Background job manager, progress tracking, timeouts, and cancellation for M&A Due-Diligence.

Provides async execution with Redis/memory persistence, incremental progress reporting,
strict timeout enforcement, and cooperative cancellation.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import uuid
from typing import Any, Callable

import redis as sync_redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.config import Settings, get_settings
from app.db.models import MNAComparisonJob
from app.mna_due_diligence.audit import log_mna_due_diligence_audit_event
from app.mna_due_diligence.comparison_engine import run_due_diligence_comparison
from app.mna_due_diligence.models import (
    MNAComparisonRequest,
    MNADueDiligenceReport,
    MNAJobProgress,
    MNAJobStatus,
)
from app.mna_due_diligence.snapshot import build_entity_snapshot

logger = logging.getLogger(__name__)

# Fallback in-memory job store if Redis is unreachable or during unit tests
_IN_MEMORY_JOBS: dict[str, MNAJobProgress] = {}


_REDIS_CLIENT: sync_redis.Redis | None = None
_REDIS_CHECKED: bool = False


def _get_redis_client(settings: Settings) -> sync_redis.Redis | None:
    global _REDIS_CLIENT, _REDIS_CHECKED
    if _REDIS_CHECKED:
        return _REDIS_CLIENT
    try:
        client = sync_redis.from_url(
            settings.redis_url,
            decode_responses=True,
            socket_timeout=0.1,
            socket_connect_timeout=0.1,
        )
        client.ping()
        _REDIS_CLIENT = client
    except Exception:
        _REDIS_CLIENT = None
    _REDIS_CHECKED = True
    return _REDIS_CLIENT


def _job_key(settings: Settings, job_id: str) -> str:
    return f"{settings.mna_key_prefix}:job:{job_id}"


class MNAJobManager:
    """Manages M&A compliance due-diligence jobs with Redis/Memory tracking."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def save_job(self, job: MNAJobProgress) -> None:
        """Persists job state to Redis with in-memory fallback."""
        job.updated_at = dt.datetime.now(dt.timezone.utc)
        _IN_MEMORY_JOBS[job.job_id] = job

        redis_client = _get_redis_client(self.settings)
        if redis_client:
            try:
                redis_client.set(
                    _job_key(self.settings, job.job_id),
                    job.model_dump_json(),
                    ex=self.settings.mna_redis_ttl_seconds,
                )
            except Exception as exc:
                logger.debug("Failed to write MNA job to Redis: %s", exc)

    def get_job(self, job_id: str) -> MNAJobProgress | None:
        """Fetches job state from Redis or in-memory fallback."""
        redis_client = _get_redis_client(self.settings)
        if redis_client:
            try:
                raw = redis_client.get(_job_key(self.settings, job_id))
                if raw:
                    return MNAJobProgress.model_validate_json(raw)
            except Exception as exc:
                logger.debug("Failed to read MNA job from Redis: %s", exc)

        return _IN_MEMORY_JOBS.get(job_id)

    def cancel_job(self, job_id: str) -> bool:
        """Sets cancellation flag on an active job."""
        job = self.get_job(job_id)
        if not job:
            return False
        if job.status in (MNAJobStatus.COMPLETED, MNAJobStatus.FAILED, MNAJobStatus.CANCELLED):
            return False

        job.cancelled = True
        job.status = MNAJobStatus.CANCELLED
        job.current_step = "Job cancelled by user"
        job.completed_at = dt.datetime.now(dt.timezone.utc)
        self.save_job(job)
        logger.info("M&A job %s cancelled.", job_id)
        return True


async def run_mna_job_pipeline(
    job_id: str,
    request: MNAComparisonRequest,
    initiator: str,
    session_factory: Callable[[], AsyncSession],
    ledger_engine: AsyncEngine,
    settings: Settings,
) -> MNADueDiligenceReport:
    """Orchestrates the multi-stage M&A due-diligence pipeline with status checkpoints."""
    job_manager = MNAJobManager(settings)
    job = job_manager.get_job(job_id) or MNAJobProgress(
        job_id=job_id,
        entity_a_id=request.entity_a_id,
        entity_b_id=request.entity_b_id,
        initiator_subject=initiator,
        status=MNAJobStatus.QUEUED,
        progress_pct=0,
    )
    job_manager.save_job(job)

    start_time = dt.datetime.now(dt.timezone.utc)
    timeout_limit = start_time + dt.timedelta(seconds=settings.mna_job_timeout_seconds)

    def _check_cancellation_and_timeout():
        current_job = job_manager.get_job(job_id)
        if current_job and current_job.cancelled:
            raise asyncio.CancelledError(f"Job {job_id} was cancelled by user.")
        if dt.datetime.now(dt.timezone.utc) > timeout_limit:
            raise TimeoutError(f"Job {job_id} exceeded timeout limit of {settings.mna_job_timeout_seconds}s.")

    try:
        # Step 1: Snapshot Acquisition (0% -> 30%)
        _check_cancellation_and_timeout()
        job.status = MNAJobStatus.SNAPSHOT_ACQUISITION
        job.progress_pct = 15
        job.current_step = "Extracting isolated snapshots for Entity A and Entity B"
        job_manager.save_job(job)

        async with session_factory() as session:
            snapshot_a = await build_entity_snapshot(session, request.entity_a_id)
            _check_cancellation_and_timeout()
            snapshot_b = await build_entity_snapshot(session, request.entity_b_id)

        job.progress_pct = 35
        job.current_step = "Snapshots generated and cryptographically hashed"
        job_manager.save_job(job)

        # Step 2: Deterministic Diffing (35% -> 60%)
        _check_cancellation_and_timeout()
        job.status = MNAJobStatus.DETERMINISTIC_DIFFING
        job.progress_pct = 50
        job.current_step = "Evaluating deterministic rule and threshold differences"
        job_manager.save_job(job)

        # Step 3: Advisory Semantic Evaluation (60% -> 85%)
        _check_cancellation_and_timeout()
        job.status = MNAJobStatus.SEMANTIC_ANALYSIS
        job.progress_pct = 75
        job.current_step = "Conducting advisory semantic and risk overlay comparison"
        job_manager.save_job(job)

        report = await run_due_diligence_comparison(
            snapshot_a=snapshot_a,
            snapshot_b=snapshot_b,
            job_id=job_id,
            initiator=initiator,
            settings=settings,
            scope=request.comparison_scope,
            advisory_mode=request.advisory_mode,
        )

        # Step 4: Audit Logging & Database Record Persistence (85% -> 100%)
        _check_cancellation_and_timeout()
        job.current_step = "Logging comparison audit event to tamper-evident ledger"
        job.progress_pct = 90
        job_manager.save_job(job)

        await log_mna_due_diligence_audit_event(ledger_engine, report)

        # Persist to relational database table mna_comparison_jobs
        async with session_factory() as session:
            try:
                db_job = MNAComparisonJob(
                    job_id=job_id,
                    entity_a_id=request.entity_a_id,
                    entity_b_id=request.entity_b_id,
                    initiator_subject=initiator,
                    status="COMPLETED",
                    progress_pct=100,
                    current_step="Due-diligence report ready for review",
                    entity_a_snapshot_hash=report.entity_a_snapshot_hash,
                    entity_b_snapshot_hash=report.entity_b_snapshot_hash,
                    overall_risk_score=report.overall_risk_score,
                    findings_count=len(report.findings),
                    report_data=report.model_dump(mode="json"),
                    started_at=start_time,
                    completed_at=dt.datetime.now(dt.timezone.utc),
                )
                session.add(db_job)
                await session.commit()
            except Exception as exc:
                logger.warning("Could not persist MNA comparison job to database table: %s", exc)
                await session.rollback()

        job.status = MNAJobStatus.COMPLETED
        job.progress_pct = 100
        job.current_step = "Due-diligence report ready for review"
        job.completed_at = dt.datetime.now(dt.timezone.utc)
        job.report = report
        job_manager.save_job(job)
        logger.info("M&A Due-Diligence job %s completed successfully.", job_id)
        return report

    except asyncio.CancelledError:
        logger.warning("M&A Due-Diligence job %s was cancelled.", job_id)
        job.status = MNAJobStatus.CANCELLED
        job.current_step = "Job cancelled"
        job.completed_at = dt.datetime.now(dt.timezone.utc)
        job_manager.save_job(job)
        raise

    except TimeoutError as exc:
        logger.error("M&A Due-Diligence job %s timed out: %s", job_id, exc)
        job.status = MNAJobStatus.FAILED
        job.error_message = str(exc)
        job.completed_at = dt.datetime.now(dt.timezone.utc)
        job_manager.save_job(job)
        raise

    except Exception as exc:
        logger.exception("M&A Due-Diligence job %s failed: %s", job_id, exc)
        job.status = MNAJobStatus.FAILED
        job.error_message = str(exc)
        job.completed_at = dt.datetime.now(dt.timezone.utc)
        job_manager.save_job(job)
        raise
