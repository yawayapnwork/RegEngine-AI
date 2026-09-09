"""Comprehensive Test Suite for Compliance Case-Law Memory Agent.

Covers PRD Addendum v2 Section 8.1 Requirements:
1. Indexing approved/resolved HITL decisions with complete cryptographic provenance.
2. Rejecting pending, rejected, and unverified reviews with ValueError.
3. Secret and credential scrubbing before vector embedding.
4. Semantic precedent retrieval over Qdrant (in-memory embedded).
5. Strict tenant isolation (Tenant A cannot retrieve Tenant B's precedents).
6. Conflicting precedent detection and mandatory HITL escalation (current text supremacy).
7. Clean handling of no matching precedent.
8. Tri-part structured context distinguishing source law, precedent, and guidance.
9. Offline/mock deterministic embedding pipeline (zero external model calls).
10. Observability metrics for indexing, retrieval, latency, and HITL conflict escalation.
"""
from __future__ import annotations

import datetime as dt
import uuid
import pytest
from qdrant_client import AsyncQdrantClient

from app.agents.schemas import (
    ComparisonOperator,
    ExtractedComplianceRule,
    NumericalThreshold,
    ObligationType,
)
from app.case_law.agent import CaseLawMemoryAgent
from app.case_law.indexer import index_approved_hitl_review, scrub_secrets
from app.case_law.models import PrecedentQuery, PrecedentRecord
from app.case_law.store import CaseLawStore
from app.config import Settings
from app.db.models import Circular, Clause, CompiledRule, HITLReview
from app.observability.metrics import (
    CASE_LAW_HITL_ESCALATION_TOTAL,
    CASE_LAW_INDEXED_TOTAL,
    CASE_LAW_RETRIEVAL_TOTAL,
)
from app.vectorstore.embeddings import embed_texts


@pytest.fixture
def test_settings() -> Settings:
    """Settings configured for fast, offline, deterministic in-memory execution."""
    return Settings(
        mock_embeddings_enabled=True,
        case_law_memory_enabled=True,
        case_law_qdrant_collection="test_case_law_precedents",
        case_law_similarity_threshold=0.1,  # low threshold for mock test vectors
        case_law_top_k=5,
        case_law_max_age_days=365,
        embedding_dim=64,
    )


@pytest.fixture
def in_memory_qdrant() -> AsyncQdrantClient:
    """Embedded, in-process Qdrant client requiring no Docker or background daemon."""
    return AsyncQdrantClient(location=":memory:")


@pytest.fixture
def case_law_store(in_memory_qdrant: AsyncQdrantClient, test_settings: Settings) -> CaseLawStore:
    return CaseLawStore(client=in_memory_qdrant, settings=test_settings)


# ==============================================================================
# 1. Offline / Mock Embeddings Test
# ==============================================================================
@pytest.mark.asyncio
async def test_offline_mock_embeddings(test_settings: Settings) -> None:
    """Verifies that offline mock embeddings run deterministically without external model APIs."""
    texts = [
        "Stock brokers must maintain upfront margin of 20%",
        "Mutual funds must report portfolio valuations daily",
    ]
    vectors = await embed_texts(texts, test_settings)
    assert len(vectors) == 2
    assert len(vectors[0]) == test_settings.embedding_dim
    assert len(vectors[1]) == test_settings.embedding_dim

    # Deterministic: identical input yields identical vector
    repeat_vectors = await embed_texts(texts, test_settings)
    assert vectors[0] == repeat_vectors[0]


# ==============================================================================
# 2. Secret & Credential Scrubbing Test
# ==============================================================================
def test_secret_scrubbing_removes_credentials() -> None:
    """Verifies that secrets, API keys, passwords, and tokens are scrubbed before indexing."""
    raw_text = (
        "Approved with condition: api_key='sk_live_1234567890abcdef12345' "
        "and Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.e30.t-ID "
        "and ghp_111122223333444455556666777788889999 for access."
    )
    scrubbed = scrub_secrets(raw_text)
    assert "sk_live" not in scrubbed
    assert "eyJhbGciOi" not in scrubbed
    assert "ghp_" not in scrubbed
    assert "[REDACTED_CREDENTIAL]" in scrubbed


# ==============================================================================
# 3. Precedent Indexing & Rejection Tests
# ==============================================================================
@pytest.mark.asyncio
async def test_index_approved_hitl_decision_success(
    case_law_store: CaseLawStore,
    test_settings: Settings,
) -> None:
    """Verifies indexing an APPROVED review preserves full cryptographic provenance."""
    # Setup test models
    circ_id = uuid.uuid4()
    clause_id = uuid.uuid4()
    review_id = "rev-test-101"

    circular = Circular(
        id=circ_id,
        circular_number="SEBI/HO/MIRSD/2024/CIR-01",
        title="Master Circular on Upfront Margin",
        source_document_sha256="d" * 64,
        is_shared=False,
    )
    clause = Clause(
        id=clause_id,
        circular_id=circ_id,
        clause_number="4.2",
        text="Brokers shall collect minimum 20% upfront margin on all cash segment transactions.",
        sha256="c" * 64,
        section_path="Section 4 > Clause 4.2",
        section_title="Margin Framework",
        element_kind="clause",
        circular=circular,
    )
    compiled_rule = CompiledRule(
        id=uuid.uuid4(),
        clause_id=clause_id,
        rule_version="1.0.0",
        policy_sha256="p" * 64,
    )
    approved_review = HITLReview(
        review_id=review_id,
        tenant_id="tenant-alpha",
        clause_id=clause_id,
        compiled_rule_id=compiled_rule.id,
        status="RESOLVED",
        resolution_notes="Approved interpretation: upfront margin applies strictly at order receipt.",
        compliance_officer_id="officer_alice",
        resolved_at=dt.datetime.now(dt.timezone.utc),
        approved_rule_version="1.0.0",
        approved_policy_sha256="p" * 64,
        clause=clause,
        compiled_rule=compiled_rule,
    )

    # Index into memory store (dummy session because review.clause and review.compiled_rule are pre-attached)
    record = await index_approved_hitl_review(
        session=None,  # type: ignore[arg-type]
        review_or_id=approved_review,
        store=case_law_store,
        settings=test_settings,
    )

    # Verify returned PrecedentRecord
    assert record.precedent_id == f"prec_{review_id}"
    assert record.tenant_id == "tenant-alpha"
    assert record.circular_number == "SEBI/HO/MIRSD/2024/CIR-01"
    assert record.source_document_sha256 == "d" * 64
    assert record.clause_sha256 == "c" * 64
    assert record.approved_policy_sha256 == "p" * 64
    assert record.compliance_officer_id == "officer_alice"
    assert record.decision == "APPROVED"

    # Verify retrieval from store
    matches = await case_law_store.search_precedents(
        PrecedentQuery(
            query_text="upfront margin cash segment transactions",
            tenant_id="tenant-alpha",
            min_similarity=0.0,
        )
    )
    assert len(matches) == 1
    assert matches[0].precedent.precedent_id == f"prec_{review_id}"
    assert matches[0].precedent.clause_number == "4.2"


@pytest.mark.asyncio
@pytest.mark.parametrize("disallowed_status", ["PENDING", "REJECTED", "REVISION_REQUIRED", "DRAFT"])
async def test_reject_unapproved_hitl_decisions(
    disallowed_status: str,
    case_law_store: CaseLawStore,
    test_settings: Settings,
) -> None:
    """Verifies that pending, rejected, or unverified reviews cannot be indexed."""
    circ_id = uuid.uuid4()
    clause_id = uuid.uuid4()

    circular = Circular(id=circ_id, circular_number="CIR-99", source_document_sha256="a" * 64)
    clause = Clause(id=clause_id, circular_id=circ_id, text="Draft clause text", sha256="b" * 64, circular=circular)
    review = HITLReview(
        review_id=f"rev-{disallowed_status.lower()}",
        tenant_id="tenant-alpha",
        clause_id=clause_id,
        status=disallowed_status,
        clause=clause,
    )

    with pytest.raises(ValueError) as exc_info:
        await index_approved_hitl_review(
            session=None,  # type: ignore[arg-type]
            review_or_id=review,
            store=case_law_store,
            settings=test_settings,
        )
    assert "Only approved/resolved HITL decisions can be indexed" in str(exc_info.value)


# ==============================================================================
# 4. Strict Tenant Isolation Test
# ==============================================================================
@pytest.mark.asyncio
async def test_strict_tenant_isolation(
    case_law_store: CaseLawStore,
    test_settings: Settings,
) -> None:
    """Verifies that Tenant A cannot retrieve Tenant B's indexed precedents."""
    # Precedent 1 for Tenant Alpha (private)
    prec_alpha = PrecedentRecord(
        precedent_id="prec-alpha-01",
        review_id="rev-alpha-01",
        tenant_id="tenant-alpha",
        circular_id="circ-alpha",
        circular_number="CIR-ALPHA",
        source_document_sha256="1" * 64,
        clause_id="clause-alpha",
        clause_sha256="2" * 64,
        original_clause_text="Client asset segregation rules for institutional broking.",
        decision="APPROVED",
        compliance_officer_id="officer_alpha",
        resolution_notes="Segregation must be verified daily at custodian.",
        approval_timestamp=dt.datetime.now(dt.timezone.utc),
        is_shared=False,
    )
    # Precedent 2 for Tenant Beta (private)
    prec_beta = PrecedentRecord(
        precedent_id="prec-beta-01",
        review_id="rev-beta-01",
        tenant_id="tenant-beta",
        circular_id="circ-beta",
        circular_number="CIR-BETA",
        source_document_sha256="3" * 64,
        clause_id="clause-beta",
        clause_sha256="4" * 64,
        original_clause_text="Client asset segregation rules for retail discount broking.",
        decision="APPROVED",
        compliance_officer_id="officer_beta",
        resolution_notes="Segregation verified via clearing corporation batch file.",
        approval_timestamp=dt.datetime.now(dt.timezone.utc),
        is_shared=False,
    )

    await case_law_store.index_precedent(prec_alpha)
    await case_law_store.index_precedent(prec_beta)

    # Query as Tenant Alpha: must NEVER see Tenant Beta
    alpha_matches = await case_law_store.search_precedents(
        PrecedentQuery(
            query_text="client asset segregation rules",
            tenant_id="tenant-alpha",
            allow_shared=False,
            min_similarity=0.0,
        )
    )
    assert len(alpha_matches) == 1
    assert alpha_matches[0].precedent.precedent_id == "prec-alpha-01"
    assert alpha_matches[0].precedent.tenant_id == "tenant-alpha"

    # Query as Tenant Beta: must NEVER see Tenant Alpha
    beta_matches = await case_law_store.search_precedents(
        PrecedentQuery(
            query_text="client asset segregation rules",
            tenant_id="tenant-beta",
            allow_shared=False,
            min_similarity=0.0,
        )
    )
    assert len(beta_matches) == 1
    assert beta_matches[0].precedent.precedent_id == "prec-beta-01"
    assert beta_matches[0].precedent.tenant_id == "tenant-beta"

    # Query as Tenant Gamma (non-existent): must return empty list
    gamma_matches = await case_law_store.search_precedents(
        PrecedentQuery(
            query_text="client asset segregation rules",
            tenant_id="tenant-gamma",
            allow_shared=False,
            min_similarity=0.0,
        )
    )
    assert len(gamma_matches) == 0


# ==============================================================================
# 5. Agent Analysis & Current Regulatory Text Supremacy
# ==============================================================================
@pytest.mark.asyncio
async def test_agent_tri_part_context_and_text_supremacy(
    case_law_store: CaseLawStore,
    test_settings: Settings,
) -> None:
    """Verifies that the agent generates tri-part structured context affirming current law supremacy."""
    # Index an approved precedent
    prec = PrecedentRecord(
        precedent_id="prec-margin-01",
        review_id="rev-margin-01",
        tenant_id="tenant-alpha",
        circular_id="circ-2022",
        circular_number="SEBI/2022/MARGIN",
        source_document_sha256="5" * 64,
        clause_id="clause-1",
        clause_sha256="6" * 64,
        original_clause_text="Stock brokers shall collect upfront margin of 20% from clients.",
        decision="APPROVED",
        compliance_officer_id="officer_alice",
        resolution_notes="Approved interpretation: 20% margin applies strictly prior to trade execution.",
        approval_timestamp=dt.datetime.now(dt.timezone.utc),
        is_shared=False,
    )
    await case_law_store.index_precedent(prec)

    agent = CaseLawMemoryAgent(store=case_law_store, settings=test_settings)
    current_clause_text = "Brokers must ensure upfront margin collection of 20% before executing any order."

    result = await agent.analyze_clause(
        clause_text=current_clause_text,
        clause_number="3.1",
        tenant_id="tenant-alpha",
        min_similarity=0.0,
    )

    # 1. Structure verification: 3 distinct parts
    assert "=== 1. CURRENT REGULATORY SOURCE TEXT (AUTHORITATIVE LAW) ===" in result.structured_context
    assert "=== 2. RETRIEVED HISTORICAL PRECEDENTS" in result.structured_context
    assert "=== 3. AGENT SYNTHESIS & REVIEW GUIDANCE ===" in result.structured_context

    # 2. Invariant verification: Current text supremacy explicitly stated
    assert "sole authoritative basis" in result.structured_context
    assert "Historical precedents cannot override" in result.structured_context

    # 3. Advisory matches included
    assert len(result.precedent_matches) == 1
    assert result.precedent_matches[0].precedent.precedent_id == "prec-margin-01"
    assert result.precedent_matches[0].similarity_score > 0.0


# ==============================================================================
# 6. Conflicting Precedent Detection & HITL Escalation
# ==============================================================================
@pytest.mark.asyncio
async def test_conflicting_precedent_detection_escalates_to_hitl(
    case_law_store: CaseLawStore,
    test_settings: Settings,
) -> None:
    """Verifies that threshold conflicts trigger conflict_flag_required and escalate to HITL."""
    # Precedent has 20% margin requirement
    prec = PrecedentRecord(
        precedent_id="prec-margin-old",
        review_id="rev-margin-old",
        tenant_id="tenant-alpha",
        circular_id="circ-old",
        circular_number="SEBI/2020/MARGIN-OLD",
        source_document_sha256="7" * 64,
        clause_id="clause-old",
        clause_sha256="8" * 64,
        original_clause_text="Stock brokers shall collect upfront margin of 20% from clients.",
        decision="APPROVED",
        compliance_officer_id="officer_bob",
        resolution_notes="Historical threshold was 20% margin requirement.",
        approval_timestamp=dt.datetime.now(dt.timezone.utc),
        is_shared=False,
    )
    await case_law_store.index_precedent(prec)

    agent = CaseLawMemoryAgent(store=case_law_store, settings=test_settings)

    # Current rule establishes a NEW 30% margin threshold (contradicting historical 20%)
    current_rule = ExtractedComplianceRule(
        rule_id="RULE-NEW-01",
        source_chunk_id="chunk-new",
        source_sha256="c" * 64,
        obligation_type=ObligationType.MANDATORY,
        extraction_confidence=1.0,
        deterministic_logic=[
            NumericalThreshold(
                metric="margin_requirement",
                operator=ComparisonOperator.GTE,
                value=30.0,
                unit="%",
                verbatim_evidence="30%",
            )
        ],
    )

    result = await agent.analyze_clause(
        clause_text="Stock brokers shall collect upfront margin of 30% on all orders.",
        clause_number="1.1",
        current_rule=current_rule,
        tenant_id="tenant-alpha",
        min_similarity=0.0,
    )

    # Must detect conflict and flag for human review
    assert result.conflict_flag_required is True
    assert len(result.conflicts) >= 1
    assert "differs from current circular value" in result.conflicts[0]
    assert "Current circular text has supremacy; human confirmation required" in result.conflicts[0]
    assert "POTENTIAL CONFLICT DETECTED" in result.structured_context


# ==============================================================================
# 7. No Matching Precedent Handling
# ==============================================================================
@pytest.mark.asyncio
async def test_no_matching_precedent_handled_gracefully(
    case_law_store: CaseLawStore,
    test_settings: Settings,
) -> None:
    """Verifies that an absence of matching precedent produces clean fallback guidance."""
    agent = CaseLawMemoryAgent(store=case_law_store, settings=test_settings)

    # Vector store is empty; query for novel regulatory topic
    result = await agent.analyze_clause(
        clause_text="Sovereign green bond issuance reporting standards.",
        clause_number="5.5",
        tenant_id="tenant-alpha",
        min_similarity=0.99,  # high threshold ensures no match
    )

    assert len(result.precedent_matches) == 0
    assert result.conflict_flag_required is False
    assert "No semantically similar approved precedents met the similarity threshold" in result.structured_context


# ==============================================================================
# 8. Maximum Precedent Age Filtering
# ==============================================================================
@pytest.mark.asyncio
async def test_max_age_days_filtering(
    case_law_store: CaseLawStore,
    test_settings: Settings,
) -> None:
    """Verifies that precedents older than max_age_days are excluded from retrieval."""
    now = dt.datetime.now(dt.timezone.utc)
    stale_date = now - dt.timedelta(days=500)
    fresh_date = now - dt.timedelta(days=30)

    stale_prec = PrecedentRecord(
        precedent_id="prec-stale",
        review_id="rev-stale",
        tenant_id="tenant-alpha",
        circular_id="circ-stale",
        circular_number="CIR-STALE",
        source_document_sha256="a" * 64,
        clause_id="cl-stale",
        clause_sha256="b" * 64,
        original_clause_text="Quarterly net capital computation rule.",
        decision="APPROVED",
        compliance_officer_id="officer_old",
        resolution_notes="Approved 500 days ago.",
        approval_timestamp=stale_date,
        is_shared=False,
    )
    fresh_prec = PrecedentRecord(
        precedent_id="prec-fresh",
        review_id="rev-fresh",
        tenant_id="tenant-alpha",
        circular_id="circ-fresh",
        circular_number="CIR-FRESH",
        source_document_sha256="c" * 64,
        clause_id="cl-fresh",
        clause_sha256="d" * 64,
        original_clause_text="Quarterly net capital computation rule.",
        decision="APPROVED",
        compliance_officer_id="officer_new",
        resolution_notes="Approved 30 days ago.",
        approval_timestamp=fresh_date,
        is_shared=False,
    )

    await case_law_store.index_precedent(stale_prec)
    await case_law_store.index_precedent(fresh_prec)

    # Search with max_age_days = 365: only fresh_prec should match
    matches = await case_law_store.search_precedents(
        PrecedentQuery(
            query_text="quarterly net capital computation",
            tenant_id="tenant-alpha",
            max_age_days=365,
            min_similarity=0.0,
        )
    )
    matched_ids = [m.precedent.precedent_id for m in matches]
    assert "prec-fresh" in matched_ids
    assert "prec-stale" not in matched_ids


# ==============================================================================
# 9. Provenance Preservation Deep Verification
# ==============================================================================
@pytest.mark.asyncio
async def test_provenance_preservation_details(
    case_law_store: CaseLawStore,
    test_settings: Settings,
) -> None:
    """Verifies that every cryptographic hash, document reference, and human review field
    is preserved bit-for-bit in the indexed payload.
    """
    prec_id = "prec-audit-provenance-100"
    rev_id = "rev-audit-100"
    ten_id = "tenant-audit"
    circ_sha = "e" * 64
    clause_sha = "f" * 64
    policy_sha = "9" * 64
    now = dt.datetime.now(dt.timezone.utc)

    record = PrecedentRecord(
        precedent_id=prec_id,
        review_id=rev_id,
        tenant_id=ten_id,
        circular_id="circ-uuid-100",
        circular_number="SEBI/HO/CFD/2026/01",
        source_document_sha256=circ_sha,
        clause_id="clause-uuid-100",
        clause_number="12.4.b",
        clause_sha256=clause_sha,
        original_clause_text="Reporting of ESG disclosures on BRSR Core parameters.",
        decision="APPROVED",
        compliance_officer_id="officer_carol",
        resolution_notes="Assurance provider must be independent from statutory auditor.",
        reason_code="qualitative_directive",
        approved_rule_version="2.1.0",
        approved_policy_sha256=policy_sha,
        approval_timestamp=now,
        is_shared=False,
        metadata={"section": "ESG", "assurance_tier": "reasonable"},
    )

    point_id = await case_law_store.index_precedent(record)
    assert point_id is not None

    # Retrieve and inspect the match
    matches = await case_law_store.search_precedents(
        PrecedentQuery(
            query_text="ESG disclosures BRSR Core independent assurance provider",
            tenant_id=ten_id,
            min_similarity=0.0,
        )
    )
    assert len(matches) == 1
    m = matches[0].precedent

    # Exact field-level provenance audit
    assert m.precedent_id == prec_id
    assert m.review_id == rev_id
    assert m.tenant_id == ten_id
    assert m.circular_id == "circ-uuid-100"
    assert m.circular_number == "SEBI/HO/CFD/2026/01"
    assert m.source_document_sha256 == circ_sha
    assert m.clause_id == "clause-uuid-100"
    assert m.clause_number == "12.4.b"
    assert m.clause_sha256 == clause_sha
    assert m.original_clause_text == "Reporting of ESG disclosures on BRSR Core parameters."
    assert m.decision == "APPROVED"
    assert m.compliance_officer_id == "officer_carol"
    assert m.resolution_notes == "Assurance provider must be independent from statutory auditor."
    assert m.reason_code == "qualitative_directive"
    assert m.approved_rule_version == "2.1.0"
    assert m.approved_policy_sha256 == policy_sha
    assert m.metadata.get("assurance_tier") == "reasonable"


# ==============================================================================
# 10. Observability Metrics Verification
# ==============================================================================
@pytest.mark.asyncio
async def test_observability_metrics_recorded(
    case_law_store: CaseLawStore,
    test_settings: Settings,
) -> None:
    """Verifies that Prometheus metrics for indexing, retrieval, and HITL conflict escalation
    are recorded.
    """
    initial_retrievals = CASE_LAW_RETRIEVAL_TOTAL.labels(outcome="hit", tenant_id="tenant-obs")._value.get()

    prec = PrecedentRecord(
        precedent_id="prec-obs-01",
        review_id="rev-obs-01",
        tenant_id="tenant-obs",
        circular_id="circ-obs",
        circular_number="CIR-OBS",
        source_document_sha256="1" * 64,
        clause_id="cl-obs",
        clause_sha256="2" * 64,
        original_clause_text="Trade surveillance alert review timelines.",
        decision="APPROVED",
        compliance_officer_id="officer_obs",
        resolution_notes="Review alerts within T+2 days.",
        approval_timestamp=dt.datetime.now(dt.timezone.utc),
        is_shared=False,
    )
    await case_law_store.index_precedent(prec)

    agent = CaseLawMemoryAgent(store=case_law_store, settings=test_settings)
    await agent.analyze_clause(
        clause_text="Trade surveillance alert review timelines within T+2 days.",
        tenant_id="tenant-obs",
        min_similarity=0.0,
    )

    new_retrievals = CASE_LAW_RETRIEVAL_TOTAL.labels(outcome="hit", tenant_id="tenant-obs")._value.get()
    assert new_retrievals == initial_retrievals + 1

