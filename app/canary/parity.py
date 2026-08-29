"""Requirement 2 -- Parity Analyzer: turns one production/candidate OPA
call pair into a `ComparisonResult` and folds it into the canary's
running `CanaryWindowStats` in real time.

This is the "worker" in the requirement's "parity evaluation workers"
sense in the same way `app.execution.opa_engine.OPAEngine.evaluate` is
itself the single choke point every evaluation path already runs
through: `ParityAnalyzer.compare_and_record` is called once per
shadowed transaction from inside the fire-and-forget task
`app.canary.mirroring` spawns, so "real time" here means "as each
transaction is shadowed," not "batched and processed later" -- there is
no separate queue/broker hop for the comparison step itself (the
periodic Celery sweep in `app.canary.tasks` is a DIFFERENT concern:
deciding whether an already-accumulated window of comparisons warrants
promotion/rollback, not computing the comparisons).
"""
from __future__ import annotations

import logging

from app.canary.models import CanaryDecision, ComparisonResult
from app.canary.store import CanaryStore
from app.execution.models import Decision

logger = logging.getLogger(__name__)


def decision_from_opa_result(result: dict | None) -> Decision:
    """Same reduction OPA outcome -> Decision as one entry of
    app.execution.evaluator.Evaluator._reduce, applied to a SINGLE
    policy result rather than reduced across many -- a shadow
    comparison is inherently one production package against one
    candidate package, not a multi-policy transaction-wide decision."""
    if result is None:
        return Decision.FLAGGED
    return Decision.DENY if result.get("violations") else Decision.ALLOW


class ParityAnalyzer:
    def __init__(self, store: CanaryStore, rollback_divergence_pct: float, rollback_min_sample_size: int) -> None:
        self._store = store
        self._rollback_divergence_pct = rollback_divergence_pct
        self._rollback_min_sample_size = rollback_min_sample_size

    async def compare_and_record(
        self,
        canary_id: str,
        transaction_id: str,
        *,
        production_result: dict | None,
        production_latency_ms: float,
        production_error: str | None,
        candidate_result: dict | None,
        candidate_latency_ms: float,
        candidate_error: str | None,
    ) -> ComparisonResult:
        comparison = ComparisonResult(
            transaction_id=transaction_id,
            production_decision=Decision.FLAGGED if production_error else decision_from_opa_result(production_result),
            candidate_decision=Decision.FLAGGED if candidate_error else decision_from_opa_result(candidate_result),
            production_latency_ms=production_latency_ms,
            candidate_latency_ms=candidate_latency_ms,
            production_error=production_error,
            candidate_error=candidate_error,
        )
        await self._store.record_comparison(canary_id, comparison)
        if comparison.diverged:
            logger.info(
                "Canary %s divergence on transaction %s: production=%s candidate=%s",
                canary_id, transaction_id, comparison.production_decision.value, comparison.candidate_decision.value,
            )
        return comparison

    async def check_rollback_spike(self, canary_id: str) -> CanaryDecision:
        """Called immediately after every recorded comparison (not just
        on the periodic sweep) so a genuine divergence spike triggers a
        rollback within one comparison of crossing the threshold, not
        up to `canary_evaluation_sweep_interval_seconds` later."""
        run = await self._store.get(canary_id)
        if run is None:
            return CanaryDecision.CONTINUE
        if run.stats.total_compared < self._rollback_min_sample_size:
            return CanaryDecision.CONTINUE
        if run.stats.divergence_pct >= self._rollback_divergence_pct:
            return CanaryDecision.ROLLBACK
        return CanaryDecision.CONTINUE
