"""Bridges `app.compiler` output into the execution service: the one place
where a `CompilationResult` becomes a live, evaluable policy.

Only rules that compiled cleanly (`compiled=True`, i.e. no BLOCKING HITL
flag per `app.compiler.pipeline`) are ever published to OPA — an
unresolved compiler-time HITL flag must never reach the execution engine
as a silently-permissive or silently-denying policy.
"""
from __future__ import annotations

import logging

from app.agents.schemas import ExtractedComplianceRule
from app.compiler.models import CompilationResult
from app.execution.opa_engine import OPAEngine
from app.execution.policy_registry import PolicyRegistry

logger = logging.getLogger(__name__)


async def publish_compiled_rule(
    result: CompilationResult,
    rule: ExtractedComplianceRule,
    opa_engine: OPAEngine,
    policy_registry: PolicyRegistry,
) -> bool:
    """Returns True if a policy was published, False if it was skipped
    (uncompiled or Rego-less result)."""
    if not result.compiled or result.rego is None:
        logger.info("Skipping publish for rule %s: not compiled or no Rego output.", result.rule_id)
        return False

    entity_types = sorted({e.normalized_entity or e.raw_text for e in rule.target_entities if (e.normalized_entity or e.raw_text)})

    await opa_engine.publish_policy(result.rego)
    await policy_registry.register(result.rego, entity_types)
    logger.info("Published policy for rule %s (package=%s, entity_types=%s).", result.rule_id, result.rego.package, entity_types or ["*"])
    return True


async def retract_rule(rule_id: str, opa_engine: OPAEngine, policy_registry: PolicyRegistry) -> None:
    """Used when a rule is superseded/repealed: removes it from OPA and the
    registry so it stops being evaluated against new transactions."""
    await opa_engine.remove_policy(rule_id)
    await policy_registry.unregister(rule_id)
    logger.info("Retracted policy for rule %s.", rule_id)
