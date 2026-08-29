"""Ties fetch -> replay -> report into one call -- the single function
both the Celery task (app.backtest.tasks) and a synchronous test/CLI
caller invoke, so the two never drift on what "running a backtest"
actually means.
"""
from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncEngine

from app.backtest.candidate_evaluator import CandidateEvaluator, JsonLogicCandidateEvaluator, OpaCandidateEvaluator
from app.backtest.models import BacktestOutcome, BacktestRunRequest, BacktestSummary
from app.backtest.replay_engine import fetch_historical_transactions, replay_all
from app.backtest.reporting import build_outcomes, build_summary
from app.config import Settings
from app.execution.opa_engine import OPAEngine

logger = logging.getLogger(__name__)


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
