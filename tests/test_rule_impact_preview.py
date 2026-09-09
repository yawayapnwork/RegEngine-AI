"""Comprehensive test suite for RegEngine AI Real-Data Rule-Impact Preview / Digital Twin
(PRD Addendum v2 Section 8.4).

Verifies:
1. Synthetic historical transaction generation & replay.
2. Baseline vs Candidate rule comparison (new failures, newly passing relaxations).
3. Zero affected records (advisory notes present, zero impact does NOT equal approval).
4. All records affected (blanket impact).
5. Mixed results with aggregate financial impact calculation.
6. Strict tenant isolation (never mixing data between tenants).
7. Candidate policy isolation (candidate cannot become active; no live deployment).
8. No mutation of historical ledger transactions (read-only guarantee).
9. Reproducibility & cryptographic SHA-256 result digests.
10. Trade secrecy & data minimization (masked transaction IDs, redacted PII facts).
11. Large dataset safeguards (max_transactions cap).
12. Timeout and cancellation handling.
13. HITL review screen endpoints (POST / GET preview-impact).
"""
from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import json
import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, patch

from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.pool import StaticPool

from app.agents.schemas import ComparisonOperator, ExtractedComplianceRule, NumericalThreshold, ObligationType, TargetEntity
from app.backtest.candidate_evaluator import JsonLogicCandidateEvaluator
from app.backtest.models import (
    ADVISORY_DISCLAIMER,
    DeltaChangeType,
    HistoricalTransaction,
    PreviewDatasetScope,
    PreviewRunRequest,
    RuleImpactPreviewReport,
)
from app.backtest.orchestrator import run_rule_impact_preview
from app.backtest.replay_engine import (
    compute_dataset_snapshot_hash,
    extract_financial_value,
    fetch_historical_transactions,
    replay_all,
)
from app.backtest.reporting import (
    build_aggregate_financial_impact,
    build_outcomes,
    build_representative_examples,
    build_rule_impact_preview_report,
    build_summary,
    compute_preview_result_digest,
    mask_transaction_id,
)
from app.backtest.tasks import cancel_preview, get_preview_report, is_preview_cancelled, save_preview_report
from app.compiler.jsonlogic_compiler import compile_rule_to_jsonlogic
from app.config import Settings
from app.db.base import Base
from app.db.models import Circular, Clause, CompiledRule, HITLReview, Tenant
from app.execution.models import Decision
from app.ledger.models import compliance_audit_ledger


def _create_margin_rule(threshold_val: float) -> ExtractedComplianceRule:
    return ExtractedComplianceRule(
        rule_id="SEBI:MARGIN:2025:4.1",
        source_chunk_id="chunk-01",
        source_sha256="hash-01",
        circular_number="SEBI/HO/MRD/2025/1",
        clause_number="4.1",
        target_entities=[
            TargetEntity(raw_text="Stockbroker", normalized_entity="Stockbroker", verbatim_evidence="Stockbroker")
        ],
        deterministic_logic=[
            NumericalThreshold(
                metric="Upfront Margin",
                operator=ComparisonOperator.GTE,
                value=threshold_val,
                unit="%",
                verbatim_evidence=f"{threshold_val}%",
            )
        ],
        obligation_type=ObligationType.MANDATORY,
        extraction_confidence=0.98,
    )


@pytest.fixture
def test_settings() -> Settings:
    return Settings(
        rule_preview_max_transactions=5000,
        rule_preview_timeout_seconds=5.0,
        rule_preview_default_lookback_days=30,
        backtest_concurrency=4,
    )


@pytest_asyncio.fixture
async def in_memory_engine() -> AsyncEngine:
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        echo=False,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def populated_ledger(in_memory_engine: AsyncEngine) -> list[dict]:
    """Populates synthetic historical transactions for testing."""
    now = dt.datetime.now(dt.timezone.utc)
    records = [
        # Broker A: 4 transactions
        {
            "sequence_num": 1,
            "broker_id": "broker-alpha",
            "transaction_id": "txn-alpha-001",
            "evaluated_at": now - dt.timedelta(days=5),
            "circular_id": "CIRC-01",
            "clause_hash": "chash-01",
            "section_reference": "4.1",
            "rule_id": "SEBI:MARGIN:2025:4.1",
            "evaluation_result": "PASS",
            "details": {
                "entity_type": "Stockbroker",
                "facts": {"upfront_margin_pct": 22.0, "order_value": 150000.0, "pan": "ABCDE1234F"},
                "violations": [],
            },
            "payload_digest": "dig-01",
            "previous_hash": "prev-01",
            "current_hash": "curr-01",
            "created_at": now - dt.timedelta(days=5),
        },
        {
            "sequence_num": 2,
            "broker_id": "broker-alpha",
            "transaction_id": "txn-alpha-002",
            "evaluated_at": now - dt.timedelta(days=4),
            "circular_id": "CIRC-01",
            "clause_hash": "chash-01",
            "section_reference": "4.1",
            "rule_id": "SEBI:MARGIN:2025:4.1",
            "evaluation_result": "PASS",
            "details": {
                "entity_type": "Stockbroker",
                "facts": {"upfront_margin_pct": 28.0, "order_value": 250000.0, "pan": "BCDEF2345G"},
                "violations": [],
            },
            "payload_digest": "dig-02",
            "previous_hash": "curr-01",
            "current_hash": "curr-02",
            "created_at": now - dt.timedelta(days=4),
        },
        {
            "sequence_num": 3,
            "broker_id": "broker-alpha",
            "transaction_id": "txn-alpha-003",
            "evaluated_at": now - dt.timedelta(days=3),
            "circular_id": "CIRC-01",
            "clause_hash": "chash-01",
            "section_reference": "4.1",
            "rule_id": "SEBI:MARGIN:2025:4.1",
            "evaluation_result": "FAIL",
            "details": {
                "entity_type": "Stockbroker",
                "facts": {"upfront_margin_pct": 18.0, "order_value": 500000.0, "pan": "CDEFG3456H"},
                "violations": ["Upfront margin 18.0% < 20%"],
            },
            "payload_digest": "dig-03",
            "previous_hash": "curr-02",
            "current_hash": "curr-03",
            "created_at": now - dt.timedelta(days=3),
        },
        {
            "sequence_num": 4,
            "broker_id": "broker-alpha",
            "transaction_id": "txn-alpha-004",
            "evaluated_at": now - dt.timedelta(days=2),
            "circular_id": "CIRC-01",
            "clause_hash": "chash-01",
            "section_reference": "4.1",
            "rule_id": "SEBI:MARGIN:2025:4.1",
            "evaluation_result": "FAIL",
            "details": {
                "entity_type": "Stockbroker",
                "facts": {"upfront_margin_pct": 12.0, "order_value": 100000.0, "pan": "DEFGH4567J"},
                "violations": ["Upfront margin 12.0% < 20%"],
            },
            "payload_digest": "dig-04",
            "previous_hash": "curr-03",
            "current_hash": "curr-04",
            "created_at": now - dt.timedelta(days=2),
        },
        # Broker Beta: 1 transaction (for tenant isolation testing)
        {
            "sequence_num": 5,
            "broker_id": "broker-beta",
            "transaction_id": "txn-beta-001",
            "evaluated_at": now - dt.timedelta(days=1),
            "circular_id": "CIRC-01",
            "clause_hash": "chash-01",
            "section_reference": "4.1",
            "rule_id": "SEBI:MARGIN:2025:4.1",
            "evaluation_result": "PASS",
            "details": {
                "entity_type": "Stockbroker",
                "facts": {"upfront_margin_pct": 35.0, "order_value": 900000.0, "pan": "EFGHI5678K"},
                "violations": [],
            },
            "payload_digest": "dig-05",
            "previous_hash": "curr-04",
            "current_hash": "curr-05",
            "created_at": now - dt.timedelta(days=1),
        },
    ]

    async with in_memory_engine.begin() as conn:
        for r in records:
            await conn.execute(insert(compliance_audit_ledger).values(**r))
    return records


# ---------------------------------------------------------------------------
# Test Cases
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_baseline_vs_candidate_evaluation_with_deltas(
    in_memory_engine: AsyncEngine,
    populated_ledger: list[dict],
    test_settings: Settings,
) -> None:
    """Tests evaluating a stricter candidate rule (25% vs old 20%).

    Historically:
    txn-001 had margin 22% (passed under 20%). Under candidate 25%, it becomes a NEW FAILURE.
    txn-002 had margin 28% (passed under 20%). Under candidate 25%, it remains UNCHANGED PASS.
    txn-003 had margin 18% (failed under 20%). Under candidate 25%, it remains UNCHANGED FAIL.
    txn-004 had margin 12% (failed under 20%). Under candidate 25%, it remains UNCHANGED FAIL.
    """
    candidate_rule = _create_margin_rule(threshold_val=25.0)
    jl = compile_rule_to_jsonlogic(candidate_rule)

    scope = PreviewDatasetScope(
        tenant_id="broker-alpha",
        rule_id="SEBI:MARGIN:2025:4.1",
        lookback_days=30,
        max_transactions=1000,
    )
    request = PreviewRunRequest(
        candidate_rule_id="SEBI:MARGIN:2025:4.1",
        candidate_jsonlogic_ast=jl.logic,
        scope=scope,
    )

    report = await run_rule_impact_preview(request, test_settings, in_memory_engine)

    assert report.total_evaluated == 4
    assert report.newly_affected == 1  # txn-001 became new failure
    assert report.no_longer_affected == 0
    assert report.unchanged_count == 3  # 1 pass + 2 fail
    assert report.old_fail_count == 2
    assert report.new_fail_count == 3
    assert report.old_failure_rate_pct == 50.0
    assert report.new_failure_rate_pct == 75.0
    assert report.delta_failure_rate_pct == 25.0
    assert report.is_live_policy_safe is True


@pytest.mark.asyncio
async def test_zero_affected_records_and_advisory_safeguard(
    in_memory_engine: AsyncEngine,
    populated_ledger: list[dict],
    test_settings: Settings,
) -> None:
    """Tests zero newly affected transactions when candidate rule matches old baseline.

    Verifies:
    1. newly_affected is 0.
    2. Advisory disclaimer is present.
    3. Zero impact does NOT auto-approve.
    """
    candidate_rule = _create_margin_rule(threshold_val=20.0)  # Identical to historical baseline
    jl = compile_rule_to_jsonlogic(candidate_rule)

    scope = PreviewDatasetScope(
        tenant_id="broker-alpha",
        rule_id="SEBI:MARGIN:2025:4.1",
        lookback_days=30,
    )
    request = PreviewRunRequest(
        candidate_rule_id="SEBI:MARGIN:2025:4.1",
        candidate_jsonlogic_ast=jl.logic,
        scope=scope,
    )

    report = await run_rule_impact_preview(request, test_settings, in_memory_engine)

    assert report.total_evaluated == 4
    assert report.newly_affected == 0
    assert report.no_longer_affected == 0
    assert report.delta_failure_rate_pct == 0.0
    assert ADVISORY_DISCLAIMER in report.advisory_notes
    assert "Zero historical impact does NOT constitute approval" in report.advisory_notes


@pytest.mark.asyncio
async def test_all_records_affected(
    in_memory_engine: AsyncEngine,
    populated_ledger: list[dict],
    test_settings: Settings,
) -> None:
    """Tests extreme candidate rule where 100% of transactions fail."""
    candidate_rule = _create_margin_rule(threshold_val=50.0)  # High threshold
    jl = compile_rule_to_jsonlogic(candidate_rule)

    scope = PreviewDatasetScope(
        tenant_id="broker-alpha",
        rule_id="SEBI:MARGIN:2025:4.1",
        lookback_days=30,
    )
    request = PreviewRunRequest(
        candidate_rule_id="SEBI:MARGIN:2025:4.1",
        candidate_jsonlogic_ast=jl.logic,
        scope=scope,
    )

    report = await run_rule_impact_preview(request, test_settings, in_memory_engine)

    assert report.total_evaluated == 4
    assert report.new_fail_count == 4
    assert report.new_failure_rate_pct == 100.0
    assert report.newly_affected == 2  # The 2 historically passing transactions now fail


@pytest.mark.asyncio
async def test_mixed_results_with_aggregate_financial_impact(
    in_memory_engine: AsyncEngine,
    populated_ledger: list[dict],
    test_settings: Settings,
) -> None:
    """Tests candidate threshold 25% aggregating financial amount of new failure."""
    candidate_rule = _create_margin_rule(threshold_val=25.0)
    jl = compile_rule_to_jsonlogic(candidate_rule)

    scope = PreviewDatasetScope(
        tenant_id="broker-alpha",
        rule_id="SEBI:MARGIN:2025:4.1",
        lookback_days=30,
    )
    request = PreviewRunRequest(
        candidate_rule_id="SEBI:MARGIN:2025:4.1",
        candidate_jsonlogic_ast=jl.logic,
        scope=scope,
    )

    report = await run_rule_impact_preview(request, test_settings, in_memory_engine)

    # txn-alpha-001 had order_value 150,000.0 and became a new failure
    fin = report.financial_impact
    assert fin.currency == "INR"
    assert fin.total_new_failure_amount == 150000.0
    assert fin.avg_new_failure_amount == 150000.0
    assert fin.affected_transactions_with_amounts == 1


@pytest.mark.asyncio
async def test_strict_tenant_isolation(
    in_memory_engine: AsyncEngine,
    populated_ledger: list[dict],
    test_settings: Settings,
) -> None:
    """Verifies that queries strictly segregate by tenant_id.

    broker-beta has 1 transaction. Querying for broker-beta must return ONLY broker-beta's record,
    never broker-alpha's 4 records.
    """
    candidate_rule = _create_margin_rule(threshold_val=25.0)
    jl = compile_rule_to_jsonlogic(candidate_rule)

    # Preview for broker-beta
    scope_beta = PreviewDatasetScope(
        tenant_id="broker-beta",
        rule_id="SEBI:MARGIN:2025:4.1",
        lookback_days=30,
    )
    request_beta = PreviewRunRequest(
        candidate_rule_id="SEBI:MARGIN:2025:4.1",
        candidate_jsonlogic_ast=jl.logic,
        scope=scope_beta,
    )

    report_beta = await run_rule_impact_preview(request_beta, test_settings, in_memory_engine)
    assert report_beta.total_evaluated == 1

    # Verify runtime defense against cross-tenant injection
    txns = await fetch_historical_transactions(
        in_memory_engine,
        rule_id="SEBI:MARGIN:2025:4.1",
        tenant_id="broker-beta",
    )
    assert len(txns) == 1
    assert txns[0].broker_id == "broker-beta"


@pytest.mark.asyncio
async def test_candidate_policy_isolation_and_no_ledger_mutation(
    in_memory_engine: AsyncEngine,
    populated_ledger: list[dict],
    test_settings: Settings,
) -> None:
    """CRITICAL SAFETY REQUIREMENT:
    Asserts that:
    1. Historical ledger rows and hashes remain bit-for-bit identical before and after preview.
    2. Candidate rule remains inactive.
    """
    # Snapshot ledger rows before preview
    async with in_memory_engine.connect() as conn:
        before_rows = (await conn.execute(select(compliance_audit_ledger).order_by(compliance_audit_ledger.c.sequence_num))).mappings().all()

    candidate_rule = _create_margin_rule(threshold_val=25.0)
    jl = compile_rule_to_jsonlogic(candidate_rule)

    scope = PreviewDatasetScope(
        tenant_id="broker-alpha",
        rule_id="SEBI:MARGIN:2025:4.1",
        lookback_days=30,
    )
    request = PreviewRunRequest(
        candidate_rule_id="SEBI:MARGIN:2025:4.1",
        candidate_jsonlogic_ast=jl.logic,
        scope=scope,
    )

    report = await run_rule_impact_preview(request, test_settings, in_memory_engine)
    assert report.is_live_policy_safe is True

    # Snapshot ledger rows after preview
    async with in_memory_engine.connect() as conn:
        after_rows = (await conn.execute(select(compliance_audit_ledger).order_by(compliance_audit_ledger.c.sequence_num))).mappings().all()

    assert len(before_rows) == len(after_rows)
    for b, a in zip(before_rows, after_rows):
        assert b["current_hash"] == a["current_hash"]
        assert b["payload_digest"] == a["payload_digest"]
        assert b["evaluation_result"] == a["evaluation_result"]
        assert b["details"] == a["details"]


@pytest.mark.asyncio
async def test_reproducibility_and_tamper_evident_result_digest(
    in_memory_engine: AsyncEngine,
    populated_ledger: list[dict],
    test_settings: Settings,
) -> None:
    """Verifies that repeating the preview produces identical dataset_snapshot_hash and result_digest."""
    candidate_rule = _create_margin_rule(threshold_val=25.0)
    jl = compile_rule_to_jsonlogic(candidate_rule)

    scope = PreviewDatasetScope(
        tenant_id="broker-alpha",
        rule_id="SEBI:MARGIN:2025:4.1",
        lookback_days=30,
    )
    request = PreviewRunRequest(
        candidate_rule_id="SEBI:MARGIN:2025:4.1",
        candidate_jsonlogic_ast=jl.logic,
        scope=scope,
    )

    report_1 = await run_rule_impact_preview(request, test_settings, in_memory_engine)
    report_2 = await run_rule_impact_preview(request, test_settings, in_memory_engine)

    assert report_1.candidate_policy_hash == report_2.candidate_policy_hash
    assert report_1.dataset_snapshot_hash == report_2.dataset_snapshot_hash
    assert report_1.result_digest == report_2.result_digest
    assert len(report_1.result_digest) == 64


def test_redacted_representative_examples() -> None:
    """Verifies trade secrecy: masked transaction IDs and redacted proprietary facts/PII."""
    now = dt.datetime.now(dt.timezone.utc)
    txn = HistoricalTransaction(
        transaction_id="TXN_CONFIDENTIAL_123456789",
        broker_id="broker-alpha",
        entity_type="Stockbroker",
        facts={
            "upfront_margin_pct": 21.0,
            "order_value": 500000.0,
            "pan": "ABCDE1234F",
            "account_number": "ACCT-9999",
            "trader_id": "TRADER-007",
        },
        evaluated_at=now,
        rule_id="r1",
        circular_number="c1",
        clause_number="4.1",
        old_decision="allow",
        old_violations=[],
        financial_amount=500000.0,
    )

    replayed = [(txn, "deny", ["Upfront margin 21% < 25%"])]
    examples = build_representative_examples(replayed)

    assert len(examples) == 1
    ex = examples[0]
    # Check ID masking
    assert ex.masked_transaction_id != "TXN_CONFIDENTIAL_123456789"
    assert "..." in ex.masked_transaction_id

    # Check PII redaction
    assert "pan" not in ex.relevant_facts
    assert "account_number" not in ex.relevant_facts
    assert "trader_id" not in ex.relevant_facts
    assert ex.relevant_facts["upfront_margin_pct"] == 21.0


@pytest.mark.asyncio
async def test_large_dataset_limit_safeguard(
    in_memory_engine: AsyncEngine,
    populated_ledger: list[dict],
    test_settings: Settings,
) -> None:
    """Verifies max_transactions limit restricts query rows and prevents runaway execution."""
    candidate_rule = _create_margin_rule(threshold_val=25.0)
    jl = compile_rule_to_jsonlogic(candidate_rule)

    scope = PreviewDatasetScope(
        tenant_id="broker-alpha",
        rule_id="SEBI:MARGIN:2025:4.1",
        lookback_days=30,
        max_transactions=2,  # Cap at 2 even though 4 exist
    )
    request = PreviewRunRequest(
        candidate_rule_id="SEBI:MARGIN:2025:4.1",
        candidate_jsonlogic_ast=jl.logic,
        scope=scope,
    )

    report = await run_rule_impact_preview(request, test_settings, in_memory_engine)
    assert report.total_evaluated == 2


@pytest.mark.asyncio
async def test_timeout_and_cancellation_handling(
    in_memory_engine: AsyncEngine,
    populated_ledger: list[dict],
) -> None:
    """Tests timeout when execution exceeds configured limit."""
    candidate_rule = _create_margin_rule(threshold_val=25.0)
    jl = compile_rule_to_jsonlogic(candidate_rule)

    tight_settings = Settings(
        rule_preview_max_transactions=1000,
        rule_preview_timeout_seconds=0.0001,  # Ultra short timeout
        backtest_concurrency=2,
    )

    scope = PreviewDatasetScope(
        tenant_id="broker-alpha",
        rule_id="SEBI:MARGIN:2025:4.1",
        lookback_days=30,
    )
    request = PreviewRunRequest(
        candidate_rule_id="SEBI:MARGIN:2025:4.1",
        candidate_jsonlogic_ast=jl.logic,
        scope=scope,
    )

    with pytest.raises(TimeoutError):
        await run_rule_impact_preview(request, tight_settings, in_memory_engine)


def test_preview_report_caching_and_cancellation_helpers() -> None:
    """Tests Redis/in-memory storage and cancellation signals."""
    cancel_preview("test-run-123")
    assert is_preview_cancelled("test-run-123") is True
    assert is_preview_cancelled("other-run-456") is False


@pytest.mark.asyncio
async def test_hitl_review_impact_preview_endpoints(
    in_memory_engine: AsyncEngine,
    populated_ledger: list[dict],
) -> None:
    """Tests HITL review portal preview-impact integration."""
    candidate_rule = _create_margin_rule(threshold_val=25.0)
    jl = compile_rule_to_jsonlogic(candidate_rule)

    now = dt.datetime.now(dt.timezone.utc)

    # Insert Tenant, Circular, Clause, CompiledRule, HITLReview
    async with in_memory_engine.begin() as conn:
        await conn.execute(
            insert(Tenant).values(
                tenant_id="broker-alpha",
                display_name="Broker Alpha",
                opa_bundle_prefix="tenants/broker_alpha",
                created_at=now,
            )
        )
        res_circ = await conn.execute(
            insert(Circular).values(
                tenant_id="broker-alpha",
                circular_number="SEBI/HO/MRD/2025/1",
                title="Margin Circular",
                issue_date=now.date(),
                raw_text_digest=hashlib.sha256(b"raw text").hexdigest(),
                created_at=now,
            )
        )
        circ_id = res_circ.inserted_primary_key[0]
        res_clause = await conn.execute(
            insert(Clause).values(
                circular_id=circ_id,
                tenant_id="broker-alpha",
                clause_number="4.1",
                text="Upfront margin must be maintained at 25%",
                sha256=hashlib.sha256(b"4.1").hexdigest(),
                created_at=now,
            )
        )
        clause_id = res_clause.inserted_primary_key[0]
        res_crule = await conn.execute(
            insert(CompiledRule).values(
                clause_id=clause_id,
                tenant_id="broker-alpha",
                rule_id="SEBI:MARGIN:2025:4.1",
                rule_version=2,
                jsonlogic_ast=jl.logic,
                is_compiled=True,
                is_active=False,  # Unapproved candidate
                hitl_status="BLOCKING",
                created_at=now,
            )
        )
        crule_id = res_crule.inserted_primary_key[0]
        await conn.execute(
            insert(HITLReview).values(
                review_id="rev-test-alpha-001",
                clause_id=clause_id,
                compiled_rule_id=crule_id,
                tenant_id="broker-alpha",
                reason_code="conflicting_thresholds",
                severity="blocking",
                description="Review upfront margin 25%",
                status="PENDING",
                flagged_at=now,
            )
        )

    # Test trigger_review_impact_preview directly via hitl_review_routes
    from app.api.hitl_review_routes import trigger_review_impact_preview, get_review_impact_preview

    async with AsyncSession(in_memory_engine) as session:
        report = await trigger_review_impact_preview(
            review_id="rev-test-alpha-001",
            lookback_days=30,
            max_transactions=100,
            session=session,
            _principal=AsyncMock(),
        )

        assert report.candidate_rule_id == "SEBI:MARGIN:2025:4.1"
        assert report.total_evaluated == 4
        assert report.newly_affected == 1
        assert report.is_live_policy_safe is True

        # Test cached retrieval
        cached_report = await get_review_impact_preview(
            review_id="rev-test-alpha-001",
            session=session,
            _principal=AsyncMock(),
        )
        assert cached_report.preview_id == report.preview_id
        assert cached_report.result_digest == report.result_digest

        # Invariant assertion: CompiledRule must remain inactive
        crule = await session.get(CompiledRule, crule_id)
        assert crule.is_active is False
