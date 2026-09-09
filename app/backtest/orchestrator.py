"""Ties fetch -> replay -> report into one call -- the single function
both the Celery task (app.backtest.tasks) and a synchronous test/CLI
caller invoke, so the two never drift on what "running a backtest"
actually means.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time

from sqlalchemy.ext.asyncio import AsyncEngine

from app.backtest.candidate_evaluator import CandidateEvaluator, JsonLogicCandidateEvaluator, OpaCandidateEvaluator
from app.backtest.models import (
    BacktestOutcome,
    BacktestRunRequest,
    BacktestSummary,
    PreviewRunRequest,
    RuleImpactPreviewReport,
)
from app.backtest.replay_engine import compute_dataset_snapshot_hash, fetch_historical_transactions, replay_all
from app.backtest.reporting import build_outcomes, build_rule_impact_preview_report, build_summary
from app.config import Settings
from app.execution.opa_engine import OPAEngine
from app.observability.metrics import (
    RULE_PREVIEW_DURATION_SECONDS,
    RULE_PREVIEW_EXECUTIONS_TOTAL,
    RULE_PREVIEW_NEW_FAILURES_TOTAL,
    RULE_PREVIEW_TRANSACTIONS_EVALUATED_TOTAL,
)

logger = logging.getLogger(__name__)


def compute_policy_hash(ast: dict | None = None, package_or_code: str | None = None) -> str:
    """Computes a deterministic cryptographic SHA-256 hash of the policy AST or text."""
    if ast is not None:
        raw = json.dumps(ast, sort_keys=True)
    elif package_or_code is not None:
        raw = str(package_or_code).strip()
    else:
        raw = "empty_candidate_policy"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def build_candidate_evaluator(request: BacktestRunRequest, settings: Settings) -> CandidateEvaluator:
    if request.candidate_jsonlogic_ast is not None:
        return JsonLogicCandidateEvaluator(request.candidate_jsonlogic_ast)
    if request.candidate_opa_package is not None:
        # Deliberately backtest_opa_server_url, never opa_server_url --
        # see app.backtest.candidate_evaluator's module docstring.
        opa = OPAEngine(base_url=settings.backtest_opa_server_url, timeout_seconds=settings.opa_request_timeout_seconds)
        return OpaCandidateEvaluator(opa, request.candidate_opa_package)
    raise ValueError("BacktestRunRequest must set exactly one of candidate_jsonlogic_ast or candidate_opa_package.")


async def run_backtest(
    request: BacktestRunRequest,
    settings: Settings,
    ledger_engine: AsyncEngine,
    run_id: str,
) -> tuple[BacktestSummary, list[BacktestOutcome]]:
    evaluator = build_candidate_evaluator(request, settings)

    transactions = await fetch_historical_transactions(
        ledger_engine, request.candidate_rule_id, request.lookback_days, request.tenant_id
    )
    logger.info(
        "Backtest run=%s: replaying %d historical transaction(s) for rule_id=%s over the last %d day(s).",
        run_id, len(transactions), request.candidate_rule_id, request.lookback_days,
    )

    replayed = await replay_all(transactions, evaluator, settings.backtest_concurrency)
    outcomes = build_outcomes(replayed)
    summary = build_summary(request.candidate_rule_id, request.lookback_days, outcomes, run_id=run_id)

    logger.info(
        "Backtest run=%s complete: %d transactions, failure rate %.2f%% -> %.2f%% (%+.2f pp), %d new failure(s), %d newly-passing.",
        run_id, summary.total_transactions, summary.old_failure_rate_pct, summary.new_failure_rate_pct,
        summary.delta_failure_rate_pct, summary.new_failures, summary.newly_passing,
    )
    return summary, outcomes


async def run_rule_impact_preview(
    request: PreviewRunRequest,
    settings: Settings,
    ledger_engine: AsyncEngine,
    preview_id: str | None = None,
    cancellation_token: asyncio.Event | None = None,
) -> RuleImpactPreviewReport:
    """Executes an isolated real-data rule-impact preview / digital twin replay.

    Safety Invariants:
    1. Never modifies historical transactions.
    2. Never activates, deploys, or alters the candidate rule's live production state.
    3. Strictly partitions historical transactions by scope.tenant_id.
    4. Evaluates in bounded time with cancellation and runaway query limits.
    """
    start_time = time.perf_counter()

    # Determine evaluator and candidate policy hash
    if request.candidate_jsonlogic_ast is not None:
        evaluator = JsonLogicCandidateEvaluator(request.candidate_jsonlogic_ast)
        candidate_hash = compute_policy_hash(ast=request.candidate_jsonlogic_ast)
        evaluator_version = "regengine-jsonlogic-v1"
    elif request.candidate_opa_package is not None:
        opa = OPAEngine(base_url=settings.backtest_opa_server_url, timeout_seconds=settings.opa_request_timeout_seconds)
        evaluator = OpaCandidateEvaluator(opa, request.candidate_opa_package)
        candidate_hash = compute_policy_hash(package_or_code=request.candidate_opa_package)
        evaluator_version = "regengine-opa-v1"
    else:
        raise ValueError("PreviewRunRequest must specify candidate_jsonlogic_ast or candidate_opa_package.")

    scope = request.scope
    timeout_sec = settings.rule_preview_timeout_seconds

    async def _execute() -> RuleImpactPreviewReport:
        # Fetch historical transactions with strict tenant isolation and limits
        transactions = await fetch_historical_transactions(
            ledger_engine=ledger_engine,
            rule_id=scope.rule_id,
            lookback_days=scope.lookback_days,
            tenant_id=scope.tenant_id,
            start_time=scope.start_time,
            end_time=scope.end_time,
            entity_type=scope.entity_type,
            limit=scope.max_transactions,
            scope=scope,
        )

        dataset_snapshot_hash = compute_dataset_snapshot_hash(transactions)

        # Replay transactions
        replayed = await replay_all(
            transactions=transactions,
            evaluator=evaluator,
            concurrency=settings.backtest_concurrency,
            cancellation_token=cancellation_token,
        )

        # Generate report
        report = build_rule_impact_preview_report(
            candidate_rule_id=request.candidate_rule_id,
            candidate_policy_hash=candidate_hash,
            scope=scope,
            dataset_snapshot_hash=dataset_snapshot_hash,
            replayed=replayed,
            evaluator_version=evaluator_version,
            preview_id=preview_id,
        )
        return report

    try:
        report = await asyncio.wait_for(_execute(), timeout=timeout_sec)
        elapsed = time.perf_counter() - start_time
        RULE_PREVIEW_EXECUTIONS_TOTAL.labels(status="completed").inc()
        RULE_PREVIEW_DURATION_SECONDS.observe(elapsed)
        RULE_PREVIEW_TRANSACTIONS_EVALUATED_TOTAL.inc(report.total_evaluated)
        RULE_PREVIEW_NEW_FAILURES_TOTAL.inc(report.newly_affected)
        logger.info(
            "Rule impact preview completed: preview_id=%s, tenant=%s, evaluated=%d, newly_affected=%d in %.2fs",
            report.preview_id, scope.tenant_id, report.total_evaluated, report.newly_affected, elapsed,
        )
        return report
    except asyncio.TimeoutError:
        RULE_PREVIEW_EXECUTIONS_TOTAL.labels(status="timeout").inc()
        logger.error("Rule impact preview timed out after %.1fs (tenant=%s, rule=%s)", timeout_sec, scope.tenant_id, scope.rule_id)
        raise TimeoutError(f"Rule impact preview timed out after {timeout_sec} seconds.")
    except asyncio.CancelledError:
        RULE_PREVIEW_EXECUTIONS_TOTAL.labels(status="cancelled").inc()
        logger.warning("Rule impact preview cancelled by user (tenant=%s, rule=%s)", scope.tenant_id, scope.rule_id)
        raise
    except Exception as exc:
        RULE_PREVIEW_EXECUTIONS_TOTAL.labels(status="failed").inc()
        logger.exception("Rule impact preview failed: %s", exc)
        raise

