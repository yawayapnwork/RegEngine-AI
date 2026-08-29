"""Celery wrapper around `app.healing.orchestrator.SelfHealingLoop`,
matching `app.compiler.tasks.compile_audited_rule_task`'s shape:
synchronous task body, one `asyncio.run` for the async orchestrator
call, DLQ routing on exhaustion via the same
`app.resilience.celery_helpers.route_to_dlq_sync` helper every other
non-retryable pipeline failure in this codebase uses.

Trigger points (either can dispatch this task; both already exist in
the codebase and already catch the exception this task's caller needs
to react to):
  - `app.execution.policy_hot_reload.PolicyHotReloadSubscriber._apply_active`
    re-raises `OPAEngineError` after logging it -- a caller wrapping that
    re-publish attempt can catch it and dispatch
    `self_heal_policy_task.delay(...)` with a `PolicyFailure` built via
    `app.healing.detectors.build_failure_from_opa_error`.
  - `app.compiler.tasks.compile_audited_rule_task`'s `MalformedASTError`
    path is deliberately left untouched (see
    `app.healing.orchestrator`'s module docstring for why) -- this task
    is NOT wired as an alternative to that one.
"""
from __future__ import annotations

import asyncio
import logging

import redis.asyncio as aioredis

from app.config import get_settings
from app.execution.celery_app import celery_app
from app.healing.models import HealingOutcome, PolicyFailure, SelfHealingResult
from app.healing.orchestrator import SelfHealingLoop
from app.healing.tracking import HealingAttemptTracker
from app.resilience.celery_helpers import route_to_dlq_sync
from app.resilience.models import FailureCategory

logger = logging.getLogger(__name__)


async def _run_heal(failure_dict: dict, test_fixtures: list[dict]) -> SelfHealingResult:
    settings = get_settings()
    redis_client = aioredis.from_url(settings.redis_url, decode_responses=True)
    try:
        tracker = HealingAttemptTracker(redis_client, settings.policy_self_healing_key_prefix)
        loop = SelfHealingLoop(tracker, settings)
        failure = PolicyFailure.model_validate(failure_dict)
        return await loop.heal(failure, test_fixtures)
    finally:
        await redis_client.aclose()


@celery_app.task(name="app.healing.tasks.self_heal_policy_task", bind=True)
def self_heal_policy_task(self, failure_dict: dict, test_fixtures: list[dict]) -> dict:
    """`failure_dict` is `PolicyFailure.model_dump(mode="json")`.
    Returns `SelfHealingResult.model_dump(mode="json")`. Never raises on
    a HEALED or gracefully-escalated outcome -- only an unexpected
    internal error (a bug in this task itself, not a policy defect) is
    allowed to propagate and hit Celery's own retry/failure machinery."""
    settings = get_settings()
    if not settings.policy_self_healing_enabled:
        logger.info("policy_self_healing_enabled is False; skipping self-heal for rule_id=%s and routing straight to DLQ.", failure_dict.get("rule_id"))
        result = SelfHealingResult(rule_id=failure_dict["rule_id"], outcome=HealingOutcome.ESCALATED_MAX_RETRIES, attempts=[])
    else:
        result = asyncio.run(_run_heal(failure_dict, test_fixtures))

    if result.outcome != HealingOutcome.HEALED:
        route_to_dlq_sync(
            category=FailureCategory.POLICY_SELF_HEAL_EXHAUSTED,
            task_name="app.healing.tasks.self_heal_policy_task",
            payload={"failure": failure_dict, "test_fixtures": test_fixtures},
            exc=RuntimeError(f"Self-healing loop concluded with outcome={result.outcome.value} after {len(result.attempts)} attempt(s)."),
            original_task_id=self.request.id,
            attempt_count=len(result.attempts) or 1,
        )
        logger.error("Self-healing exhausted for rule_id=%s (outcome=%s); routed to DLQ.", result.rule_id, result.outcome.value)
    else:
        logger.info("Self-healing succeeded for rule_id=%s after %d attempt(s); ready for the HITL pipeline.", result.rule_id, len(result.attempts))

    return result.model_dump(mode="json")
