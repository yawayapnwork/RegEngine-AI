"""Requirement 1 -- Traffic Shadowing Engine: duplicates a live
transaction's already-built OPA `input_doc` to a candidate policy
running alongside production, without ever affecting or delaying the
real decision.

There is no live message bus to "duplicate traffic" off in this
codebase (see this package's `__init__.py` docstring) -- the honest
integration point is the exact place
`app.execution.evaluator.Evaluator.evaluate_transaction` already builds
`input_doc` and calls `OPAEngine.evaluate` for each applicable
production policy. `spawn_shadow_evaluation` is meant to be called from
that call site, ONE extra line, wrapped in `asyncio.create_task` so it
runs concurrently with (never blocking) the real evaluation the
synchronous `/evaluate` endpoint must "return instantly" for.
"""
from __future__ import annotations

import asyncio
import logging
import time

from app.canary.models import CanaryDecision, CanaryRun
from app.canary.opa_publisher import canary_opa_rule_id
from app.canary.orchestrator import CanaryOrchestrator
from app.canary.parity import ParityAnalyzer
from app.execution.opa_engine import OPAEngine, OPAEngineError

logger = logging.getLogger(__name__)


class ShadowTrafficMirror:
    def __init__(self, opa_engine: OPAEngine, parity_analyzer: ParityAnalyzer, orchestrator: CanaryOrchestrator) -> None:
        self._opa = opa_engine
        self._parity = parity_analyzer
        self._orchestrator = orchestrator

    async def shadow_evaluate(self, run: CanaryRun, transaction_id: str, input_doc: dict) -> None:
        """Evaluates BOTH the production package (again -- a second,
        independent call, not a reuse of the live path's own result;
        see this method's docstring note below on why) and the
        candidate package, records the comparison, and immediately
        checks for a rollback-worthy divergence spike.

        Deliberately swallows every exception rather than letting one
        propagate: a shadow evaluation failing must never surface to,
        retry against, or in any way perturb the live transaction path
        that already returned its real decision by the time this runs.
        """
        try:
            await self._shadow_evaluate(run, transaction_id, input_doc)
        except Exception:  # noqa: BLE001 - a shadow-path failure must never propagate
            logger.exception("Shadow evaluation failed for canary %s, transaction %s.", run.canary_id, transaction_id)

    async def _shadow_evaluate(self, run: CanaryRun, transaction_id: str, input_doc: dict) -> None:
        # Re-evaluating production here (rather than reusing the
        # PolicyOutcome the live Evaluator already computed) is
        # deliberate: this comparison must measure the CURRENT state of
        # the production package at the moment the candidate is also
        # evaluated, immune to any staleness in a value captured earlier
        # in the request, and it lets this module be called from any
        # context that only has a raw input_doc (e.g. a future replay
        # tool), not only from inside Evaluator's own request lifecycle.
        production_result, production_latency_ms, production_error = await self._timed_evaluate(run.production_package, input_doc)
        candidate_result, candidate_latency_ms, candidate_error = await self._timed_evaluate(run.candidate_package, input_doc)

        await self._parity.compare_and_record(
            run.canary_id, transaction_id,
            production_result=production_result, production_latency_ms=production_latency_ms, production_error=production_error,
            candidate_result=candidate_result, candidate_latency_ms=candidate_latency_ms, candidate_error=candidate_error,
        )

        decision = await self._parity.check_rollback_spike(run.canary_id)
        if decision == CanaryDecision.ROLLBACK:
            await self._orchestrator.rollback(
                run.canary_id,
                reason=f"Divergence spike detected in real time (candidate_opa_rule_id={run.candidate_opa_rule_id}).",
            )

    async def _timed_evaluate(self, package: str, input_doc: dict) -> tuple[dict | None, float, str | None]:
        started = time.perf_counter()
        try:
            result = await self._opa.evaluate(package, input_doc)
            return result, (time.perf_counter() - started) * 1000, None
        except OPAEngineError as exc:
            return None, (time.perf_counter() - started) * 1000, str(exc)


def spawn_shadow_evaluation(mirror: ShadowTrafficMirror, runs: list[CanaryRun], transaction_id: str, input_doc: dict) -> None:
    """Fire-and-forget entrypoint for the live evaluation call site:
    schedules one shadow evaluation per RUNNING canary whose
    `rule_id`/package this transaction's policies touched, and returns
    immediately -- never awaited by the caller. A canary's own
    `production_package` field scopes which transactions it shadows
    (only ones that actually exercise the policy under test), so a
    caller can pass every active canary for the transaction's
    entity_type unconditionally without needing to pre-filter."""
    for run in runs:
        asyncio.create_task(mirror.shadow_evaluate(run, transaction_id, input_doc))
