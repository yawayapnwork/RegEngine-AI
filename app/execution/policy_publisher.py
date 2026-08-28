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

from app.db.models import CompiledRule
from app.execution.policy_events import PolicyEvent, PolicyEventPublisher, PolicyEventType


class PolicyPublisher:
    def __init__(self, event_publisher: PolicyEventPublisher) -> None:
        self._events = event_publisher

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
