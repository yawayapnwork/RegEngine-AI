"""Bridges "a CompiledRule's is_active flag changed in Postgres" to "OPA
and every process's PolicyCache actually reflect that" -- the write side
of the hot-reload story (app.execution.policy_hot_reload is the read/apply
side). Call this from any code path that activates or deactivates a
compiled policy.

Today that's exactly one call site: `app.api.hitl_review_routes.approve_review`,
the instant a Compliance_Officer approves a policy that was gated on human
review. A future auto-publish path for rules the compiler produces with NO
blocking HITL flags at all (clean, deterministic re-compiles needing no
human sign-off) would call `publish_amended` from wherever that
persistence happens -- this class doesn't care which path triggered it, on
purpose.
"""
from __future__ import annotations

import logging

from app.compiler.models import CompiledRego
from app.db.models import CompiledRule
from app.execution.opa_engine import OPAEngine
from app.execution.policy_cache import PolicyCache
from app.execution.policy_events import PolicyEvent, PolicyEventPublisher, PolicyEventType
from app.execution.policy_registry import PolicyRegistry

logger = logging.getLogger(__name__)


class PolicyPublisher:
    def __init__(
        self,
        event_publisher: PolicyEventPublisher,
        *,
        opa_engine: OPAEngine | None = None,
        policy_registry: PolicyRegistry | None = None,
        policy_cache: PolicyCache | None = None,
    ) -> None:
        self._events = event_publisher
        self._opa = opa_engine
        self._registry = policy_registry
        self._cache = policy_cache

    async def publish_approved(
        self, compiled_rule: CompiledRule, *, approved_by: str, entity_types: list[str] | None = None
    ) -> None:
        await self._publish_active(PolicyEventType.APPROVED, compiled_rule, entity_types, approved_by=approved_by)

    async def publish_amended(self, compiled_rule: CompiledRule, *, entity_types: list[str] | None = None) -> None:
        await self._publish_active(PolicyEventType.AMENDED, compiled_rule, entity_types, approved_by=None)

    async def _publish_active(
        self,
        event_type: PolicyEventType,
        compiled_rule: CompiledRule,
        entity_types: list[str] | None,
        *,
        approved_by: str | None,
    ) -> None:
        if not compiled_rule.rego_policy or not compiled_rule.opa_package_name:
            raise ValueError(f"CompiledRule {compiled_rule.id} has no compiled Rego/package to publish.")

        event = PolicyEvent(
            event_type=event_type,
            rule_id=compiled_rule.rule_id,
            rule_version=compiled_rule.rule_version,
            package=compiled_rule.opa_package_name,
            rego_code=compiled_rule.rego_policy,
            compiled_rule_id=compiled_rule.id,
            approved_by=approved_by,
            # Entity-type scoping isn't yet persisted on compiled_rules
            # (it lives upstream, in-memory only, on
            # ExtractedComplianceRule.target_entities) -- "*" is the safe
            # default (the policy is checked against every transaction)
            # until that column exists. Pass entity_types explicitly once
            # it does.
            entity_types=entity_types or ["*"],
        )
        await self._events.publish(event)

        if self._opa is not None and compiled_rule.rego_policy:
            compiled = CompiledRego(
                rule_id=compiled_rule.rule_id,
                package=compiled_rule.opa_package_name or "",
                rego_code=compiled_rule.rego_policy,
                thresholds_compiled=0,
            )
            try:
                await self._opa.publish_policy(compiled)
            except Exception as exc:
                logger.warning("Direct OPA publish failed (relying on subscriber): %s", exc)

        if self._registry is not None and compiled_rule.rego_policy:
            compiled = CompiledRego(
                rule_id=compiled_rule.rule_id,
                package=compiled_rule.opa_package_name or "",
                rego_code=compiled_rule.rego_policy,
                thresholds_compiled=0,
            )
            try:
                await self._registry.register(compiled, entity_types or ["*"])
            except Exception as exc:
                logger.warning("Direct PolicyRegistry register failed (relying on subscriber): %s", exc)

        if self._cache is not None:
            for et in (entity_types or ["*"]):
                self._cache.invalidate(et)

    async def publish_revoked(self, compiled_rule: CompiledRule, *, entity_types: list[str] | None = None) -> None:
        event = PolicyEvent(
            event_type=PolicyEventType.REVOKED,
            rule_id=compiled_rule.rule_id,
            rule_version=compiled_rule.rule_version,
            package=compiled_rule.opa_package_name or "",
            rego_code=None,
            compiled_rule_id=compiled_rule.id,
            entity_types=entity_types or ["*"],
        )
        await self._events.publish(event)

        if self._opa is not None:
            try:
                await self._opa.remove_policy(compiled_rule.rule_id)
            except Exception as exc:
                logger.warning("Direct OPA removal failed: %s", exc)

        if self._registry is not None:
            try:
                await self._registry.unregister(compiled_rule.rule_id)
            except Exception as exc:
                logger.warning("Direct PolicyRegistry unregister failed: %s", exc)

        if self._cache is not None:
            for et in (entity_types or ["*"]):
                self._cache.invalidate(et)
