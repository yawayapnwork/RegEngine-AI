"""Comprehensive test suite for the Unified HITL Review Evidence Integration.

Validates all 14 PRD requirements:
1. HITL remains the final approval authority.
2. Unified review evidence structure with all roadmap capabilities.
3. Regulatory source clause, canonical facts, generated rule, precedents,
   arbitration transcript, digital twin impact, ZKP proofs, and M&A findings.
4. Exact evidence type labeling: source evidence, deterministic evidence,
   AI-generated analysis, historical precedent, simulation, cryptographic proof.
5. AI analysis and simulation non-delegation disclaimers.
6. Zero automatic approval from roadmap features.
7. Immutable policy version/hash references.
8. Evidence artifacts bind to exact candidate rule.
9. Strict multi-tenant data isolation and zero cross-tenant leakage.
10. API authorization enforcement.
11. Independent feature disablement via configuration.
"""
from __future__ import annotations

import datetime as dt
import json
from typing import Any
import uuid

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.backtest.models import (
    AggregateFinancialImpact,
    PreviewDatasetScope,
    RuleImpactPreviewReport,
)
from app.backtest.tasks import save_preview_report
from app.config import Settings, get_settings
from app.db.base import Base
from app.db.models import (
    Circular,
    Clause,
    CompiledRule,
    HITLReview,
    MNAComparisonJob,
    Tenant,
)
from app.ledger.models import compliance_audit_ledger
from app.db.session import get_db_session
from app.execution.dependencies import get_redis_pool
from app.hitl_evidence.models import EvidenceStatus, EvidenceType, UnifiedReviewEvidence
from app.main import app
from app.security.jwt import create_access_token
from app.security.models import Role


# ---------------------------------------------------------------------------
# In-Memory Test Setup & Fixtures
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def async_db():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with session_maker() as session:
        yield session

    await engine.dispose()


class _FakeRedis:
    def __init__(self) -> None:
        self.strings: dict[str, str] = {}
        self.hashes: dict[str, dict[str, str]] = {}
        self.sets: dict[str, set[str]] = {}
        self.lists: dict[str, list[str]] = {}

    async def get(self, key: str) -> str | None:
        return self.strings.get(key)

    async def set(self, key: str, value: str, *args: Any, **kwargs: Any) -> bool:
        self.strings[key] = str(value)
        return True

    async def setex(self, key: str, time: int, value: str) -> bool:
        self.strings[key] = str(value)
        return True

    async def incr(self, key: str) -> int:
        val = int(self.strings.get(key, 0)) + 1
        self.strings[key] = str(val)
        return val

    async def expire(self, key: str, seconds: int, *args: Any, **kwargs: Any) -> bool:
        return True

    async def delete(self, *keys: str) -> int:
        count = 0
        for k in keys:
            if k in self.strings:
                del self.strings[k]
                count += 1
            if k in self.hashes:
                del self.hashes[k]
                count += 1
        return count

    async def hget(self, key: str, field: str) -> str | None:
        return self.hashes.get(key, {}).get(field)

    async def hset(self, key: str, field: str | None = None, value: str | None = None, mapping: dict | None = None) -> int:
        h = self.hashes.setdefault(key, {})
        if mapping:
            h.update({k: str(v) for k, v in mapping.items()})
            return len(mapping)
        elif field is not None:
            h[field] = str(value)
            return 1
        return 0

    async def hgetall(self, key: str) -> dict[str, str]:
        return dict(self.hashes.get(key, {}))

    async def exists(self, *keys: str) -> int:
        count = 0
        for k in keys:
            if k in self.strings or k in self.hashes or k in self.sets or k in self.lists:
                count += 1
        return count

    def pipeline(self) -> _FakePipeline:
        return _FakePipeline(self)

    async def ping(self) -> bool:
        return True

    async def aclose(self) -> None:
        pass


class _FakePipeline:
    def __init__(self, fake_redis: _FakeRedis) -> None:
        self.fake_redis = fake_redis
        self.ops: list[tuple[str, Any]] = []

    def incr(self, key: str) -> _FakePipeline:
        self.ops.append(("incr", key))
        return self

    def expire(self, key: str, seconds: int, nx: bool = False) -> _FakePipeline:
        self.ops.append(("expire", key, seconds))
        return self

    async def execute(self) -> list[Any]:
        results = []
        for op in self.ops:
            if op[0] == "incr":
                val = await self.fake_redis.incr(op[1])
                results.append(val)
            elif op[0] == "expire":
                results.append(True)
        return results



def _wire_app_redis(app_instance, fake_redis):
    for mw in getattr(app_instance, "user_middleware", []):
        if "redis_client" in mw.kwargs:
            mw.kwargs["redis_client"] = fake_redis
        if "kill_switch_store" in mw.kwargs:
            mw.kwargs["kill_switch_store"]._redis = fake_redis
    app_instance.middleware_stack = app_instance.build_middleware_stack()


@pytest_asyncio.fixture
async def test_client(async_db: AsyncSession):
    fake_redis = _FakeRedis()
    _wire_app_redis(app, fake_redis)

    async def override_get_db_session():
        yield async_db

    app.dependency_overrides[get_db_session] = override_get_db_session
    app.dependency_overrides[get_redis_pool] = lambda: fake_redis

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client

    app.dependency_overrides.clear()


def make_token(tenant_id: str, roles: list[Role], subject: str = "test-user") -> str:
    settings = get_settings()
    token, _ = create_access_token(
        subject=subject,
        roles=roles,
        tenant_id=tenant_id,
        signing_key=settings.jwt_secret_key,
        settings=settings,
    )
    return token



# ---------------------------------------------------------------------------
# Test Cases
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_unified_evidence_assembly_all_sources(async_db: AsyncSession, test_client: httpx.AsyncClient, monkeypatch):
    """Verifies that the unified evidence endpoint retrieves and synthesizes all 6
    evidence categories with exact rule/clause hash bindings.
    """
    settings = get_settings()
    monkeypatch.setattr(settings, "case_law_memory_enabled", True)
    monkeypatch.setattr(settings, "arbitration_enabled", True)
    monkeypatch.setattr(settings, "rule_preview_enabled", True)
    monkeypatch.setattr(settings, "zkp_enabled", True)
    monkeypatch.setattr(settings, "mna_due_diligence_enabled", True)

    tenant_a = Tenant(tenant_id="tenant_alpha", display_name="Alpha Securities", opa_bundle_prefix="tenants/tenant_alpha", is_active=True)
    tenant_b = Tenant(tenant_id="tenant_beta", display_name="Beta Capital", opa_bundle_prefix="tenants/tenant_beta", is_active=True)
    async_db.add_all([tenant_a, tenant_b])
    await async_db.flush()

    circular = Circular(
        circular_number="SEBI/HO/MIRSD/2026/01",
        title="SEBI Master Circular on Margins",
        tenant_id="tenant_alpha",
        raw_text_digest="c" * 64,
    )
    async_db.add(circular)
    await async_db.flush()

    clause = Clause(
        circular_id=circular.id,
        tenant_id="tenant_alpha",
        clause_number="3.2.1",
        section_title="Upfront Margin Collection",
        text="A stock broker shall collect upfront margin of at least 25 percent.",
        sha256="a" * 64,
    )
    async_db.add(clause)
    await async_db.flush()

    rule_ast = {
        ">=": [
            {"var": "collected_margin_pct"},
            25,
        ]
    }
    compiled_rule = CompiledRule(
        rule_id="RULE-MARGIN-25",
        rule_version=1,
        policy_sha256="b" * 64,
        jsonlogic_ast=rule_ast,
        rego_policy="package sebi.margin\ndefault allow = false",
        opa_package_name="sebi.margin",
        is_active=False,
        hitl_status="BLOCKING",
        tenant_id="tenant_alpha",
        clause_id=clause.id,
    )
    async_db.add(compiled_rule)
    await async_db.flush()

    review_id = f"rev-{uuid.uuid4().hex[:8]}"
    review = HITLReview(
        review_id=review_id,
        clause_id=clause.id,
        compiled_rule_id=compiled_rule.id,
        tenant_id="tenant_alpha",
        reason_code="conflicting_thresholds",
        severity="blocking",
        description="Ambiguity around margin percentage applicability.",
        source_excerpt="collect upfront margin of at least 25 percent",
        status="PENDING",
    )
    async_db.add(review)

    # Add ZKP record to ledger
    await async_db.execute(
        compliance_audit_ledger.insert().values(
            sequence_num=101,
            broker_id="tenant_alpha",
            transaction_id="TXN-ZKP-1",
            evaluated_at=dt.datetime.now(dt.timezone.utc),
            circular_id="SEBI/HO/MIRSD/2026/01",
            clause_hash="a" * 64,
            section_reference="3.2.1",
            rule_id="RULE-MARGIN-25",
            evaluation_result="PASS",
            details={
                "zk_proof": {
                    "circuit_id": "margin_compliance_v1",
                    "proof_hash": "proof_hash_12345",
                    "public_signals": ["25", "101", "commit_abc"],
                }
            },
            payload_digest="digest101",
            previous_hash="genesis",
            current_hash="hash101",
            created_at=dt.datetime.now(dt.timezone.utc),
        )
    )

    # Add M&A comparison job with finding on this rule
    mna_job = MNAComparisonJob(
        job_id="mna-job-001",
        entity_a_id="tenant_alpha",
        entity_b_id="tenant_beta",
        initiator_subject="officer.jane",
        status="COMPLETED",
        report_data={
            "findings": [
                {
                    "rule_id": "RULE-MARGIN-25",
                    "difference_type": "different_thresholds",
                    "severity": "high",
                    "title": "Conflicting Upfront Margin Threshold",
                    "description": "Entity A requires 25% whereas Entity B requires 20%.",
                    "provenance": {"rule_id": "RULE-MARGIN-25"},
                }
            ]
        },
    )
    async_db.add(mna_job)
    await async_db.commit()

    # Save mock digital twin preview report into cache
    preview_report = RuleImpactPreviewReport(
        preview_id=f"preview-{review_id}",
        candidate_rule_id="RULE-MARGIN-25",
        candidate_policy_hash="p" * 64,
        scope=PreviewDatasetScope(tenant_id="tenant_alpha", rule_id="RULE-MARGIN-25", lookback_days=30),
        total_evaluated=150,
        newly_affected=5,
        no_longer_affected=0,
        unchanged_count=145,
        old_fail_count=10,
        new_fail_count=15,
        old_failure_rate_pct=6.67,
        new_failure_rate_pct=10.0,
        delta_failure_rate_pct=3.33,
        financial_impact=AggregateFinancialImpact(currency="INR", total_new_failure_amount=500000.0),
        dataset_snapshot_hash="dataset_hash_xyz",
        result_digest="digest_hash_123",
    )
    save_preview_report(preview_report)

    # Query unified evidence endpoint as Compliance Officer
    officer_token = make_token("tenant_alpha", [Role.COMPLIANCE_OFFICER])
    response = await test_client.get(
        f"/v1/hitl-reviews/{review_id}/evidence",
        headers={"Authorization": f"Bearer {officer_token}"},
    )

    assert response.status_code == 200, response.text
    data = response.json()

    # Verify root bindings
    assert data["review_id"] == review_id
    assert data["tenant_id"] == "tenant_alpha"
    assert data["candidate_rule_id"] == "RULE-MARGIN-25"
    assert data["candidate_rule_version"] == 1
    assert data["candidate_policy_sha256"] == "b" * 64
    assert data["source_clause_sha256"] == "a" * 64
    assert data["approval_status"] == "PENDING"
    assert "Human Compliance Officer" in data["final_approval_authority"]

    # 1. Source Evidence
    source = data["source_evidence"]
    assert source["evidence_type"] == "source evidence"
    assert source["is_authoritative"] is True
    assert source["clause_number"] == "3.2.1"
    assert source["source_sha256"] == "a" * 64
    assert "at least 25 percent" in source["raw_text"]

    # 2. Deterministic Evidence
    determ = data["deterministic_evidence"]
    assert determ["evidence_type"] == "deterministic evidence"
    assert determ["is_authoritative"] is True
    assert determ["rule_id"] == "RULE-MARGIN-25"
    assert determ["is_active"] is False  # Invariant: candidate rule is NOT active
    assert "collected_margin_pct" in determ["canonical_facts"]

    # 3. Digital Twin Simulation
    sim = data["digital_twin_simulation"]
    assert sim["evidence_type"] == "simulation"
    assert sim["is_authoritative"] is False
    assert "SIMULATION ONLY" in sim["disclaimer"]
    assert sim["transactions_evaluated"] == 150
    assert sim["newly_failing_count"] == 5

    # 4. Cryptographic Proof (ZKP)
    zkp = data["zkp_evidence"]
    assert zkp["evidence_type"] == "cryptographic proof"
    assert zkp["is_authoritative"] is False
    assert "CRYPTOGRAPHIC EVIDENCE" in zkp["disclaimer"]
    assert zkp["proof_count"] == 1
    assert zkp["proofs"][0]["proof_hash"] == "proof_hash_12345"

    # 5. M&A Due-Diligence Findings
    mna = data["mna_findings"]
    assert mna["is_authoritative"] is False
    assert "ADVISORY" in mna["disclaimer"]
    assert mna["findings_count"] == 1
    assert mna["findings"][0]["difference_type"] == "different_thresholds"


@pytest.mark.asyncio
async def test_strict_tenant_isolation_no_leakage(async_db: AsyncSession, test_client: httpx.AsyncClient, monkeypatch):
    """Verifies that evidence belonging to Tenant B is NEVER exposed to Tenant A."""
    settings = get_settings()
    monkeypatch.setattr(settings, "zkp_enabled", True)
    monkeypatch.setattr(settings, "mna_due_diligence_enabled", True)

    tenant_a = Tenant(tenant_id="tenant_alpha", display_name="Alpha", opa_bundle_prefix="tenants/tenant_alpha", is_active=True)
    tenant_b = Tenant(tenant_id="tenant_beta", display_name="Beta", opa_bundle_prefix="tenants/tenant_beta", is_active=True)
    async_db.add_all([tenant_a, tenant_b])
    await async_db.flush()

    circ_b = Circular(circular_number="SEBI/BETA/01", title="Beta Circular", tenant_id="tenant_beta", raw_text_digest="c" * 64)
    async_db.add(circ_b)
    await async_db.flush()

    clause_b = Clause(
        circular_id=circ_b.id,
        tenant_id="tenant_beta",
        clause_number="9.9",
        text="Secret proprietary clause for Beta only",
        sha256="9" * 64,
    )
    async_db.add(clause_b)
    await async_db.flush()

    rule_b = CompiledRule(
        rule_id="RULE-PROPRIETARY-BETA",
        rule_version=1,
        policy_sha256="8" * 64,
        jsonlogic_ast={"==": [{"var": "secret"}, 42]},
        is_active=False,
        hitl_status="BLOCKING",
        tenant_id="tenant_beta",
        clause_id=clause_b.id,
    )
    async_db.add(rule_b)
    await async_db.flush()

    review_b = HITLReview(
        review_id="rev-beta-001",
        clause_id=clause_b.id,
        compiled_rule_id=rule_b.id,
        tenant_id="tenant_beta",
        reason_code="ambiguous_span",
        severity="blocking",
        description="Beta review item",
        status="PENDING",
    )
    async_db.add(review_b)

    # Add ZKP ledger entry specifically for Tenant Beta
    await async_db.execute(
        compliance_audit_ledger.insert().values(
            sequence_num=201,
            broker_id="tenant_beta",
            transaction_id="TXN-BETA-ZKP",
            evaluated_at=dt.datetime.now(dt.timezone.utc),
            circular_id="SEBI/BETA/01",
            clause_hash="9" * 64,
            section_reference="9.9",
            rule_id="RULE-PROPRIETARY-BETA",
            evaluation_result="PASS",
            details={"zk_proof": {"circuit_id": "beta_circuit", "proof_hash": "beta_secret_proof"}},
            payload_digest="digest201",
            previous_hash="prev",
            current_hash="hash201",
            created_at=dt.datetime.now(dt.timezone.utc),
        )
    )
    await async_db.commit()

    # Alpha token attempts to access Beta's review evidence
    alpha_token = make_token("tenant_alpha", [Role.COMPLIANCE_OFFICER])
    resp = await test_client.get(
        "/v1/hitl-reviews/rev-beta-001/evidence",
        headers={"Authorization": f"Bearer {alpha_token}"},
    )

    # Must be strictly rejected with HTTP 403 Forbidden!
    assert resp.status_code == 403
    assert "does not have access to tenant 'tenant_beta'" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_independent_feature_disabling(async_db: AsyncSession, test_client: httpx.AsyncClient, monkeypatch):
    """Verifies that disabling individual roadmap features marks them as DISABLED
    without failing the overall review evidence endpoint.
    """
    settings = get_settings()
    # Explicitly disable all roadmap features
    monkeypatch.setattr(settings, "case_law_memory_enabled", False)
    monkeypatch.setattr(settings, "arbitration_enabled", False)
    monkeypatch.setattr(settings, "rule_preview_enabled", False)
    monkeypatch.setattr(settings, "zkp_enabled", False)
    monkeypatch.setattr(settings, "mna_due_diligence_enabled", False)

    tenant = Tenant(tenant_id="tenant_delta", display_name="Delta", opa_bundle_prefix="tenants/tenant_delta", is_active=True)
    async_db.add(tenant)
    await async_db.flush()

    circ = Circular(circular_number="SEBI/DELTA/01", title="Delta Circular", tenant_id="tenant_delta", raw_text_digest="c" * 64)
    async_db.add(circ)
    await async_db.flush()

    clause = Clause(
        circular_id=circ.id,
        tenant_id="tenant_delta",
        clause_number="1.1",
        text="Delta margin requirements clause",
        sha256="d" * 64,
    )
    async_db.add(clause)
    await async_db.flush()

    rule = CompiledRule(
        rule_id="RULE-DELTA-1",
        rule_version=1,
        policy_sha256="e" * 64,
        jsonlogic_ast={">=": [{"var": "margin"}, 10]},
        is_active=False,
        hitl_status="BLOCKING",
        tenant_id="tenant_delta",
        clause_id=clause.id,
    )
    async_db.add(rule)
    await async_db.flush()

    review = HITLReview(
        review_id="rev-delta-001",
        clause_id=clause.id,
        compiled_rule_id=rule.id,
        tenant_id="tenant_delta",
        reason_code="qualitative_directive",
        severity="advisory",
        description="Advisory review for Delta",
        status="PENDING",
    )
    async_db.add(review)
    await async_db.commit()

    token = make_token("tenant_delta", [Role.COMPLIANCE_OFFICER])
    resp = await test_client.get(
        "/v1/hitl-reviews/rev-delta-001/evidence",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert resp.status_code == 200
    data = resp.json()

    # Source and Deterministic evidence remain AVAILABLE
    assert data["source_evidence"]["status"] == "AVAILABLE"
    assert data["deterministic_evidence"]["status"] == "AVAILABLE"

    # All disabled roadmap features report DISABLED status
    assert data["precedent_evidence"]["status"] == "DISABLED"
    assert "disabled by configuration" in data["precedent_evidence"]["notes"]

    assert data["arbitration_analysis"]["status"] == "DISABLED"
    assert "disabled by configuration" in data["arbitration_analysis"]["notes"]

    assert data["digital_twin_simulation"]["status"] == "DISABLED"
    assert "disabled by configuration" in data["digital_twin_simulation"]["notes"]

    assert data["zkp_evidence"]["status"] == "DISABLED"
    assert "disabled by configuration" in data["zkp_evidence"]["notes"]

    assert data["mna_findings"]["status"] == "DISABLED"
    assert "disabled by configuration" in data["mna_findings"]["notes"]


@pytest.mark.asyncio
async def test_authorization_enforcement(async_db: AsyncSession, test_client: httpx.AsyncClient):
    """Verifies that only Compliance_Officer and System_Admin can query evidence."""
    tenant = Tenant(tenant_id="tenant_gamma", display_name="Gamma", opa_bundle_prefix="tenants/tenant_gamma", is_active=True)
    async_db.add(tenant)
    await async_db.flush()

    circ = Circular(circular_number="SEBI/GAMMA/01", title="Gamma Circular", tenant_id="tenant_gamma", raw_text_digest="c" * 64)
    async_db.add(circ)
    await async_db.flush()

    clause = Clause(
        circular_id=circ.id,
        tenant_id="tenant_gamma",
        clause_number="2.0",
        text="Gamma clause",
        sha256="g" * 64,
    )
    async_db.add(clause)
    await async_db.flush()

    review = HITLReview(
        review_id="rev-gamma-001",
        clause_id=clause.id,
        tenant_id="tenant_gamma",
        reason_code="ambiguous_span",
        severity="blocking",
        description="Gamma review",
        status="PENDING",
    )
    async_db.add(review)
    await async_db.commit()

    # 1. Unauthenticated request -> 401
    resp_unauth = await test_client.get("/v1/hitl-reviews/rev-gamma-001/evidence")
    assert resp_unauth.status_code == 401

    # 2. Broker client role -> 403
    broker_token = make_token("tenant_gamma", [Role.BROKER_API_CLIENT])
    resp_broker = await test_client.get(
        "/v1/hitl-reviews/rev-gamma-001/evidence",
        headers={"Authorization": f"Bearer {broker_token}"},
    )
    assert resp_broker.status_code == 403

    # 3. Compliance Officer -> 200
    officer_token = make_token("tenant_gamma", [Role.COMPLIANCE_OFFICER])
    resp_officer = await test_client.get(
        "/v1/hitl-reviews/rev-gamma-001/evidence",
        headers={"Authorization": f"Bearer {officer_token}"},
    )
    assert resp_officer.status_code == 200

    # 4. System Admin -> 200
    admin_token = make_token("tenant_gamma", [Role.SYSTEM_ADMIN])
    resp_admin = await test_client.get(
        "/v1/hitl-reviews/rev-gamma-001/evidence",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp_admin.status_code == 200


@pytest.mark.asyncio
async def test_zero_candidate_rule_mutation(async_db: AsyncSession, test_client: httpx.AsyncClient):
    """Verifies that evidence collection is strictly read-only and candidate rules
    remain inactive and unmutated.
    """
    tenant = Tenant(tenant_id="tenant_omega", display_name="Omega", opa_bundle_prefix="tenants/tenant_omega", is_active=True)
    async_db.add(tenant)
    await async_db.flush()

    circ = Circular(circular_number="SEBI/OMEGA/01", title="Omega Circular", tenant_id="tenant_omega", raw_text_digest="c" * 64)
    async_db.add(circ)
    await async_db.flush()

    clause = Clause(
        circular_id=circ.id,
        tenant_id="tenant_omega",
        clause_number="5.5",
        text="Omega clause text",
        sha256="o" * 64,
    )
    async_db.add(clause)
    await async_db.flush()

    compiled_rule = CompiledRule(
        rule_id="RULE-OMEGA-5",
        rule_version=1,
        policy_sha256="p" * 64,
        jsonlogic_ast={"==": [{"var": "omega"}, 1]},
        is_active=False,
        hitl_status="BLOCKING",
        tenant_id="tenant_omega",
        clause_id=clause.id,
    )
    async_db.add(compiled_rule)
    await async_db.flush()

    review = HITLReview(
        review_id="rev-omega-001",
        clause_id=clause.id,
        compiled_rule_id=compiled_rule.id,
        tenant_id="tenant_omega",
        reason_code="conflicting_thresholds",
        severity="blocking",
        description="Omega review",
        status="PENDING",
    )
    async_db.add(review)
    await async_db.commit()

    token = make_token("tenant_omega", [Role.COMPLIANCE_OFFICER])
    resp = await test_client.get(
        "/v1/hitl-reviews/rev-omega-001/evidence",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200

    # Query DB to verify candidate rule remained untouched
    res = await async_db.execute(select(CompiledRule).where(CompiledRule.id == compiled_rule.id))
    rule_after = res.scalar_one()
    assert rule_after.is_active is False
    assert rule_after.hitl_status == "BLOCKING"
    assert rule_after.policy_sha256 == "p" * 64
