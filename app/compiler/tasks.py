"""Celery wrapper around app.compiler.pipeline.compile_audited_rule, giving
compilation DLQ semantics for a genuinely malformed JSON-Logic AST (a
compiler bug, not a data-quality issue -- see
app.resilience.exceptions.MalformedASTError's docstring for why that means
"never retry, a human needs to see this").

Compilation itself is pure/synchronous (no network calls), so this task
has no transient-failure retry path -- either it compiles, is correctly
blocked pending HITL review (not an error at all, see
app.compiler.pipeline's module docstring), or trips the AST validator, in
which case it goes straight to the DLQ.
"""
from __future__ import annotations

import asyncio
import logging

from app.agents.schemas import AuditedComplianceRule
from app.compiler.pipeline import compile_audited_rule
from app.config import get_settings
from app.execution.celery_app import celery_app
from app.execution.dependencies import get_redis_pool
from app.incident.publisher import raise_breach_event
from app.incident.trigger_matrix import policy_compiled_event
from app.resilience.celery_helpers import route_to_dlq_sync
from app.resilience.exceptions import MalformedASTError
from app.resilience.models import FailureCategory

logger = logging.getLogger(__name__)


def _raise_policy_compiled_event(audited: AuditedComplianceRule, package: str) -> None:
    """INFO trigger-matrix entry (Requirement 1) -- fired only on a clean
    compile with no blocking HITL flags; a partial/blocked compile is
    already visible via the HITL review queue and does not need a
    duplicate dashboard entry here."""
    try:
        settings = get_settings()
        event = policy_compiled_event(
            rule_id=audited.rule.rule_id,
            circular_number=audited.rule.circular_number,
            clause_number=audited.rule.clause_number,
            package=package,
        )
        asyncio.run(raise_breach_event(event, get_redis_pool(), settings))
    except Exception:  # noqa: BLE001 - a dashboard-feed notification failure must never fail a successful compilation
        logger.exception("Failed to raise policy-compiled breach event for rule_id=%s", audited.rule.rule_id)


@celery_app.task(name="app.compiler.tasks.compile_audited_rule_task", bind=True)
def compile_audited_rule_task(self, audited_rule_dict: dict) -> dict:
    """`audited_rule_dict` is `AuditedComplianceRule.model_dump(mode="json")`.
    Returns `CompilationResult.model_dump(mode="json")` on success. On
    MalformedASTError, routes to the DLQ (category MALFORMED_AST) and
    re-raises so the task is visibly FAILED in Celery's own result
    backend too -- the DLQ is the durable, actionable record; the task
    failure is just an accurate status, not a second source of truth."""
    audited = AuditedComplianceRule.model_validate(audited_rule_dict)

    try:
        result = compile_audited_rule(audited)
    except MalformedASTError as exc:
        logger.error("Compilation produced a malformed AST for rule_id=%s: %s", audited.rule.rule_id, exc)
        route_to_dlq_sync(
            category=FailureCategory.MALFORMED_AST,
            task_name="app.compiler.tasks.compile_audited_rule_task",
            payload={"audited_rule_dict": audited_rule_dict},
            exc=exc,
            original_task_id=self.request.id,
        )
        raise

    if result.compiled and result.rego is not None and not result.hitl_flags:
        _raise_policy_compiled_event(audited, result.rego.package)

    return result.model_dump(mode="json")
