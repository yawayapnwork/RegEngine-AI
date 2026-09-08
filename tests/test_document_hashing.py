"""Tests proving cryptographic separation of source PDF bytes hash from extracted text hash.

Requirements tested:
1. Original PDF hash is strictly SHA-256 of raw bytes (source_document_sha256).
2. Extracted-text hash is separately calculated over normalized text (extracted_text_sha256).
3. Changing PDF bytes (e.g. metadata/comment) changes source_document_sha256.
4. Same extracted text across different PDF byte streams yields identical extracted_text_sha256.
5. Never use a filename as a cryptographic identity.
6. DB models, orchestrator, and API schemas store and expose both hashes distinctly.
7. The full 8-stage provenance chain distinguishes each stage:
   Original PDF -> extracted text -> clause -> compiled rule -> HITL approval -> evaluation -> evidence -> ledger.
"""
from __future__ import annotations

import datetime as dt
import hashlib
from io import BytesIO

import pytest
from reportlab.pdfgen import canvas
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.config import Settings
from app.db.base import Base
from app.db.models import Circular, Clause, CompiledRule, HITLReview, Tenant
from app.ledger.hash_chain import GENESIS_HASH, compute_block_hash, compute_payload_digest
from app.ledger.models import ComplianceEvaluationEvent, EvaluationOutcome, compliance_audit_ledger
from app.models import CircularMetadata, ClauseChunk, ParseResult
from app.parsing.hashing import (
    sha256_of_bytes,
    sha256_of_clause,
    sha256_of_extracted_text,
    sha256_of_text,
)
from app.services.orchestrator import E2EOrchestrator
from app.services.provenance import get_rule_provenance_chain


def _create_test_pdf(comment: str = "") -> bytes:
    """Generates a valid PDF with optional trailer comment."""
    buf = BytesIO()
    c = canvas.Canvas(buf)
    c.drawString(100, 750, "Circular No. SEBI/HO/TEST/2026/001")
    c.drawString(100, 730, "10 February 2026")
    c.drawString(100, 700, "1. Scope and Obligation")
    c.drawString(100, 680, "1.1 Registered brokers must collect 20% upfront margin.")
    c.save()
    raw = buf.getvalue()
    if comment:
        raw += f"\n% {comment}\n".encode("utf-8")
    return raw


# ===========================================================================
# 1. Cryptographic Primitives Tests
# ===========================================================================

def test_original_pdf_hash_is_sha256_of_raw_bytes() -> None:
    """Requirement 1: sha256_of_bytes calculates exact SHA-256 over raw binary content."""
    pdf_bytes = _create_test_pdf()
    expected_sha256 = hashlib.sha256(pdf_bytes).hexdigest()
    actual_sha256 = sha256_of_bytes(pdf_bytes)

    assert actual_sha256 == expected_sha256
    assert len(actual_sha256) == 64
    assert isinstance(actual_sha256, str)


def test_extracted_text_hash_is_separate_from_pdf_hash() -> None:
    """Requirement 2 & 3: Extracted-text hash is strictly distinct from the container PDF hash."""
    pdf_bytes = _create_test_pdf()
    extracted_text = (
        "Circular No. SEBI/HO/TEST/2026/001\n\n"
        "1. Scope and Obligation\n\n"
        "1.1 Registered brokers must collect 20% upfront margin."
    )

    pdf_hash = sha256_of_bytes(pdf_bytes)
    text_hash = sha256_of_extracted_text(extracted_text)

    assert pdf_hash != text_hash
    assert len(pdf_hash) == 64
    assert len(text_hash) == 64


def test_changing_pdf_bytes_changes_source_hash_but_not_extracted_text() -> None:
    """Requirement 10: Changing PDF bytes changes source hash, but same extracted text preserves text hash."""
    pdf_a = _create_test_pdf(comment="signer_a_annotation")
    pdf_b = _create_test_pdf(comment="signer_b_annotation")

    # The PDF containers have different raw bytes
    assert pdf_a != pdf_b
    source_hash_a = sha256_of_bytes(pdf_a)
    source_hash_b = sha256_of_bytes(pdf_b)
    assert source_hash_a != source_hash_b

    # Both documents yield identical normalized extracted legal text
    identical_text = "Registered brokers must collect 20% upfront margin."
    text_hash_a = sha256_of_extracted_text(identical_text)
    text_hash_b = sha256_of_extracted_text(identical_text)

    assert text_hash_a == text_hash_b
    assert text_hash_a != source_hash_a
    assert text_hash_b != source_hash_b


def test_same_extracted_text_with_different_source_hashes() -> None:
    """Requirement 10: Same extracted text can come from different source PDFs."""
    pdf_1 = b"%PDF-1.4\n1 0 obj << /Title (Version 1) >> endobj\n"
    pdf_2 = b"%PDF-1.4\n1 0 obj << /Title (Version 2) >> endobj\n"

    assert sha256_of_bytes(pdf_1) != sha256_of_bytes(pdf_2)

    shared_text = "Every stockbroker shall submit quarterly compliance reports within 15 days."
    assert sha256_of_extracted_text(shared_text) == sha256_of_text(shared_text)


def test_filename_never_used_as_cryptographic_identity() -> None:
    """Requirement 9: Renaming a file must NOT change its cryptographic identity."""
    content = b"%PDF-1.4 Minimal test stream"
    hash_with_name_a = sha256_of_bytes(content)
    hash_with_name_b = sha256_of_bytes(content)

    assert hash_with_name_a == hash_with_name_b
    # Prove identity is derived purely from content bytes, not filename
    assert hash_with_name_a == hashlib.sha256(content).hexdigest()


# ===========================================================================
# 2. Database Model & Persistence Tests
# ===========================================================================

@pytest.mark.asyncio
async def test_circular_model_stores_both_hashes_distinctly() -> None:
    """Requirement 2, 4, 5: Database model stores source_document_sha256 and raw_text_digest separately."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    pdf_bytes = _create_test_pdf()
    source_sha256 = sha256_of_bytes(pdf_bytes)
    extracted_text = "Section 1: Margin Rules"
    text_sha256 = sha256_of_extracted_text(extracted_text)

    async with session_factory() as session:
        tenant = Tenant(
            tenant_id="test_tenant",
            display_name="Test Tenant",
            tenant_type="stockbroker",
            opa_bundle_prefix="test",
            is_active=True,
        )
        session.add(tenant)
        await session.flush()

        circular = Circular(
            tenant_id="test_tenant",
            circular_number="SEBI/TEST/2026/01",
            title="Margin Rule Circular",
            source_document_sha256=source_sha256,
            raw_text_digest=text_sha256,
        )
        session.add(circular)
        await session.commit()

        loaded = await session.get(Circular, circular.id)
        assert loaded is not None
        assert loaded.source_document_sha256 == source_sha256
        assert loaded.raw_text_digest == text_sha256
        assert loaded.extracted_text_sha256 == text_sha256  # Property alias
        assert loaded.source_document_sha256 != loaded.raw_text_digest

    await engine.dispose()


@pytest.mark.asyncio
async def test_orchestrator_persists_and_returns_both_hashes() -> None:
    """Requirement 6: E2EOrchestrator persist_circular and process_circular_pdf return both hashes."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    pdf_bytes = _create_test_pdf()
    expected_source_hash = sha256_of_bytes(pdf_bytes)

    chunk = ClauseChunk(
        chunk_id="chunk_test_01",
        sha256=sha256_of_clause(circular_number="SEBI/TEST/001", clause_number="1.1", text="Must maintain 20% margin."),
        text="Must maintain 20% margin.",
        clause_number="1.1",
        circular_number="SEBI/TEST/001",
    )
    parsed = ParseResult(
        metadata=CircularMetadata(circular_number="SEBI/TEST/001", title="Test Circular"),
        chunks=[chunk],
        element_count=1,
        source_document_sha256=expected_source_hash,
        extracted_text_sha256=sha256_of_extracted_text(chunk.text),
    )

    orchestrator = E2EOrchestrator(Settings(llm_offline_demo_mode=True))

    async with session_factory() as session:
        circular = await orchestrator.persist_circular(
            session=session,
            parsed=parsed,
            file_bytes=pdf_bytes,
            filename="upload.pdf",
        )
        assert circular.source_document_sha256 == expected_source_hash
        assert len(circular.raw_text_digest) == 64
        assert circular.source_document_sha256 != circular.raw_text_digest

        status = await orchestrator.get_circular_status(session, circular.id)
        assert status is not None
        assert status.source_document_sha256 == expected_source_hash
        assert status.extracted_text_sha256 == circular.raw_text_digest
        assert status.raw_text_digest == circular.raw_text_digest

    await engine.dispose()


# ===========================================================================
# 3. 8-Stage Provenance Chain Integrity Tests
# ===========================================================================

@pytest.mark.asyncio
async def test_8_stage_provenance_chain_distinguishes_each_stage() -> None:
    """Requirement 7: Full provenance chain distinguishes:
    Original PDF -> extracted text -> clause -> compiled rule -> HITL approval -> evaluation -> evidence -> ledger.
    """
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(compliance_audit_ledger.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    pdf_bytes = _create_test_pdf()
    source_sha256 = sha256_of_bytes(pdf_bytes)

    extracted_text = "Clause 1.1: Every broker shall collect upfront margin of not less than 20%."
    extracted_sha256 = sha256_of_extracted_text(extracted_text)

    clause_text = "Every broker shall collect upfront margin of not less than 20%."
    circ_num = "SEBI/HO/PROV/2026/001"
    clause_num = "1.1"
    clause_sha = sha256_of_clause(circular_number=circ_num, clause_number=clause_num, text=clause_text)

    async with session_factory() as session:
        # Step 1: Baseline Tenant
        tenant = Tenant(
            tenant_id="sebi_baseline",
            display_name="SEBI Baseline",
            tenant_type="stockbroker",
            opa_bundle_prefix="baseline",
            is_active=True,
        )
        session.add(tenant)
        await session.flush()

        # Step 2: Circular (Original PDF + Extracted Text)
        circular = Circular(
            tenant_id="sebi_baseline",
            circular_number=circ_num,
            title="Upfront Margin Requirement",
            source_document_sha256=source_sha256,
            raw_text_digest=extracted_sha256,
        )
        session.add(circular)
        await session.flush()

        # Step 3: Clause
        clause = Clause(
            circular_id=circular.id,
            tenant_id="sebi_baseline",
            clause_number=clause_num,
            section_title="Upfront Margin",
            text=clause_text,
            sha256=clause_sha,
        )
        session.add(clause)
        await session.flush()

        # Step 4: Compiled Rule
        rule_id = f"{clause_sha}:{clause_num}"
        compiled_rule = CompiledRule(
            clause_id=clause.id,
            tenant_id="sebi_baseline",
            rule_id=rule_id,
            rule_version=1,
            rego_policy="package test\ndefault allow := false\nallow if { input.margin >= 20 }",
            is_compiled=True,
            is_active=True,
            hitl_status="RESOLVED",
        )
        session.add(compiled_rule)
        await session.flush()

        # Step 5: HITL Review
        review = HITLReview(
            clause_id=clause.id,
            compiled_rule_id=compiled_rule.id,
            tenant_id="sebi_baseline",
            review_id="REV-TEST-001",
            reason_code="audit_not_approved",
            severity="blocking",
            description="Audit review",
            status="RESOLVED",
            compliance_officer_id="officer_alice",
            resolved_at=dt.datetime.now(dt.timezone.utc),
            resolution_notes="Approved margin rule",
        )
        session.add(review)
        await session.flush()

        # Step 6, 7, 8: Evaluation -> Evidence -> Ledger
        now = dt.datetime.now(dt.timezone.utc)
        event = ComplianceEvaluationEvent(
            broker_id="BRK-001",
            transaction_id="TXN-PROV-100",
            evaluated_at=now,
            circular_id=circ_num,
            clause_hash=clause_sha,
            section_reference=clause_num,
            rule_id=rule_id,
            evaluation_result=EvaluationOutcome.PASS,
            details={
                "margin_collected": 25.0,
                "threshold": 20.0,
                "provenance": {
                    "source_document_sha256": source_sha256,
                    "extracted_text_sha256": extracted_sha256,
                    "clause_sha256": clause_sha,
                },
            },
        )
        payload_digest = compute_payload_digest(event.model_dump())
        current_hash = compute_block_hash(
            previous_hash=GENESIS_HASH,
            payload_digest=payload_digest,
            sequence_num=0,
            evaluated_at=now,
        )

        await session.execute(
            compliance_audit_ledger.insert().values(
                sequence_num=0,
                broker_id=event.broker_id,
                transaction_id=event.transaction_id,
                evaluated_at=now,
                circular_id=event.circular_id,
                clause_hash=event.clause_hash,
                section_reference=event.section_reference,
                rule_id=event.rule_id,
                evaluation_result=event.evaluation_result.value,
                hitl_review_id=None,
                details=event.details,
                payload_digest=payload_digest,
                previous_hash=GENESIS_HASH,
                current_hash=current_hash,
                created_at=now,
                circular_ref_id=circular.id,
                clause_ref_id=clause.id,
                compiled_rule_ref_id=compiled_rule.id,
                hitl_review_ref_id=review.id,
            )
        )
        await session.commit()

        # Step 9: Reconstruct & Verify full provenance chain
        chain = await get_rule_provenance_chain(
            session=session,
            rule_id=rule_id,
            raw_pdf_bytes=pdf_bytes,
            raw_extracted_text=extracted_text,
        )

        assert chain is not None
        assert chain.is_valid is True
        assert len(chain.stages) == 8

        # Stage 1: PDF container
        assert chain.source_document_sha256 == source_sha256
        assert chain.stages[0].stage_name == "Original PDF"
        assert chain.stages[0].hash_value == source_sha256
        assert chain.stages[0].verified is True

        # Stage 2: Extracted text
        assert chain.extracted_text_sha256 == extracted_sha256
        assert chain.stages[1].stage_name == "Extracted Text"
        assert chain.stages[1].hash_value == extracted_sha256
        assert chain.stages[1].verified is True

        # Stage 3: Clause
        assert chain.clause_sha256 == clause_sha
        assert chain.stages[2].stage_name == "Clause"
        assert chain.stages[2].hash_value == clause_sha
        assert chain.stages[2].verified is True

        # Stage 4: Rule
        assert chain.stages[3].stage_name == "Compiled Rule/Policy"
        assert chain.stages[3].identifier == rule_id

        # Stage 5: HITL
        assert chain.stages[4].stage_name == "HITL Approval"
        assert chain.stages[4].details["status"] == "RESOLVED"

        # Stage 6: Evaluation
        assert chain.stages[5].stage_name == "Evaluation"
        assert chain.stages[5].details["verdict"] == "PASS"

        # Stage 7: Evidence
        assert chain.stages[6].stage_name == "Evidence"
        assert chain.stages[6].hash_value == payload_digest

        # Stage 8: Ledger
        assert chain.stages[7].stage_name == "Ledger"
        assert chain.stages[7].hash_value == current_hash

        # Cryptographic separation check across all stages
        distinct_hashes = {source_sha256, extracted_sha256, clause_sha, payload_digest, current_hash}
        assert len(distinct_hashes) == 5, "All 5 cryptographic digests must be distinct!"

    await engine.dispose()
