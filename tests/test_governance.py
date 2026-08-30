"""Tests for app.governance: the Board-Level Governance & Kill-Switch
Control Engine.

Redis is faked throughout (this repo's established `_FakeRedis`
convention); Postgres is a real in-memory SQLite engine (matches
tests/test_ledger.py's convention) so `KillSwitchEvent`/`AgentInventory`
rows are genuinely inserted/queried via real SQLAlchemy, not mocked.
`Evaluator`'s kill-switch fallback is tested against real OPA/HITL test
doubles matching tests/test_opa_execution.py's established shape --
proving NO OPA call happens while the switch is active, not just that
some flag was set.
"""
from __future__ import annotations

import datetime as dt

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.analytics.models import Granularity, ReportPeriod
from app.db.base import Base
from app.db.models import AgentInventory, Circular, Clause, CompiledRule, HITLReview, KillSwitchEvent, Tenant
from app.execution.evaluator import Evaluator
from app.execution.models import HITLCase, PolicyOutcome, TransactionPayload
from app.governance.drills import run_kill_switch_drill
from app.governance.inventory import (
    OWNERSHIP_REVIEW_WINDOW_DAYS,
    SEED_AGENTS,
    agents_overdue_for_review,
    get_agent,
    list_agents,
    register_agent,
    retire_agent,
    seed_agent_inventory,
    update_agent,
)
from app.governance.kill_switch import KillSwitchStore, kill_switch
from app.governance.middleware import KillSwitchMiddleware
from app.governance.reporting import build_governance_report
from app.governance.schemas import AgentInventoryCreate, AgentInventoryUpdate, KillSwitchScope
from app.ledger.models import ComplianceEvaluationEvent, EvaluationOutcome, compliance_audit_ledger
from app.ledger.service import LedgerService


# --------------------------------------------------------------------------
# Fakes
# --------------------------------------------------------------------------


class _FakeRedis:
    def __init__(self) -> None:
        self.strings: dict[str, str] = {}
        self.sets: dict[str, set[str]] = {}

    async def set(self, key: str, value: str) -> None:
        self.strings[key] = value

    async def get(self, key: str) -> str | None:
        return self.strings.get(key)

    async def delete(self, key: str) -> None:
        self.strings.pop(key, None)

    async def exists(self, key: str) -> int:
        return 1 if key in self.strings else 0

    async def sadd(self, key: str, member: str) -> None:
        self.sets.setdefault(key, set()).add(member)

    async def srem(self, key: str, member: str) -> None:
        self.sets.get(key, set()).discard(member)

    async def smembers(self, key: str) -> set[str]:
        return set(self.sets.get(key, set()))

    def pipeline(self):
        outer = self

        class _Pipe:
            def __init__(self) -> None:
                self.ops: list[tuple] = []

            def set(self, key, value):
                self.ops.append(("set", key, value))
                return self

            def delete(self, key):
                self.ops.append(("delete", key))
                return self

            def sadd(self, key, member):
                self.ops.append(("sadd", key, member))
                return self

            def srem(self, key, member):
                self.ops.append(("srem", key, member))
                return self

            async def execute(self) -> None:
                for op in self.ops:
                    if op[0] == "set":
                        outer.strings[op[1]] = op[2]
                    elif op[0] == "delete":
                        outer.strings.pop(op[1], None)
                    elif op[0] == "sadd":
                        outer.sets.setdefault(op[1], set()).add(op[2])
                    elif op[0] == "srem":
                        outer.sets.get(op[1], set()).discard(op[2])

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

        return _Pipe()


class _FakeOPAEngine:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def evaluate(self, package: str, input_doc: dict) -> dict | None:
        self.calls.append((package, input_doc))
        return {"allow": True, "violations": []}


class _FakePolicyRegistry:
    def __init__(self, policies: dict[str, list[dict]]) -> None:
        self._policies = policies

    async def policies_for(self, entity_type: str) -> list[dict]:
        return self._policies.get(entity_type, [])


class _FakeHITLQueue:
    def __init__(self) -> None:
        self.enqueued: list[dict] = []

    async def enqueue(self, transaction: TransactionPayload, reason: str, matched_policies: list[PolicyOutcome]) -> HITLCase:
        case = HITLCase(case_id=f"case-{len(self.enqueued) + 1}", transaction=transaction, reason=reason, matched_policies=matched_policies)
        self.enqueued.append({"transaction_id": transaction.transaction_id, "reason": reason})
        return case


def _transaction(**overrides) -> TransactionPayload:
    base = dict(transaction_id="TXN-001", entity_type="Stockbroker", facts={"upfront_margin_pct": 25.0}, broker_id="BRK-001")
    base.update(overrides)
    return TransactionPayload(**base)


# --------------------------------------------------------------------------
# KillSwitchStore
# --------------------------------------------------------------------------


@pytest.mark.asyncio
class TestKillSwitchStore:
    async def test_global_activate_and_deactivate(self):
        store = KillSwitchStore(_FakeRedis(), "test:gov")
        assert await store.is_global_active() is False

        await store.activate_global("emergency", "admin@x")
        assert await store.is_global_active() is True

        await store.deactivate_global()
        assert await store.is_global_active() is False

    async def test_tenant_activate_and_deactivate(self):
        store = KillSwitchStore(_FakeRedis(), "test:gov")
        await store.activate_tenant("BRK-001", "suspicious activity", "officer@x")

        assert await store.is_tenant_active("BRK-001") is True
        assert await store.is_tenant_active("BRK-002") is False

        await store.deactivate_tenant("BRK-001")
        assert await store.is_tenant_active("BRK-001") is False

    async def test_is_active_for_checks_global_or_tenant(self):
        store = KillSwitchStore(_FakeRedis(), "test:gov")
        assert await store.is_active_for("BRK-001") is False

        await store.activate_tenant("BRK-001", "r", "a")
        assert await store.is_active_for("BRK-001") is True
        assert await store.is_active_for("BRK-002") is False
        assert await store.is_active_for(None) is False

        await store.deactivate_tenant("BRK-001")
        await store.activate_global("r", "a")
        assert await store.is_active_for("BRK-002") is True  # global covers every tenant
        assert await store.is_active_for(None) is True

    async def test_get_status_reports_both_scopes(self):
        store = KillSwitchStore(_FakeRedis(), "test:gov")
        await store.activate_global("global reason", "admin@x")
        await store.activate_tenant("BRK-001", "tenant reason", "officer@x")

        status = await store.get_status()
        assert status.global_status.active is True
        assert status.global_status.reason == "global reason"
        assert len(status.tenant_statuses) == 1
        assert status.tenant_statuses[0].tenant_id == "BRK-001"
        assert status.tenant_statuses[0].reason == "tenant reason"


# --------------------------------------------------------------------------
# kill_switch() -- Redis + durable Postgres event, dual-write
# --------------------------------------------------------------------------


@pytest_asyncio.fixture
async def db_engine():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def db_session(db_engine):
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as session:
        yield session


@pytest.mark.asyncio
class TestKillSwitchFunction:
    async def test_activate_global_dual_writes(self, db_session):
        store = KillSwitchStore(_FakeRedis(), "test:gov")
        result = await kill_switch(store, db_session, scope=KillSwitchScope.GLOBAL, activate=True, reason="board directive", actor="admin@x")

        assert result.action == "activated"
        assert await store.is_global_active() is True

        from sqlalchemy import select

        row = (await db_session.execute(select(KillSwitchEvent).where(KillSwitchEvent.event_id == result.event_id))).scalar_one()
        assert row.scope == "global"
        assert row.tenant_id is None
        assert row.reason == "board directive"
        assert row.actor == "admin@x"
        assert row.is_drill is False

    async def test_tenant_scope_requires_tenant_id(self, db_session):
        store = KillSwitchStore(_FakeRedis(), "test:gov")
        with pytest.raises(ValueError, match="tenant_id is required"):
            await kill_switch(store, db_session, scope=KillSwitchScope.TENANT, activate=True, reason="r", actor="a")

    async def test_global_scope_rejects_tenant_id(self, db_session):
        store = KillSwitchStore(_FakeRedis(), "test:gov")
        with pytest.raises(ValueError, match="must not be set"):
            await kill_switch(store, db_session, scope=KillSwitchScope.GLOBAL, activate=True, reason="r", actor="a", tenant_id="BRK-001")

    async def test_deactivate_persists_event_too(self, db_session):
        store = KillSwitchStore(_FakeRedis(), "test:gov")
        await kill_switch(store, db_session, scope=KillSwitchScope.TENANT, activate=True, reason="r", actor="a", tenant_id="BRK-001")
        result = await kill_switch(store, db_session, scope=KillSwitchScope.TENANT, activate=False, reason="resolved", actor="a", tenant_id="BRK-001")

        assert result.action == "deactivated"
        assert await store.is_tenant_active("BRK-001") is False


# --------------------------------------------------------------------------
# KillSwitchMiddleware
# --------------------------------------------------------------------------


@pytest.mark.asyncio
class TestKillSwitchMiddleware:
    async def _build_app(self, store, settings):
        from starlette.applications import Starlette
        from starlette.responses import PlainTextResponse
        from starlette.routing import Route

        async def homepage(request):
            return PlainTextResponse("ok")

        async def healthz(request):
            return PlainTextResponse("healthy")

        app = Starlette(routes=[
            Route("/v1/execution/transactions/evaluate", homepage, methods=["POST"]),
            Route("/healthz", healthz),
            Route("/v1/governance/kill-switch/status", homepage),
        ])
        app.add_middleware(KillSwitchMiddleware, settings=settings, kill_switch_store=store)
        return app

    async def test_blocks_when_global_switch_active(self):
        import httpx

        from app.config import Settings

        store = KillSwitchStore(_FakeRedis(), "test:gov")
        await store.activate_global("emergency", "admin@x")
        app = await self._build_app(store, Settings())

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post("/v1/execution/transactions/evaluate")
            assert resp.status_code == 503
            assert resp.json()["kill_switch_active"] is True

    async def test_exempt_paths_pass_through_even_when_active(self):
        import httpx

        from app.config import Settings

        store = KillSwitchStore(_FakeRedis(), "test:gov")
        await store.activate_global("emergency", "admin@x")
        app = await self._build_app(store, Settings())

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            healthz_resp = await client.get("/healthz")
            assert healthz_resp.status_code == 200

            governance_resp = await client.get("/v1/governance/kill-switch/status")
            assert governance_resp.status_code == 200  # never lock yourself out of the control that turns it off

    async def test_passes_through_when_inactive(self):
        import httpx

        from app.config import Settings

        store = KillSwitchStore(_FakeRedis(), "test:gov")
        app = await self._build_app(store, Settings())

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post("/v1/execution/transactions/evaluate")
            assert resp.status_code == 200


# --------------------------------------------------------------------------
# Evaluator kill-switch fallback
# --------------------------------------------------------------------------


@pytest.mark.asyncio
class TestEvaluatorKillSwitchFallback:
    async def test_no_opa_call_and_flagged_when_globally_active(self):
        store = KillSwitchStore(_FakeRedis(), "test:gov")
        await store.activate_global("halt", "admin@x")

        opa = _FakeOPAEngine()
        registry = _FakePolicyRegistry({"Stockbroker": [{"rule_id": "r1", "package": "p1"}]})
        hitl = _FakeHITLQueue()
        evaluator = Evaluator(opa_engine=opa, policy_registry=registry, hitl_queue=hitl, kill_switch_store=store)

        result = await evaluator.evaluate_transaction(_transaction())

        assert result.decision.value == "flagged"
        assert result.hitl_case_id == "case-1"
        assert opa.calls == []  # the whole point: OPA is never even called
        assert len(hitl.enqueued) == 1
        assert "kill switch" in hitl.enqueued[0]["reason"].lower()

    async def test_tenant_specific_halt_only_affects_that_tenant(self):
        store = KillSwitchStore(_FakeRedis(), "test:gov")
        await store.activate_tenant("BRK-999", "halt", "officer@x")

        opa = _FakeOPAEngine()
        registry = _FakePolicyRegistry({"Stockbroker": [{"rule_id": "r1", "package": "p1"}]})
        hitl = _FakeHITLQueue()
        evaluator = Evaluator(opa_engine=opa, policy_registry=registry, hitl_queue=hitl, kill_switch_store=store)

        result = await evaluator.evaluate_transaction(_transaction(broker_id="BRK-001"))

        assert result.decision.value == "allow"  # BRK-001 is unaffected
        assert len(opa.calls) == 1

    async def test_no_kill_switch_store_preserves_old_behavior(self):
        opa = _FakeOPAEngine()
        registry = _FakePolicyRegistry({"Stockbroker": [{"rule_id": "r1", "package": "p1"}]})
        hitl = _FakeHITLQueue()
        evaluator = Evaluator(opa_engine=opa, policy_registry=registry, hitl_queue=hitl)  # no kill_switch_store at all

        result = await evaluator.evaluate_transaction(_transaction())
        assert result.decision.value == "allow"
        assert len(opa.calls) == 1


# --------------------------------------------------------------------------
# Agent inventory
# --------------------------------------------------------------------------


@pytest.mark.asyncio
class TestAgentInventory:
    async def test_seed_is_idempotent(self, db_session):
        created_once = await seed_agent_inventory(db_session)
        assert len(created_once) == len(SEED_AGENTS)

        created_twice = await seed_agent_inventory(db_session)
        assert created_twice == []  # already present -- nothing new created

        all_agents = await list_agents(db_session)
        assert len(all_agents) == len(SEED_AGENTS)
        assert {a.agent_key for a in all_agents} == {a.agent_key for a in SEED_AGENTS}

    async def test_every_seed_agent_has_a_named_owner_and_real_model_version(self):
        for spec in SEED_AGENTS:
            assert spec.owner_name and " " in spec.owner_name  # a real "First Last" name, not a role alias
            assert "@" in spec.owner_email
            assert spec.model_weight_version == "Qwen/Qwen2.5-72B-Instruct"

    async def test_register_get_update_retire_round_trip(self, db_session):
        spec = AgentInventoryCreate(
            agent_key="test_agent", display_name="Test Agent", model_provider="huggingface",
            model_weight_version="Qwen/Qwen2.5-72B-Instruct", business_domain="testing",
            owner_name="Test Owner", owner_email="owner@test.com",
        )
        created = await register_agent(db_session, spec)
        assert created.id is not None

        fetched = await get_agent(db_session, "test_agent")
        assert fetched.display_name == "Test Agent"

        updated = await update_agent(db_session, "test_agent", AgentInventoryUpdate(owner_name="New Owner", mark_reviewed=True))
        assert updated.owner_name == "New Owner"
        assert updated.last_reviewed_at is not None

        retired = await retire_agent(db_session, "test_agent")
        assert retired.is_active is False
        assert retired.retired_at is not None

        active_only = await list_agents(db_session, active_only=True)
        assert "test_agent" not in {a.agent_key for a in active_only}

    async def test_update_nonexistent_agent_returns_none(self, db_session):
        assert await update_agent(db_session, "does_not_exist", AgentInventoryUpdate(owner_name="X")) is None
        assert await retire_agent(db_session, "does_not_exist") is None


def test_agents_overdue_for_review():
    now = dt.datetime(2026, 8, 29, tzinfo=dt.timezone.utc)
    recent = AgentInventory(agent_key="recent", display_name="d", model_provider="p", model_weight_version="v", business_domain="b", owner_name="n", owner_email="e", deployed_at=now - dt.timedelta(days=10), last_reviewed_at=now - dt.timedelta(days=10))
    overdue = AgentInventory(agent_key="overdue", display_name="d", model_provider="p", model_weight_version="v", business_domain="b", owner_name="n", owner_email="e", deployed_at=now - dt.timedelta(days=OWNERSHIP_REVIEW_WINDOW_DAYS + 30), last_reviewed_at=None)

    result = agents_overdue_for_review([recent, overdue], now=now)
    assert result == ["overdue"]


# --------------------------------------------------------------------------
# Kill-switch drill
# --------------------------------------------------------------------------


@pytest.mark.asyncio
class TestKillSwitchDrill:
    async def test_global_drill_passes_and_restores_original_state(self, db_session):
        store = KillSwitchStore(_FakeRedis(), "test:gov")
        result = await run_kill_switch_drill(store, db_session, scope=KillSwitchScope.GLOBAL, actor="admin@x")

        assert result.passed is True
        assert await store.is_global_active() is False  # deactivated again at the end of the drill

    async def test_tenant_drill_passes(self, db_session):
        store = KillSwitchStore(_FakeRedis(), "test:gov")
        result = await run_kill_switch_drill(store, db_session, scope=KillSwitchScope.TENANT, actor="officer@x", tenant_id="BRK-001")

        assert result.passed is True
        assert await store.is_tenant_active("BRK-001") is False

    async def test_drill_events_are_marked_is_drill_and_annotated(self, db_session):
        from sqlalchemy import select

        store = KillSwitchStore(_FakeRedis(), "test:gov")
        await run_kill_switch_drill(store, db_session, scope=KillSwitchScope.GLOBAL, actor="admin@x")

        events = (await db_session.execute(select(KillSwitchEvent))).scalars().all()
        assert len(events) == 2  # activation + deactivation
        assert all(e.is_drill for e in events)
        assert all(e.details.get("passed") is True for e in events)


# --------------------------------------------------------------------------
# Governance reporting -- reuses ComplianceAggregator + collect_hitl_approvals
# --------------------------------------------------------------------------


@pytest_asyncio.fixture
async def ledger_engine():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(compliance_audit_ledger.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest.mark.asyncio
class TestGovernanceReporting:
    async def test_report_combines_compliance_hitl_inventory_and_drills(self, ledger_engine, db_session):
        # Seed ledger: one PASS, one FAIL.
        service = LedgerService(ledger_engine)
        day = dt.datetime(2026, 8, 15, tzinfo=dt.timezone.utc)
        await service.append_entry(ComplianceEvaluationEvent(
            broker_id="BRK-001", transaction_id="T1", evaluated_at=day,
            circular_id="C1", clause_hash="a" * 64, section_reference="1.1",
            rule_id="a" * 64 + ":1.1", evaluation_result=EvaluationOutcome.PASS,
        ))
        await service.append_entry(ComplianceEvaluationEvent(
            broker_id="BRK-001", transaction_id="T2", evaluated_at=day,
            circular_id="C1", clause_hash="a" * 64, section_reference="1.1",
            rule_id="a" * 64 + ":1.1", evaluation_result=EvaluationOutcome.FAIL,
        ))

        # Seed a tenant + circular/clause/hitl_review chain (mirrors app.reporting.data_collector's own join requirements).
        tenant = Tenant(tenant_id="BRK-001", display_name="Test Broker", tenant_type="stockbroker", opa_bundle_prefix="tenants/brk_001")
        db_session.add(tenant)
        await db_session.flush()

        circular = Circular(circular_number="C1", tenant_id="BRK-001", raw_text_digest="a" * 64)
        db_session.add(circular)
        await db_session.flush()
        clause = Clause(circular_id=circular.id, clause_number="1.1", element_kind="clause", text="clause text", sha256="a" * 64, tenant_id="BRK-001")
        db_session.add(clause)
        await db_session.flush()
        review = HITLReview(
            review_id="rev-1", clause_id=clause.id, tenant_id="BRK-001", reason_code="audit_not_approved",
            severity="blocking", description="d", status="RESOLVED", flagged_at=day, resolved_at=day + dt.timedelta(hours=1),
        )
        db_session.add(review)

        # Seed the agent inventory + a kill-switch drill.
        await seed_agent_inventory(db_session)
        store = KillSwitchStore(_FakeRedis(), "test:gov")
        await run_kill_switch_drill(store, db_session, scope=KillSwitchScope.GLOBAL, actor="admin@x")

        await db_session.commit()

        period = ReportPeriod(start_date=dt.date(2026, 8, 1), end_date=dt.date(2026, 8, 31), granularity=Granularity.MONTHLY)
        report = await build_governance_report(ledger_engine, db_session, period, generated_by="admin@x")

        assert report.total_agent_executions == 2
        assert report.decision_error_rate_pct == pytest.approx(50.0)
        assert report.active_agent_count == len(SEED_AGENTS)
        assert report.critical_operation_agent_count == len(SEED_AGENTS)  # every seed agent is critical
        assert report.human_reviews_total == 1
        assert report.human_overrides_approved == 1
        assert report.human_override_rate_pct == pytest.approx(100.0)
        assert report.kill_switch_drills_total == 1
        assert report.kill_switch_drills_passed == 1
        assert len(report.kill_switch_drill_results) == 1
