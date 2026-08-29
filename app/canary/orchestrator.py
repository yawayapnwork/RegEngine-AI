"""Requirement 3 -- Automated Canary Promotion: the state machine
driving a canary from RUNNING to either PROMOTED or ROLLED_BACK.

`evaluate_window` is pure Redis-state decision-making (no Postgres, no
OPA) so it's safe to call from the periodic Celery sweep
(`app.canary.tasks.evaluate_canary_windows_task`) without touching a
process-wide cached SQLAlchemy engine across separate `asyncio.run`
invocations (each Celery task body gets its own event loop -- see that
module's docstring for why that makes a cached async engine/connection
pool unsafe to share across invocations). `promote` is the one method
that DOES need Postgres (to load the candidate's compiled Rego) and is
therefore only ever called from a dedicated, single-purpose Celery task
that opens and disposes its own engine within one `asyncio.run` call.
"""
from __future__ import annotations

import logging
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.canary.models import CanaryDecision, CanaryRun, CanaryStatus
from app.canary.opa_publisher import CanaryOPAPublisher
from app.canary.store import CanaryStore
from app.compiler.models import CompiledRego
from app.db.models import CompiledRule
from app.execution.policy_publisher import PolicyPublisher

logger = logging.getLogger(__name__)


class CanaryOrchestrator:
    def __init__(
        self,
        store: CanaryStore,
        opa_publisher: CanaryOPAPublisher,
        *,
        promotion_max_divergence_pct: float,
        rollback_divergence_pct: float,
        rollback_min_sample_size: int,
        evaluation_window_seconds: int,
    ) -> None:
        self._store = store
        self._opa_publisher = opa_publisher
        self._promotion_max_divergence_pct = promotion_max_divergence_pct
        self._rollback_divergence_pct = rollback_divergence_pct
        self._rollback_min_sample_size = rollback_min_sample_size
        self._window_seconds = evaluation_window_seconds

    async def start_canary(
        self,
        production_compiled_rule: CompiledRule,
        candidate_compiled: CompiledRego,
        candidate_compiled_rule_id: int,
    ) -> CanaryRun:
        """Publishes the candidate to OPA under a namespaced package
        (never reachable at production's own package path) and begins
        tracking it. `production_compiled_rule` is never republished --
        it's already live; only its `opa_package_name` is needed, to
        know what to shadow-compare against."""
        if not production_compiled_rule.opa_package_name:
            raise ValueError(f"CompiledRule {production_compiled_rule.id} has no opa_package_name; it isn't published.")

        canary_id = str(uuid.uuid4())
        namespaced_rule_id, namespaced_package = await self._opa_publisher.publish_candidate(canary_id, candidate_compiled)

        run = CanaryRun(
            canary_id=canary_id,
            rule_id=production_compiled_rule.rule_id,
            tenant_id=production_compiled_rule.tenant_id,
            production_package=production_compiled_rule.opa_package_name,
            candidate_package=namespaced_package,
            candidate_opa_rule_id=namespaced_rule_id,
            candidate_compiled_rule_id=candidate_compiled_rule_id,
        )
        await self._store.create(run)
        logger.info("Started canary %s for rule_id=%s (candidate published as %s).", canary_id, run.rule_id, namespaced_package)
        return run

    async def list_active_runs(self) -> list[CanaryRun]:
        return await self._store.list_active()

    async def evaluate_window(self, canary_id: str) -> CanaryDecision:
        """Redis-only decision, safe to call from the periodic sweep.
        Does NOT itself promote or roll back -- returns the decision;
        the caller (app.canary.tasks) is responsible for acting on it,
        so this method stays trivially unit-testable without any OPA/
        Postgres dependency."""
        run = await self._store.get(canary_id)
        if run is None or run.status != CanaryStatus.RUNNING:
            return CanaryDecision.CONTINUE

        if run.stats.total_compared >= self._rollback_min_sample_size and run.stats.divergence_pct >= self._rollback_divergence_pct:
            return CanaryDecision.ROLLBACK

        if run.window_elapsed(self._window_seconds) and run.stats.divergence_pct <= self._promotion_max_divergence_pct:
            return CanaryDecision.PROMOTE

        return CanaryDecision.CONTINUE

    async def rollback(self, canary_id: str, reason: str) -> CanaryRun:
        run = await self._store.get(canary_id)
        if run is None:
            raise KeyError(f"No canary run with id '{canary_id}'")
        if run.status != CanaryStatus.RUNNING:
            return run  # already resolved (e.g. a concurrent spike check and window sweep both fired) -- idempotent no-op

        await self._opa_publisher.remove_candidate(run.candidate_opa_rule_id)
        resolved = await self._store.mark_rolled_back(canary_id, reason)
        logger.warning("Canary %s rolled back: %s", canary_id, reason)
        return resolved

    async def promote(self, canary_id: str, session: AsyncSession, policy_publisher: PolicyPublisher) -> CanaryRun:
        """The only method in this class that touches Postgres/publishes
        for real -- promoting a canary means the candidate now IS
        production, so this goes through the exact same
        `PolicyPublisher.publish_approved` path any human-approved
        publish does (see that class's docstring: its entire contract
        is "this now affects live decisions," which is precisely what
        promotion means -- no separate publish mechanism is invented
        here)."""
        run = await self._store.get(canary_id)
        if run is None:
            raise KeyError(f"No canary run with id '{canary_id}'")
        if run.status != CanaryStatus.RUNNING:
            return run

        candidate_rule = await session.get(CompiledRule, run.candidate_compiled_rule_id)
        if candidate_rule is None:
            raise ValueError(f"CompiledRule {run.candidate_compiled_rule_id} for canary {canary_id} no longer exists.")

        await policy_publisher.publish_approved(candidate_rule, approved_by="canary-auto-promotion")
        # The namespaced OPA copy has served its purpose -- remove it now
        # that the candidate's real package is (or is about to be, once
        # the hot-reload subscriber picks up the publish event) live, so
        # the same Rego isn't left resident in OPA under two ids forever.
        await self._opa_publisher.remove_candidate(run.candidate_opa_rule_id)

        resolved = await self._store.mark_promoted(canary_id, f"Divergence {run.stats.divergence_pct:.2%} over {run.stats.total_compared} compared transaction(s), within the {self._promotion_max_divergence_pct:.2%} promotion bar after the evaluation window elapsed.")
        logger.info("Canary %s promoted: rule_id=%s is now live.", canary_id, run.rule_id)
        return resolved
