"""Tests for app.fix_gateway.hot_reload -- the subscriber that keeps
the FIX gateway's in-memory native policy set current from the same
`regengine:policy_events` channel app.execution.policy_hot_reload
listens on.

Hand-rolled fakes only (no real Redis/Postgres), matching this
session's established convention for testing a hot-reload/pub-sub
subscriber's event-handling logic in isolation from `run()`'s own
reconnect loop (see tests/test_agent_graph.py's `_FakeRedis` idiom).
"""
from __future__ import annotations

import pytest

from app.db.models import Circular, Clause, CompiledRule
from app.execution.policy_events import PolicyEvent, PolicyEventType
from app.fix_gateway.hot_reload import FixGatewayHotReloadSubscriber, FixPolicyStore


def _compiled_rule(rule_id: str, jsonlogic_ast: dict, circular_number: str = "SEBI/HO/MIRSD/2024/100", clause_number: str = "4.2.b") -> CompiledRule:
    circular = Circular(id=1, tenant_id="t1", circular_number=circular_number, title="Test Circular")
    clause = Clause(id=1, circular_id=1, tenant_id="t1", clause_number=clause_number, text="x", sha256="a" * 64)
    clause.circular = circular
    compiled_rule = CompiledRule(id=1, clause_id=1, tenant_id="t1", rule_id=rule_id, rule_version=1, jsonlogic_ast=jsonlogic_ast)
    compiled_rule.clause = clause
    return compiled_rule


class _FakeSession:
    def __init__(self, compiled_rule: CompiledRule | None) -> None:
        self._compiled_rule = compiled_rule

    async def scalar(self, _stmt):
        return self._compiled_rule

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeSessionFactory:
    def __init__(self, compiled_rule: CompiledRule | None) -> None:
        self._compiled_rule = compiled_rule

    def __call__(self):
        return _FakeSession(self._compiled_rule)


@pytest.mark.asyncio
class TestFixGatewayHotReloadSubscriber:
    async def test_approved_event_loads_a_packageable_policy(self) -> None:
        compiled_rule = _compiled_rule("qty-rule", {"<=": [{"var": "facts.order_qty"}, 10000]})
        store = FixPolicyStore()
        subscriber = FixGatewayHotReloadSubscriber(redis_client=None, session_factory=_FakeSessionFactory(compiled_rule), store=store)

        event = PolicyEvent(event_type=PolicyEventType.APPROVED, rule_id="qty-rule", rule_version=1, package="sebi.broking.x", rego_code="package x\n", compiled_rule_id=1, approved_by="officer-1")
        await subscriber._handle_raw_event(event.model_dump_json())

        loaded = store.current_policies()
        assert len(loaded) == 1
        assert loaded[0].rule_id == "qty-rule"
        assert loaded[0].rejection.clause_ref == "SEBI/HO/MIRSD/2024/100:4.2.b"

    async def test_revoked_event_removes_the_policy(self) -> None:
        compiled_rule = _compiled_rule("qty-rule", {"<=": [{"var": "facts.order_qty"}, 10000]})
        store = FixPolicyStore()
        subscriber = FixGatewayHotReloadSubscriber(redis_client=None, session_factory=_FakeSessionFactory(compiled_rule), store=store)

        approved = PolicyEvent(event_type=PolicyEventType.APPROVED, rule_id="qty-rule", rule_version=1, package="sebi.broking.x", rego_code="package x\n", compiled_rule_id=1)
        await subscriber._handle_raw_event(approved.model_dump_json())
        assert len(store.current_policies()) == 1

        revoked = PolicyEvent(event_type=PolicyEventType.REVOKED, rule_id="qty-rule", rule_version=1, package="sebi.broking.x", compiled_rule_id=1)
        await subscriber._handle_raw_event(revoked.model_dump_json())
        assert store.current_policies() == []

    async def test_unpackageable_policy_is_skipped_not_crashed(self) -> None:
        compiled_rule = _compiled_rule("margin-rule", {"<=": [{"var": "facts.margin_utilization_pct"}, 80]})
        store = FixPolicyStore()
        subscriber = FixGatewayHotReloadSubscriber(redis_client=None, session_factory=_FakeSessionFactory(compiled_rule), store=store)

        event = PolicyEvent(event_type=PolicyEventType.APPROVED, rule_id="margin-rule", rule_version=1, package="sebi.broking.x", rego_code="package x\n", compiled_rule_id=1)
        await subscriber._handle_raw_event(event.model_dump_json())  # must not raise

        assert store.current_policies() == []

    async def test_missing_compiled_rule_is_skipped_not_crashed(self) -> None:
        store = FixPolicyStore()
        subscriber = FixGatewayHotReloadSubscriber(redis_client=None, session_factory=_FakeSessionFactory(None), store=store)

        event = PolicyEvent(event_type=PolicyEventType.APPROVED, rule_id="gone-rule", rule_version=1, package="sebi.broking.x", rego_code="package x\n", compiled_rule_id=999)
        await subscriber._handle_raw_event(event.model_dump_json())  # must not raise

        assert store.current_policies() == []
