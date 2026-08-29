"""Requirement 3's periodic automation: Celery tasks bridging the
canary state machine to Celery Beat, following
app.execution.tasks's established "one asyncio.run per task invocation"
pattern (Celery tasks execute outside an event loop; each task opens
exactly one for its unit of work).

Split into two tasks deliberately:

  * `evaluate_canary_windows_task` (periodic, Beat-scheduled): Redis-only
    -- lists every RUNNING canary and calls
    `CanaryOrchestrator.evaluate_window` (pure state read, no Postgres/
    OPA writes). A ROLLBACK decision is acted on immediately, in this
    same task, since removing a namespaced OPA policy has no Postgres
    dependency. A PROMOTE decision is NOT acted on here -- it enqueues
    `promote_canary_task` instead.
  * `promote_canary_task` (one-shot, enqueued by the sweep): the only
    task in this module that touches Postgres. It builds its OWN
    `AsyncEngine`/session, scoped to this single invocation, and
    disposes it before returning -- deliberately NOT the process-wide
    `app.db.session.get_engine()` singleton, whose connection pool is
    bound to whatever event loop was running when it was first created.
    A Celery task's `asyncio.run` creates a NEW loop every invocation;
    reusing a pool created under a previous (already-closed) loop is a
    real, easy-to-hit failure mode this module avoids by never sharing
    an engine across task invocations.
"""
from __future__ import annotations

import asyncio
import logging

import redis.asyncio as async_redis
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.canary.dependencies import get_canary_orchestrator
from app.canary.models import CanaryDecision
from app.config import get_settings
from app.execution.celery_app import celery_app
from app.execution.policy_events import PolicyEventPublisher

logger = logging.getLogger(__name__)


@celery_app.task(bind=True, max_retries=3, default_retry_delay=30)
def evaluate_canary_windows_task(self) -> dict[str, int]:
    return asyncio.run(_evaluate_canary_windows())


async def _evaluate_canary_windows() -> dict[str, int]:
    settings = get_settings()
    redis_client = async_redis.Redis.from_url(settings.redis_url, decode_responses=True)
    try:
        orchestrator = get_canary_orchestrator(redis_client, settings)
        active_runs = await orchestrator.list_active_runs()

        outcome_counts = {"continue": 0, "promoted_enqueued": 0, "rolled_back": 0}
        for run in active_runs:
            decision = await orchestrator.evaluate_window(run.canary_id)
            if decision == CanaryDecision.ROLLBACK:
                await orchestrator.rollback(run.canary_id, reason=f"Divergence {run.stats.divergence_pct:.2%} over {run.stats.total_compared} compared transaction(s) at or above the rollback bar.")
                outcome_counts["rolled_back"] += 1
            elif decision == CanaryDecision.PROMOTE:
                promote_canary_task.delay(run.canary_id)
                outcome_counts["promoted_enqueued"] += 1
            else:
                outcome_counts["continue"] += 1

        logger.info("Canary window sweep: %s", outcome_counts)
        return outcome_counts
    finally:
        await redis_client.aclose()


@celery_app.task(bind=True, max_retries=3, default_retry_delay=30)
def promote_canary_task(self, canary_id: str) -> None:
    asyncio.run(_promote_canary(canary_id))


async def _promote_canary(canary_id: str) -> None:
    settings = get_settings()
    redis_client = async_redis.Redis.from_url(settings.redis_url, decode_responses=True)
    engine = create_async_engine(settings.database_url, pool_size=2, pool_pre_ping=True)
    try:
        orchestrator = get_canary_orchestrator(redis_client, settings)
        session_factory = async_sessionmaker(bind=engine, expire_on_commit=False)
        policy_publisher = PolicyPublisher(PolicyEventPublisher(redis_client))

        async with session_factory() as session:
            await orchestrator.promote(canary_id, session, policy_publisher)
            await session.commit()
    finally:
        await engine.dispose()
        await redis_client.aclose()
