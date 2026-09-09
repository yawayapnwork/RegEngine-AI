"""Celery worker for backtest runs -- mirrors
app.execution.tasks.process_batch_task's shape exactly (Redis-backed job
status keyed by run_id, one asyncio.run per task invocation) since a
backtest over 30-90 days of order flow is a batch job with the same
"submit, poll for status" lifecycle as a legacy SFTP batch.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging

import redis as sync_redis
from sqlalchemy.ext.asyncio import create_async_engine

from app.backtest.models import (
    BacktestOutcome,
    BacktestRun,
    BacktestRunRequest,
    BacktestStatus,
    RuleImpactPreviewReport,
)
from app.backtest.orchestrator import run_backtest
from app.config import get_settings
from app.execution.celery_app import celery_app

logger = logging.getLogger(__name__)

# Paginated separately from the run's own status/summary record -- a
# 90-day backtest can produce tens of thousands of BacktestOutcome rows,
# too large to hold in one Redis string comfortably; storing them as their
# own list lets GET /v1/backtest/runs/{id} stay small and fast while
# GET /v1/backtest/runs/{id}/delta paginates through the rest.
_OUTCOMES_PAGE_SIZE = 500


def _sync_redis() -> sync_redis.Redis:
    return sync_redis.from_url(get_settings().redis_url, decode_responses=True)


def _run_key(run_id: str) -> str:
    return f"{get_settings().backtest_key_prefix}:run:{run_id}"


def _outcomes_key(run_id: str) -> str:
    return f"{get_settings().backtest_key_prefix}:outcomes:{run_id}"


def save_run(run: BacktestRun) -> None:
    _sync_redis().set(_run_key(run.run_id), run.model_dump_json(), ex=30 * 24 * 3600)  # 30-day TTL: a backtest result is a point-in-time analysis artifact, not a permanent record like the ledger


def get_run(run_id: str) -> BacktestRun | None:
    raw = _sync_redis().get(_run_key(run_id))
    return BacktestRun.model_validate_json(raw) if raw else None


def save_outcomes(run_id: str, outcomes: list[BacktestOutcome]) -> None:
    redis_client = _sync_redis()
    key = _outcomes_key(run_id)
    if outcomes:
        redis_client.rpush(key, *(o.model_dump_json() for o in outcomes))
        redis_client.expire(key, 30 * 24 * 3600)


def get_outcomes_page(run_id: str, page: int, page_size: int = _OUTCOMES_PAGE_SIZE) -> tuple[list[BacktestOutcome], int]:
    redis_client = _sync_redis()
    key = _outcomes_key(run_id)
    total = redis_client.llen(key)
    start = (page - 1) * page_size
    raw_rows = redis_client.lrange(key, start, start + page_size - 1)
    return [BacktestOutcome.model_validate_json(r) for r in raw_rows], total


@celery_app.task(name="app.backtest.tasks.run_backtest_task", bind=True, max_retries=1)
def run_backtest_task(self, run_id: str, request_dict: dict) -> dict:
    request = BacktestRunRequest.model_validate(request_dict)
    run = BacktestRun(run_id=run_id, status=BacktestStatus.RUNNING, request=request, started_at=dt.datetime.now(dt.timezone.utc))
    save_run(run)

    settings = get_settings()
    ledger_engine = create_async_engine(settings.ledger_database_url, pool_pre_ping=True)

    try:
        summary, outcomes = asyncio.run(run_backtest(request, settings, ledger_engine, run_id))
        run.summary = summary
        run.status = BacktestStatus.COMPLETED
        save_outcomes(run_id, outcomes)
    except Exception as exc:  # noqa: BLE001 - report failure, don't crash the worker
        logger.exception("Backtest run '%s' failed.", run_id)
        run.status = BacktestStatus.FAILED
        run.error = str(exc)
    finally:
        run.completed_at = dt.datetime.now(dt.timezone.utc)
        save_run(run)
        asyncio.run(ledger_engine.dispose())

    return run.model_dump(mode="json")


# --- Rule-Impact Preview / Digital Twin Task & Storage ---

_IN_MEMORY_PREVIEW_REPORTS: dict[str, str] = {}
_IN_MEMORY_CANCELLATIONS: set[str] = set()


def _preview_key(preview_id: str) -> str:
    return f"{get_settings().backtest_key_prefix}:preview:{preview_id}"


def _preview_cancel_key(preview_id: str) -> str:
    return f"{get_settings().backtest_key_prefix}:preview:{preview_id}:cancel"


def save_preview_report(report: RuleImpactPreviewReport) -> None:
    """Stores a RuleImpactPreviewReport in Redis (with in-memory fallback)."""
    ttl = get_settings().rule_preview_redis_ttl_seconds
    raw = report.model_dump_json()
    try:
        r = _sync_redis()
        r.set(_preview_key(report.preview_id), raw, ex=ttl)
    except Exception:
        logger.debug("Redis unavailable for save_preview_report; storing in-memory.")
        _IN_MEMORY_PREVIEW_REPORTS[report.preview_id] = raw


def get_preview_report(preview_id: str) -> RuleImpactPreviewReport | None:
    """Retrieves a RuleImpactPreviewReport from Redis or in-memory fallback."""
    try:
        r = _sync_redis()
        raw = r.get(_preview_key(preview_id))
        if raw:
            return RuleImpactPreviewReport.model_validate_json(raw)
    except Exception:
        logger.debug("Redis unavailable for get_preview_report; falling back to in-memory.")
    
    raw_mem = _IN_MEMORY_PREVIEW_REPORTS.get(preview_id)
    return RuleImpactPreviewReport.model_validate_json(raw_mem) if raw_mem else None


def cancel_preview(preview_id: str) -> None:
    """Sets the cancellation signal for an ongoing preview replay."""
    try:
        r = _sync_redis()
        r.set(_preview_cancel_key(preview_id), "1", ex=3600)
    except Exception:
        _IN_MEMORY_CANCELLATIONS.add(preview_id)


def is_preview_cancelled(preview_id: str) -> bool:
    """Checks whether cancellation has been requested for this preview."""
    if preview_id in _IN_MEMORY_CANCELLATIONS:
        return True
    try:
        r = _sync_redis()
        return bool(r.exists(_preview_cancel_key(preview_id)))
    except Exception:
        return preview_id in _IN_MEMORY_CANCELLATIONS


@celery_app.task(name="app.backtest.tasks.run_rule_impact_preview_task", bind=True, max_retries=1)
def run_rule_impact_preview_task(self, preview_id: str, request_dict: dict) -> dict:
    from app.backtest.models import PreviewRunRequest
    from app.backtest.orchestrator import run_rule_impact_preview

    request = PreviewRunRequest.model_validate(request_dict)
    settings = get_settings()
    ledger_engine = create_async_engine(settings.ledger_database_url, pool_pre_ping=True)

    try:
        report = asyncio.run(run_rule_impact_preview(request, settings, ledger_engine, preview_id))
        save_preview_report(report)
        return report.model_dump(mode="json")
    except Exception as exc:
        logger.exception("Rule impact preview task '%s' failed: %s", preview_id, exc)
        raise
    finally:
        asyncio.run(ledger_engine.dispose())

