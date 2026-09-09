"""Comprehensive test suite for the M&A Compliance Due-Diligence Agent (PRD 8.5).

Covers:
1. same policies
2. conflicting policies
3. missing policy
4. semantic conflict & advisory LLM evidence guard
5. unresolved HITL
6. cross-tenant authorization failure
7. unauthorized entity access
8. snapshot consistency
9. provenance tracking
10. zero mutation guarantee
11. background job tracking & cancellation
12. audit ledger logging & data minimization
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.config import Settings
from app.db.base import Base
from app.db.models import Clause, CompiledRule, HITLReview, Tenant
from app.ledger.models import (
    ComplianceEvaluationEvent,
    EvaluationOutcome,
    compliance_audit_ledger,
    metadata as ledger_metadata,
)
from app.ledger.service import LedgerService
from app.mna_due_diligence.auth import verify_dual_entity_authorization
from app.mna_due_diligence.comparison_engine import (
    call_advisory_llm_comparison,
    run_due_diligence_comparison,
)
from app.mna_due_diligence.models import (
    EntityComplianceSnapshot,
    MNAComparisonRequest,
    MNADifferenceType,
    MNAFindingSeverity,
    MNAJobProgress,
    MNAJobStatus,
)
from app.mna_due_diligence.snapshot import build_entity_snapshot, compute_snapshot_hash
from app.mna_due_diligence.tasks import MNAJobManager, run_mna_job_pipeline
from app.security.models import Principal, Role


@pytest.fixture
def mna_settings() -> Settings:
    return Settings(
        mna_due_diligence_enabled=True,
        mna_job_timeout_seconds=60.0,
        mna_llm_advisory_enabled=True,
    )


@pytest_asyncio.fixture
async def async_db():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        echo=False,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(ledger_metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    # Seed entities
    async with session_factory() as session:
        t_alpha = Tenant(
            tenant_id="stockbroker_alpha",
            display_name="Alpha Securities Ltd",
            tenant_type="stockbroker",
            sebi_reg_number="INZ000000001",
            is_active=True,
            opa_bundle_prefix="tenants/stockbroker_alpha",
            risk_overlay={"margin_multiplier": 1.2, "exposure_cap_cr": 50.0},
        )
        t_beta = Tenant(
            tenant_id="stockbroker_beta",
            display_name="Beta Broking Corp",
            tenant_type="stockbroker",
            sebi_reg_number="INZ000000002",
            is_active=True,
            opa_bundle_prefix="tenants/stockbroker_beta",
            risk_overlay={"margin_multiplier": 1.5, "exposure_cap_cr": 30.0},
        )
        t_gamma = Tenant(
            tenant_id="stockbroker_gamma",
            display_name="Gamma Inactive Corp",
            tenant_type="stockbroker",
            sebi_reg_number="INZ000000003",
            is_active=False,
            opa_bundle_prefix="tenants/stockbroker_gamma",
            risk_overlay={},
        )
        session.add_all([t_alpha, t_beta, t_gamma])

        # Seed shared baseline clause
        c1 = Clause(
            id=1,
            circular_id=1,
            tenant_id="stockbroker_alpha",
            clause_number="3.1",
            text="Every stockbroker shall maintain upfront margin of at least 20 percent.",
            sha256="a" * 64,
            section_path=["Part A", "3", "3.1"],
        )
        c2 = Clause(
            id=2,
            circular_id=1,
            tenant_id="stockbroker_alpha",
            clause_number="3.2",
            text="Leverage cap shall not exceed 5x net capital.",
            sha256="b" * 64,
            section_path=["Part A", "3", "3.2"],
        )
        c3 = Clause(
            id=3,
            circular_id=1,
            tenant_id="stockbroker_alpha",
            clause_number="4.1",
            text="Algorithmic trading systems must undergo biannual independent audits.",
            sha256="c" * 64,
            section_path=["Part B", "4", "4.1"],
        )
        session.add_all([c1, c2, c3])

        # Seed compiled rules
        # 1. Identical Rule (Same policy)
        r_same_a = CompiledRule(
            rule_id="RULE_MARGIN_UPFRONT",
            rule_version=1,
            clause_id=1,
            tenant_id="stockbroker_alpha",
            policy_sha256="hash_margin_identical_001",
            rego_policy="package rules\ndefault allow = false\nallow { input.facts.margin >= 20.0 }",
            jsonlogic_ast={">=": [{"var": "facts.margin"}, 20.0]},
            is_active=True,
            is_compiled=True,
        )
        r_same_b = CompiledRule(
            rule_id="RULE_MARGIN_UPFRONT",
            rule_version=1,
            clause_id=1,
            tenant_id="stockbroker_beta",
            policy_sha256="hash_margin_identical_001",
            rego_policy="package rules\ndefault allow = false\nallow { input.facts.margin >= 20.0 }",
            jsonlogic_ast={">=": [{"var": "facts.margin"}, 20.0]},
            is_active=True,
            is_compiled=True,
        )

        # 2. Conflicting threshold
        r_conf_a = CompiledRule(
            rule_id="RULE_LEVERAGE_CAP",
            rule_version=1,
            clause_id=2,
            tenant_id="stockbroker_alpha",
            policy_sha256="hash_leverage_a_001",
            rego_policy="package rules\ndefault allow = false\nallow { input.facts.leverage <= 5.0 }",
            jsonlogic_ast={"<=": [{"var": "facts.leverage"}, 5.0]},
            is_active=True,
            is_compiled=True,
        )
        r_conf_b = CompiledRule(
            rule_id="RULE_LEVERAGE_CAP",
            rule_version=1,
            clause_id=2,
            tenant_id="stockbroker_beta",
            policy_sha256="hash_leverage_b_001",
            rego_policy="package rules\ndefault allow = false\nallow { input.facts.leverage <= 3.0 }",
            jsonlogic_ast={"<=": [{"var": "facts.leverage"}, 3.0]},
            is_active=True,
            is_compiled=True,
        )

        # 3. Missing policy (in Alpha, missing in Beta)
        r_missing_a = CompiledRule(
            rule_id="RULE_ALGO_AUDIT",
            rule_version=1,
            clause_id=3,
            tenant_id="stockbroker_alpha",
            policy_sha256="hash_algo_a_001",
            rego_policy="package rules\ndefault allow = false\nallow { input.facts.audit_age_months <= 6 }",
            jsonlogic_ast={"<=": [{"var": "facts.audit_age_months"}, 6]},
            is_active=True,
            is_compiled=True,
        )

        # 4. Contradictory operator conflict
        r_op_a = CompiledRule(
            rule_id="RULE_SETTLEMENT_WINDOW",
            rule_version=1,
            clause_id=1,
            tenant_id="stockbroker_alpha",
            policy_sha256="hash_settle_a",
            rego_policy="allow { input.facts.days <= 2.0 }",
            jsonlogic_ast={"<=": [{"var": "facts.days"}, 2.0]},
            is_active=True,
            is_compiled=True,
        )
        r_op_b = CompiledRule(
            rule_id="RULE_SETTLEMENT_WINDOW",
            rule_version=1,
            clause_id=1,
            tenant_id="stockbroker_beta",
            policy_sha256="hash_settle_b",
            rego_policy="allow { input.facts.days >= 3.0 }",
            jsonlogic_ast={">=": [{"var": "facts.days"}, 3.0]},
            is_active=True,
            is_compiled=True,
        )

        session.add_all([r_same_a, r_same_b, r_conf_a, r_conf_b, r_missing_a, r_op_a, r_op_b])

        # 5. Seed open HITL Review in Alpha
        hitl_a = HITLReview(
            review_id="hitl_alpha_001",
            clause_id=2,
            compiled_rule_id=r_conf_a.id,
            tenant_id="stockbroker_alpha",
            reason_code="conflicting_thresholds",
            severity="blocking",
            description="Leverage threshold conflicts with exchange circular guidance",
            source_excerpt="Leverage cap shall not exceed 5x net capital.",
            status="PENDING",
        )
        session.add(hitl_a)
        await session.commit()

        # 6. Seed some historical ledger fails for Beta
        ledger_svc = LedgerService(engine)
        for i in range(3):
            ev = ComplianceEvaluationEvent(
                broker_id="stockbroker_beta",
                transaction_id=f"tx_beta_fail_{i}",
                evaluated_at=dt.datetime.now(dt.timezone.utc),
                circular_id="SEBI_CIR_01",
                clause_hash="b" * 64,
                section_reference="3.2",
                rule_id="RULE_LEVERAGE_CAP",
                evaluation_result=EvaluationOutcome.FAIL,
                details={"violation": "leverage exceeded"},
            )
            await ledger_svc.append_entry(ev)

    yield engine, session_factory
    await engine.dispose()


@pytest.mark.asyncio
async def test_same_policies(async_db, mna_settings):
    """Verifies that two identical entities/policies produce 0 conflicts and 0 critical findings."""
    engine, session_factory = async_db
    async with session_factory() as session:
        # Build snapshot for Alpha and compare with identical self-snapshot
        snap_a = await build_entity_snapshot(session, "stockbroker_alpha")
        # Clone snapshot for identical comparison
        snap_a_clone = snap_a.model_copy(deep=True)

        report = await run_due_diligence_comparison(
            snapshot_a=snap_a,
            snapshot_b=snap_a_clone,
            job_id="job_same_test",
            initiator="officer@sebi.gov.in",
            settings=mna_settings,
            scope=["rules", "thresholds"],
            advisory_mode=False,
        )

        assert report.summary_counts["potential_conflict"] == 0
        assert report.summary_counts["semantic_difference"] == 0
        assert report.summary_counts["missing_policy"] == 0
        assert report.summary_counts["CRITICAL"] == 0
        assert report.overall_risk_score == 0.0


@pytest.mark.asyncio
async def test_conflicting_policies(async_db, mna_settings):
    """Detects numeric threshold divergence and contradictory operator flips."""
    engine, session_factory = async_db
    async with session_factory() as session:
        snap_a = await build_entity_snapshot(session, "stockbroker_alpha")
        snap_b = await build_entity_snapshot(session, "stockbroker_beta")

        report = await run_due_diligence_comparison(
            snapshot_a=snap_a,
            snapshot_b=snap_b,
            job_id="job_conflict_test",
            initiator="officer@sebi.gov.in",
            settings=mna_settings,
            scope=["rules", "thresholds"],
            advisory_mode=False,
        )

        # Should detect operator flip on RULE_SETTLEMENT_WINDOW (<= vs >=)
        conflicts = [f for f in report.findings if f.difference_type == MNADifferenceType.POTENTIAL_CONFLICT]
        assert len(conflicts) >= 1
        op_conflict = next((f for f in conflicts if "RULE_SETTLEMENT_WINDOW" in f.title), None)
        assert op_conflict is not None
        assert op_conflict.severity == MNAFindingSeverity.CRITICAL
        assert not op_conflict.is_advisory

        # Should detect threshold difference on RULE_LEVERAGE_CAP (5.0 vs 3.0)
        sem_diffs = [f for f in report.findings if f.difference_type == MNADifferenceType.SEMANTIC_DIFFERENCE]
        lev_diff = next((f for f in sem_diffs if "RULE_LEVERAGE_CAP" in f.title), None)
        assert lev_diff is not None
        assert lev_diff.entity_a_value == 5.0
        assert lev_diff.entity_b_value == 3.0


@pytest.mark.asyncio
async def test_missing_policy(async_db, mna_settings):
    """Detects a policy present in Entity A but absent in Entity B."""
    engine, session_factory = async_db
    async with session_factory() as session:
        snap_a = await build_entity_snapshot(session, "stockbroker_alpha")
        snap_b = await build_entity_snapshot(session, "stockbroker_beta")

        report = await run_due_diligence_comparison(
            snapshot_a=snap_a,
            snapshot_b=snap_b,
            job_id="job_missing_test",
            initiator="officer@sebi.gov.in",
            settings=mna_settings,
            scope=["rules"],
            advisory_mode=False,
        )

        missing = [f for f in report.findings if f.difference_type == MNADifferenceType.MISSING_POLICY]
        algo_missing = next((f for f in missing if "RULE_ALGO_AUDIT" in f.title), None)
        assert algo_missing is not None
        assert algo_missing.provenance.entity_a_artifact_ref == "rule:RULE_ALGO_AUDIT:v1"
        assert algo_missing.provenance.entity_b_artifact_ref is None


@pytest.mark.asyncio
async def test_semantic_conflict_and_advisory_llm(mna_settings):
    """Tests advisory LLM evaluation and enforces Invariant 11 (no equivalence without evidence)."""
    # 1. Normal advisory call
    fake_json_response = json.dumps({
        "difference_type": "semantic_difference",
        "severity": "HIGH",
        "is_equivalent": False,
        "confidence": 0.88,
        "evidence_quote": "Entity A requires 10 days while Entity B specifies 30 days.",
        "reasoning": "Substantive divergence in reporting timelines.",
        "recommendation": "Adopt the shorter 10-day window.",
    })

    with patch("litellm.acompletion") as mock_llm:
        mock_resp = AsyncMock()
        mock_resp.choices = [AsyncMock(message=AsyncMock(content=fake_json_response))]
        mock_llm.return_value = mock_resp

        diff_type, sev, quote, reasoning, rec, is_equiv = await call_advisory_llm_comparison(
            settings=mna_settings,
            title="Reporting Window",
            text_a="10 days",
            text_b="30 days",
        )
        assert diff_type == MNADifferenceType.SEMANTIC_DIFFERENCE
        assert sev == MNAFindingSeverity.HIGH
        assert not is_equiv
        assert quote == "Entity A requires 10 days while Entity B specifies 30 days."

    # 2. INVARIANT 11 TEST: LLM attempts to claim equivalence without quoting evidence
    fake_unsubstantiated_equivalence = json.dumps({
        "difference_type": "exact_difference",
        "severity": "INFO",
        "is_equivalent": True,
        "confidence": 0.95,
        "evidence_quote": "",  # Empty evidence quote!
        "reasoning": "They seem to mean the same thing in practice.",
        "recommendation": "No action needed.",
    })

    with patch("litellm.acompletion") as mock_llm:
        mock_resp = AsyncMock()
        mock_resp.choices = [AsyncMock(message=AsyncMock(content=fake_unsubstantiated_equivalence))]
        mock_llm.return_value = mock_resp

        diff_type, sev, quote, reasoning, rec, is_equiv = await call_advisory_llm_comparison(
            settings=mna_settings,
            title="Unsubstantiated Policy",
            text_a="Clause text A",
            text_b="Clause text B",
        )
        # MUST BE OVERRULED by the system!
        assert is_equiv is False
        assert diff_type == MNADifferenceType.UNRESOLVED_AMBIGUITY
        assert "[SAFETY OVERRULE]" in reasoning


@pytest.mark.asyncio
async def test_unresolved_hitl(async_db, mna_settings):
    """Surfaces unresolved open HITL reviews as UNRESOLVED_AMBIGUITY."""
    engine, session_factory = async_db
    async with session_factory() as session:
        snap_a = await build_entity_snapshot(session, "stockbroker_alpha")
        snap_b = await build_entity_snapshot(session, "stockbroker_beta")

        report = await run_due_diligence_comparison(
            snapshot_a=snap_a,
            snapshot_b=snap_b,
            job_id="job_hitl_test",
            initiator="officer@sebi.gov.in",
            settings=mna_settings,
            scope=["hitl"],
            advisory_mode=False,
        )

        hitl_findings = [f for f in report.findings if f.difference_type == MNADifferenceType.UNRESOLVED_AMBIGUITY]
        assert len(hitl_findings) >= 1
        finding = hitl_findings[0]
        assert "hitl_alpha_001" in finding.provenance.entity_a_artifact_ref
        assert finding.severity == MNAFindingSeverity.HIGH


@pytest.mark.asyncio
async def test_cross_tenant_authorization_failure(async_db):
    """A single-tenant broker cannot compare against another tenant (Anti-Cross-Tenant Leakage)."""
    engine, session_factory = async_db
    # Principal is a Broker_API_Client scoped only to stockbroker_alpha
    broker_principal = Principal(
        subject="client_alpha_api",
        roles=[Role.BROKER_API_CLIENT],
        tenant_id="stockbroker_alpha",
        token_id="tok_alpha",
    )

    async with session_factory() as session:
        with pytest.raises(HTTPException) as exc_info:
            await verify_dual_entity_authorization(
                principal=broker_principal,
                entity_a_id="stockbroker_alpha",
                entity_b_id="stockbroker_beta",
                db=session,
            )
        assert exc_info.value.status_code == 403
        assert "Cross-entity authorization failed" in exc_info.value.detail


@pytest.mark.asyncio
async def test_unauthorized_entity_access(async_db):
    """Fails closed on missing or inactive entities."""
    engine, session_factory = async_db
    admin_principal = Principal(
        subject="admin_user",
        roles=[Role.SYSTEM_ADMIN],
        token_id="tok_admin",
    )

    async with session_factory() as session:
        # Non-existent entity
        with pytest.raises(HTTPException) as exc_missing:
            await verify_dual_entity_authorization(
                principal=admin_principal,
                entity_a_id="stockbroker_alpha",
                entity_b_id="non_existent_broker",
                db=session,
            )
        assert exc_missing.value.status_code == 404

        # Deactivated entity (stockbroker_gamma)
        with pytest.raises(HTTPException) as exc_inactive:
            await verify_dual_entity_authorization(
                principal=admin_principal,
                entity_a_id="stockbroker_alpha",
                entity_b_id="stockbroker_gamma",
                db=session,
            )
        assert exc_inactive.value.status_code == 400
        assert "deactivated" in exc_inactive.value.detail


@pytest.mark.asyncio
async def test_snapshot_consistency(async_db):
    """Verifies that snapshot hash reflects canonical content and detects mutations."""
    engine, session_factory = async_db
    async with session_factory() as session:
        snap_a = await build_entity_snapshot(session, "stockbroker_alpha")
        assert len(snap_a.snapshot_hash) == 64

        # Re-generating snapshot with identical DB data produces identical hash
        snap_a_repeat = await build_entity_snapshot(session, "stockbroker_alpha")
        assert snap_a.snapshot_hash == snap_a_repeat.snapshot_hash

        # Mutate one field in payload -> hash changes
        mutated_payload = {
            "entity_id": snap_a.entity_id,
            "tenant_type": snap_a.tenant_type,
            "risk_overlay": {"margin_multiplier": 99.9},  # Changed!
            "rules": [],
            "clauses": [],
            "unresolved_hitl": [],
            "violations": {},
            "graph": [],
        }
        mutated_hash = compute_snapshot_hash(mutated_payload)
        assert mutated_hash != snap_a.snapshot_hash


@pytest.mark.asyncio
async def test_provenance_retention(async_db, mna_settings):
    """Verifies that every finding in the report retains full provenance back to source artifacts."""
    engine, session_factory = async_db
    async with session_factory() as session:
        snap_a = await build_entity_snapshot(session, "stockbroker_alpha")
        snap_b = await build_entity_snapshot(session, "stockbroker_beta")

        report = await run_due_diligence_comparison(
            snapshot_a=snap_a,
            snapshot_b=snap_b,
            job_id="job_prov_test",
            initiator="officer@sebi.gov.in",
            settings=mna_settings,
            advisory_mode=False,
        )

        assert len(report.findings) > 0
        for f in report.findings:
            prov = f.provenance
            assert prov is not None
            # Every finding has at least one source artifact reference
            assert prov.entity_a_artifact_ref is not None or prov.entity_b_artifact_ref is not None
            # Must have evidence notes
            assert len(prov.evidence_notes) > 0


@pytest.mark.asyncio
async def test_zero_mutation_guarantee(async_db, mna_settings):
    """Critical safety test: running due-diligence comparison MUST NEVER mutate production DB state."""
    engine, session_factory = async_db

    # 1. Capture snapshot of DB state before comparison
    async with session_factory() as session:
        res = await session.execute(select(CompiledRule).order_by(CompiledRule.id))
        rules_before = res.scalars().all()
        rules_state_before = [
            (r.id, r.rule_id, r.tenant_id, r.rule_version, r.is_active, r.policy_sha256, r.rego_policy)
            for r in rules_before
        ]

        tenants_res = await session.execute(select(Tenant).order_by(Tenant.tenant_id))
        tenants_before = tenants_res.scalars().all()
        tenants_state_before = [(t.tenant_id, t.is_active, dict(t.risk_overlay)) for t in tenants_before]

    # 2. Run full due-diligence pipeline
    request = MNAComparisonRequest(
        entity_a_id="stockbroker_alpha",
        entity_b_id="stockbroker_beta",
        advisory_mode=False,
    )
    report = await run_mna_job_pipeline(
        job_id="job_mutation_check",
        request=request,
        initiator="officer@sebi.gov.in",
        session_factory=session_factory,
        ledger_engine=engine,
        settings=mna_settings,
    )
    assert report is not None

    # 3. Capture and verify DB state after comparison: MUST BE BIT-FOR-BIT IDENTICAL
    async with session_factory() as session:
        res = await session.execute(select(CompiledRule).order_by(CompiledRule.id))
        rules_after = res.scalars().all()
        rules_state_after = [
            (r.id, r.rule_id, r.tenant_id, r.rule_version, r.is_active, r.policy_sha256, r.rego_policy)
            for r in rules_after
        ]

        tenants_res = await session.execute(select(Tenant).order_by(Tenant.tenant_id))
        tenants_after = tenants_res.scalars().all()
        tenants_state_after = [(t.tenant_id, t.is_active, dict(t.risk_overlay)) for t in tenants_after]

    assert rules_state_before == rules_state_after, "FATAL: Compiled rules were mutated during M&A comparison!"
    assert tenants_state_before == tenants_state_after, "FATAL: Tenants were mutated during M&A comparison!"


@pytest.mark.asyncio
async def test_background_job_tracking_and_cancellation(mna_settings):
    """Tests job progress tracking, state persistence, and cancellation."""
    manager = MNAJobManager(mna_settings)
    job_id = "test_job_cancel_123"

    job = MNAJobProgress(
        job_id=job_id,
        entity_a_id="stockbroker_alpha",
        entity_b_id="stockbroker_beta",
        initiator_subject="officer@sebi.gov.in",
        status=MNAJobStatus.SNAPSHOT_ACQUISITION,
        progress_pct=25,
        current_step="Extracting snapshots",
    )
    manager.save_job(job)

    retrieved = manager.get_job(job_id)
    assert retrieved is not None
    assert retrieved.progress_pct == 25
    assert retrieved.status == MNAJobStatus.SNAPSHOT_ACQUISITION

    # Cancel job
    cancelled = manager.cancel_job(job_id)
    assert cancelled is True

    cancelled_job = manager.get_job(job_id)
    assert cancelled_job.status == MNAJobStatus.CANCELLED
    assert cancelled_job.cancelled is True


@pytest.mark.asyncio
async def test_data_minimization_and_audit_logging(async_db, mna_settings):
    """Verifies that audit entry is written to ledger and no raw transaction rows are leaked."""
    engine, session_factory = async_db
    request = MNAComparisonRequest(
        entity_a_id="stockbroker_alpha",
        entity_b_id="stockbroker_beta",
        advisory_mode=False,
    )
    job_id = "job_audit_test_999"

    report = await run_mna_job_pipeline(
        job_id=job_id,
        request=request,
        initiator="officer_lead@sebi.gov.in",
        session_factory=session_factory,
        ledger_engine=engine,
        settings=mna_settings,
    )

    # 1. Verify ledger contains PASS audit entry
    async with engine.connect() as conn:
        stmt = select(compliance_audit_ledger).where(compliance_audit_ledger.c.transaction_id == f"mna_job_{job_id}")
        res = await conn.execute(stmt)
        entry = res.first()
        assert entry is not None
        assert entry.evaluation_result == "PASS"
        assert entry.rule_id == "MNA_DUE_DILIGENCE_AUDIT"
        assert entry.details["initiator_subject"] == "officer_lead@sebi.gov.in"
        assert entry.details["entity_a_id"] == "stockbroker_alpha"
        assert entry.details["entity_b_id"] == "stockbroker_beta"

    # 2. Data Minimization check: no raw transaction payloads or trade amounts in findings
    findings_str = json.dumps([f.model_dump(mode="json") for f in report.findings])
    assert "account_number" not in findings_str
    assert "client_pan" not in findings_str
    assert "order_id" not in findings_str


def test_mna_api_endpoints(mna_settings):
    """Verifies FastAPI endpoints: POST /v1/mna/compare, GET /v1/mna/jobs/{id}, and cancel."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api.mna_routes import router as mna_router
    from app.security.dependencies import get_current_principal
    from app.config import get_settings

    test_app = FastAPI()
    test_app.include_router(mna_router)

    # 1. Test unauthorized Broker_API_Client comparing Alpha vs Beta (HTTP 403)
    broker_principal = Principal(
        subject="broker_client_single",
        roles=[Role.BROKER_API_CLIENT],
        tenant_id="stockbroker_alpha",
        token_id="tok_alpha",
    )
    test_app.dependency_overrides[get_current_principal] = lambda: broker_principal
    test_app.dependency_overrides[get_settings] = lambda: mna_settings

    client = TestClient(test_app)
    try:
        resp = client.post(
            "/v1/mna/compare",
            json={
                "entity_a_id": "stockbroker_alpha",
                "entity_b_id": "stockbroker_beta",
                "advisory_mode": False,
            },
        )
        assert resp.status_code == 403
        assert "Cross-entity authorization failed" in resp.json()["detail"]

        # 2. Test authorized Compliance Officer initiating job retrieval
        officer_principal = Principal(
            subject="officer@sebi.gov.in",
            roles=[Role.COMPLIANCE_OFFICER],
            token_id="tok_officer",
        )
        test_app.dependency_overrides[get_current_principal] = lambda: officer_principal

        manager = MNAJobManager(mna_settings)
        test_job = MNAJobProgress(
            job_id="api_test_job_123",
            entity_a_id="stockbroker_alpha",
            entity_b_id="stockbroker_beta",
            initiator_subject="officer@sebi.gov.in",
            status=MNAJobStatus.QUEUED,
            progress_pct=10,
        )
        manager.save_job(test_job)

        resp_get = client.get("/v1/mna/jobs/api_test_job_123")
        assert resp_get.status_code == 200
        assert resp_get.json()["job_id"] == "api_test_job_123"
        assert resp_get.json()["progress_pct"] == 10

        # Test job cancellation endpoint
        resp_cancel = client.post("/v1/mna/jobs/api_test_job_123/cancel")
        assert resp_cancel.status_code == 200
        assert resp_cancel.json()["status"] == "cancelled"

        # Verify job is now cancelled
        resp_cancelled = client.get("/v1/mna/jobs/api_test_job_123")
        assert resp_cancelled.json()["status"] == "CANCELLED"
    finally:
        test_app.dependency_overrides.clear()

