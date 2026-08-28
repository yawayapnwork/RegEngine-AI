"""Orchestrates HITL flagging + Rego + JSON-Logic compilation for one
AuditedComplianceRule (the output of the extraction/audit crew).

Compilation gate: a rule is only compiled to Rego/JSON-Logic when it has at
least one NumericalThreshold AND carries no BLOCKING HITL flag (unapproved
audit, low confidence, conflicting thresholds, or zero deterministic logic).
Qualitative directives and ambiguous spans are ADVISORY — they never block
compilation of the deterministic portion of the same rule, they are simply
carried alongside it for a human to separately action.
"""
from __future__ import annotations

import logging

from app.agents.schemas import AuditedComplianceRule
from app.compiler.hitl import collect_hitl_flags, has_blocking_flags
from app.compiler.jsonlogic_compiler import compile_rule_to_jsonlogic
from app.compiler.jsonlogic_validator import validate_json_logic_ast, validate_json_serializable
from app.compiler.models import CompilationResult
from app.compiler.rego_compiler import compile_rule_to_rego

logger = logging.getLogger(__name__)


def compile_audited_rule(audited: AuditedComplianceRule) -> CompilationResult:
    rule = audited.rule
    hitl_flags = collect_hitl_flags(audited)

    if has_blocking_flags(hitl_flags) or not rule.deterministic_logic:
        logger.info(
            "Rule %s not compiled: %d blocking flag(s), %d threshold(s).",
            rule.rule_id,
            sum(1 for f in hitl_flags if f.severity.value == "blocking"),
            len(rule.deterministic_logic),
        )
        return CompilationResult(rule_id=rule.rule_id, compiled=False, hitl_flags=hitl_flags)

    rego = compile_rule_to_rego(rule)
    json_logic = compile_rule_to_jsonlogic(rule)

    # Defense in depth: a compiler bug that emits a structurally invalid
    # JSON-Logic node (an operator dict with the wrong key count, a value
    # json.dumps can't round-trip) must be caught HERE, before the AST
    # ships to a downstream microservice's evaluator or gets persisted --
    # not discovered later as a mysterious runtime failure somewhere else
    # entirely. Raises MalformedASTError, which app.compiler.tasks'
    # Celery wrapper routes straight to the DLQ (never retried -- see
    # that exception's docstring on why retrying a compiler bug is futile).
    validate_json_logic_ast(json_logic.logic)
    validate_json_serializable(json_logic.logic, context=f"JsonLogicRule.logic for rule_id={rule.rule_id!r}")

    return CompilationResult(
        rule_id=rule.rule_id,
        compiled=True,
        rego=rego,
        json_logic=json_logic,
        hitl_flags=hitl_flags,  # advisory flags (qualitative/ambiguous/unresolved-entity) still surfaced
    )


def compile_audited_rules(audited_rules: list[AuditedComplianceRule]) -> list[CompilationResult]:
    return [compile_audited_rule(a) for a in audited_rules]
