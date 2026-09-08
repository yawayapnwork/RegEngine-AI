"""Tests for resumable and observable circular processing pipeline.

Verifies:
1. Retry after transient failure: completed clauses are preserved; only failed clauses re-execute.
2. Partial failure handling: per-clause failure tracking and circular failure transition.
3. Duplicate processing idempotency: calling process_circular_pdf multiple times does not duplicate records.
4. Full lifecycle auditability: state transitions are persisted in circular_state_transitions.
5. HITL waiting state: circular remains in AWAITING_HITL until human approval; retries never auto-deploy.
"""
from __future__ import annotations

import asyncio
from collections import defaultdict
import datetime as dt
import hashlib
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.agents.schemas import (
    AuditedComplianceRule,
    AuditFinding,
    AuditVerdict,
    ComparisonOperator,
    ComplianceRuleAudit,
    ExtractedComplianceRule,
    FindingType,
    NumericalThreshold,
    ObligationType,
    Severity,
    TargetEntity,
)
from app.config import Settings
from app.db.base import Base
from app.db.models import Circular, CircularStateTransition, Clause, CompiledRule, HITLReview
from app.models import CircularMetadata, ClauseChunk, ParseResult
from app.services.hitl_service import HITLReviewService
from app.services.orchestrator import (
    ClauseProcessingState,
    E2EOrchestrator,
    ProcessingState,
)


def _make_chunk(i: int, text: str | None = None) -> ClauseChunk:
    chunk_text = text or f"Stockbroker shall maintain upfront margin of not less than {20 + i}% for client trades."
    return ClauseChunk(
        chunk_id=f"chk-{i:03d}",
        sha256=hashlib.sha256(chunk_text.encode("utf-8")).hexdigest(),
        clause_number=f"3.{i}.0",
        section_title=f"Section {i}",
        section_path=["Chapter I", f"Section {i}"],
        text=chunk_text,
        page_start=1,
        page_end=1,
        contains_table=False,
    )


def _make_audited_rule(chunk: ClauseChunk) -> AuditedComplianceRule:
    rule_id = f"RULE-{chunk.clause_number}"
    rule = ExtractedComplianceRule(
        rule_id=rule_id,
        source_chunk_id=chunk.chunk_id,
        source_sha256=chunk.sha256,
        clause_number=chunk.clause_number,
        target_entities=[
            TargetEntity(raw_text="stock broker", normalized_entity="Stockbroker", verbatim_evidence="stock broker")
        ],
        obligation_type=ObligationType.MANDATORY,
        deterministic_logic=[
            NumericalThreshold(
                metric="Upfront Margin",
                operator=ComparisonOperator.GTE,
                value=20.0,
                unit="%",
                applies_to="Stockbroker",
                verbatim_evidence="not less than 20%",
            )
        ],
        qualitative_directives=[],
        ambiguous_spans=[],
        extraction_confidence=0.95,
    )
    audit = ComplianceRuleAudit(
        rule_id=rule_id,
        verdict=AuditVerdict.APPROVED,
        findings=[],
        fidelity_score=0.95,
        verified_quote_count=1,
        unverified_quote_count=0,
    )
    return AuditedComplianceRule(rule=rule, audit=audit, revision_round=0)


@pytest_asyncio.fixture
async def resumable_db_session():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with session_factory() as session:
        # Pre-seed baseline tenant
        orchestrator = E2EOrchestrator()
        await orchestrator.ensure_baseline_tenant(session)
        await session.commit()
        yield session

    await engine.dispose()


@pytest.mark.asyncio
class TestCircularResumability:
    async def test_retry_after_transient_failure_preserves_completed_clauses(self, resumable_db_session):
        """When 1 of 4 clauses fails transiently during first run:
        - First run completes 3 clauses, marks 1 clause failed.
        - Retry call (resume_circular) extracts ONLY the 1 failed clause (skipping the 3 completed ones).
        - Successfully reaches AWAITING_HITL without duplicating records.
        """
        orchestrator = E2EOrchestrator(clause_concurrency=2)
        chunks = [_make_chunk(i) for i in range(4)]
        mock_parsed = ParseResult(
            chunks=chunks,
            element_count=4,
            metadata=CircularMetadata(
                title="Resumable Margin Circular",
                circular_number="SEBI/CIR/2026/RESUME-01",
                issue_date=dt.date(2026, 1, 15),
            ),
            source_document_sha256="a" * 64,
            extracted_text_sha256="b" * 64,
        )
        orchestrator.parse_pdf = AsyncMock(return_value=mock_parsed)

        call_counts: dict[str, int] = defaultdict(int)

        # First run: clause 3.2.0 fails with transient LLM rate limit
        async def _extract_run_1(chunk, siblings):
            call_counts[chunk.clause_number] += 1
            if chunk.clause_number == "3.2.0":
                raise RuntimeError("LLM rate limit exceeded (HTTP 429)")
            return _make_audited_rule(chunk)

        orchestrator.extract_and_audit = _extract_run_1

        res1 = await orchestrator.process_circular_pdf(
            session=resumable_db_session,
            file_bytes=b"pdf-content-resume",
            filename="resumable_circular.pdf",
        )

        assert res1.clause_count == 4
        assert res1.rules_compiled == 3
        assert res1.processing_state == ProcessingState.AWAITING_HITL.value

        # Verify call counts for Run 1: all 4 chunks were attempted once
        assert call_counts["3.0.0"] == 1
        assert call_counts["3.1.0"] == 1
        assert call_counts["3.2.0"] == 1
        assert call_counts["3.3.0"] == 1

        # Check clause status in DB: 3.2.0 is FAILED, others are COMPILED
        clauses = (
            await resumable_db_session.execute(
                select(Clause).where(Clause.circular_id == res1.circular_id).order_by(Clause.id)
            )
        ).scalars().all()
        assert len(clauses) == 4
        assert clauses[0].processing_status == ClauseProcessingState.COMPILED.value
        assert clauses[1].processing_status == ClauseProcessingState.COMPILED.value
        assert clauses[2].processing_status == ClauseProcessingState.FAILED.value
        assert "HTTP 429" in (clauses[2].error_message or "")
        assert clauses[3].processing_status == ClauseProcessingState.COMPILED.value

        # Second run (RETRY): chunk 2 now succeeds
        async def _extract_run_2(chunk, siblings):
            call_counts[chunk.clause_number] += 1
            return _make_audited_rule(chunk)

        orchestrator.extract_and_audit = _extract_run_2

        res2 = await orchestrator.resume_circular(
            session=resumable_db_session,
            circular_id=res1.circular_id,
        )

        assert res2.clause_count == 4
        assert res2.rules_compiled == 4
        assert res2.processing_state == ProcessingState.AWAITING_HITL.value

        # CRITICAL VERIFICATION: Completed chunks (3.0.0, 3.1.0, 3.3.0) were NEVER re-extracted!
        # Only clause 3.2.0 was called on retry!
        assert call_counts["3.0.0"] == 1
        assert call_counts["3.1.0"] == 1
        assert call_counts["3.2.0"] == 2  # Retried!
        assert call_counts["3.3.0"] == 1

        # Check all clauses are now COMPILED
        clauses_after = (
            await resumable_db_session.execute(
                select(Clause).where(Clause.circular_id == res1.circular_id).order_by(Clause.id)
            )
        ).scalars().all()
        assert all(c.processing_status == ClauseProcessingState.COMPILED.value for c in clauses_after)

        # Check total rules in DB: exactly 4 rules (no duplicates)
        rules_after = (
            await resumable_db_session.execute(
                select(CompiledRule).where(CompiledRule.clause_id.in_([c.id for c in clauses_after]))
            )
        ).scalars().all()
        assert len(rules_after) == 4
        assert all(r.is_compiled for r in rules_after)

    async def test_partial_failure_persists_state_and_error_details(self, resumable_db_session):
        """When an unrecoverable exception crashes during extraction:
        - Circular state transitions to FAILED.
        - Error details are recorded in Circular and CircularStateTransition.
        - Ingested clauses remain committed in the DB.
        """
        orchestrator = E2EOrchestrator()
        chunks = [_make_chunk(i) for i in range(2)]
        mock_parsed = ParseResult(
            chunks=chunks,
            element_count=2,
            metadata=CircularMetadata(
                title="Fatal Failure Circular",
                circular_number="SEBI/CIR/2026/FATAL-01",
                issue_date=dt.date(2026, 1, 15),
            ),
            source_document_sha256="c" * 64,
            extracted_text_sha256="d" * 64,
        )
        orchestrator.parse_pdf = AsyncMock(return_value=mock_parsed)
        orchestrator.extract_and_audit_circular = AsyncMock(
            side_effect=RuntimeError("Fatal provider cluster connection loss")
        )

        with pytest.raises(RuntimeError, match="Fatal provider cluster connection loss"):
            await orchestrator.process_circular_pdf(
                session=resumable_db_session,
                file_bytes=b"fatal-pdf",
                filename="fatal.pdf",
            )

        # Check circular in DB
        circular = (
            await resumable_db_session.execute(
                select(Circular).where(Circular.circular_number == "SEBI/CIR/2026/FATAL-01")
            )
        ).scalar_one_or_none()
        assert circular is not None
        assert circular.processing_state == ProcessingState.FAILED.value
        assert "Fatal provider cluster connection loss" in (circular.error_message or "")

        # Check clauses survived intact
        clauses = (
            await resumable_db_session.execute(
                select(Clause).where(Clause.circular_id == circular.id)
            )
        ).scalars().all()
        assert len(clauses) == 2

        # Check state transition audit trail
        transitions = (
            await resumable_db_session.execute(
                select(CircularStateTransition)
                .where(CircularStateTransition.circular_id == circular.id)
                .order_by(CircularStateTransition.created_at.asc())
            )
        ).scalars().all()
        states = [t.to_state for t in transitions]
        assert "INGESTED" in states
        assert "FAILED" in states
        failed_trans = next(t for t in transitions if t.to_state == "FAILED")
        assert "Fatal provider cluster connection loss" in (failed_trans.error_message or "")

    async def test_duplicate_processing_is_idempotent(self, resumable_db_session):
        """Calling process_circular_pdf repeatedly with the same document is strictly idempotent:
        does not duplicate circulars, clauses, rules, or reviews.
        """
        orchestrator = E2EOrchestrator()
        chunks = [_make_chunk(0), _make_chunk(1)]
        mock_parsed = ParseResult(
            chunks=chunks,
            element_count=2,
            metadata=CircularMetadata(
                title="Idempotent Circular",
                circular_number="SEBI/CIR/2026/IDEM-01",
                issue_date=dt.date(2026, 1, 15),
            ),
            source_document_sha256="e" * 64,
            extracted_text_sha256="f" * 64,
        )
        orchestrator.parse_pdf = AsyncMock(return_value=mock_parsed)
        orchestrator.extract_and_audit = AsyncMock(side_effect=lambda c, s: _make_audited_rule(c))

        # First call
        res1 = await orchestrator.process_circular_pdf(
            session=resumable_db_session,
            file_bytes=b"idem-pdf",
            filename="idem.pdf",
        )

        # Second call with same PDF bytes and filename
        res2 = await orchestrator.process_circular_pdf(
            session=resumable_db_session,
            file_bytes=b"idem-pdf",
            filename="idem.pdf",
        )

        assert res1.circular_id == res2.circular_id
        assert res1.circular_number == res2.circular_number
        assert res2.rules_compiled == 2

        # Exactly 1 circular row
        circ_rows = (
            await resumable_db_session.execute(
                select(Circular).where(Circular.circular_number == "SEBI/CIR/2026/IDEM-01")
            )
        ).scalars().all()
        assert len(circ_rows) == 1

        # Exactly 2 clauses
        clause_rows = (
            await resumable_db_session.execute(
                select(Clause).where(Clause.circular_id == res1.circular_id)
            )
        ).scalars().all()
        assert len(clause_rows) == 2

        # Exactly 2 rules
        rule_rows = (
            await resumable_db_session.execute(
                select(CompiledRule).where(CompiledRule.clause_id.in_([c.id for c in clause_rows]))
            )
        ).scalars().all()
        assert len(rule_rows) == 2

        # No duplicate reviews
        review_rows = (
            await resumable_db_session.execute(
                select(HITLReview).where(HITLReview.clause_id.in_([c.id for c in clause_rows]))
            )
        ).scalars().all()
        assert len(review_rows) == 2

    async def test_successful_completion_lifecycle_and_state_transitions(self, resumable_db_session):
        """Proves full lifecycle progression through states with immutable audit log:
        INGESTED -> EXTRACTING -> EXTRACTED -> COMPILING -> AWAITING_HITL.
        """
        orchestrator = E2EOrchestrator()
        chunks = [_make_chunk(0), _make_chunk(1)]
        mock_parsed = ParseResult(
            chunks=chunks,
            element_count=2,
            metadata=CircularMetadata(
                title="Full Lifecycle Circular",
                circular_number="SEBI/CIR/2026/LIFE-01",
                issue_date=dt.date(2026, 1, 15),
            ),
            source_document_sha256="1" * 64,
            extracted_text_sha256="2" * 64,
        )
        orchestrator.parse_pdf = AsyncMock(return_value=mock_parsed)
        orchestrator.extract_and_audit = AsyncMock(side_effect=lambda c, s: _make_audited_rule(c))

        result = await orchestrator.process_circular_pdf(
            session=resumable_db_session,
            file_bytes=b"lifecycle-pdf",
            filename="lifecycle.pdf",
        )

        assert result.processing_state == ProcessingState.AWAITING_HITL.value
        assert result.status == "review_required"

        status_result = await orchestrator.get_circular_status(resumable_db_session, result.circular_id)
        assert status_result is not None
        assert status_result.processing_state == ProcessingState.AWAITING_HITL.value
        assert status_result.status == "review_required"
        assert status_result.clause_status_counts == {"COMPILED": 2}

        # Inspect state transition audit trail
        transitions = status_result.state_transitions
        assert len(transitions) >= 5
        states = [t["to_state"] for t in transitions]
        assert states == ["INGESTED", "EXTRACTING", "EXTRACTED", "COMPILING", "AWAITING_HITL"]
        assert all(t["triggered_by"] == "orchestrator" for t in transitions)
        assert all(t["created_at"] is not None for t in transitions)

    async def test_hitl_waiting_state_and_approval_workflow(self, resumable_db_session):
        """Proves:
        1. Compiled circular lands in AWAITING_HITL with inactive rules.
        2. Retries never auto-deploy rules.
        3. Human approval via HITLReviewService transitions circular to APPROVED.
        """
        orchestrator = E2EOrchestrator()
        chunk = _make_chunk(0)
        mock_parsed = ParseResult(
            chunks=[chunk],
            element_count=1,
            metadata=CircularMetadata(
                title="HITL Gated Circular",
                circular_number="SEBI/CIR/2026/GATE-01",
                issue_date=dt.date(2026, 1, 15),
            ),
            source_document_sha256="3" * 64,
            extracted_text_sha256="4" * 64,
        )
        orchestrator.parse_pdf = AsyncMock(return_value=mock_parsed)
        orchestrator.extract_and_audit = AsyncMock(return_value=_make_audited_rule(chunk))

        res = await orchestrator.process_circular_pdf(
            session=resumable_db_session,
            file_bytes=b"gate-pdf",
            filename="gate.pdf",
        )

        # 1. Verify AWAITING_HITL and inactive rule
        assert res.processing_state == ProcessingState.AWAITING_HITL.value
        assert len(res.reviews) == 1
        review_id = res.reviews[0]

        rule = (
            await resumable_db_session.execute(
                select(CompiledRule).where(CompiledRule.rule_id == f"RULE-{chunk.clause_number}")
            )
        ).scalar_one()
        assert rule.is_active is False
        assert rule.hitl_status == "BLOCKING"

        # 2. Retry must NOT deploy anything
        await orchestrator.resume_circular(resumable_db_session, res.circular_id)
        rule_recheck = (
            await resumable_db_session.execute(
                select(CompiledRule).where(CompiledRule.id == rule.id)
            )
        ).scalar_one()
        assert rule_recheck.is_active is False

        # 3. Compliance officer approves the review
        review, rule_activated, compiled_rule = await HITLReviewService.approve_review(
            session=resumable_db_session,
            review_id=review_id,
            principal_subject="compliance_officer_alice",
            notes="Upfront margin rule verified accurate against circular.",
        )

        assert rule_activated is True
        assert compiled_rule.is_active is True
        assert compiled_rule.hitl_status == "RESOLVED"

        # Check circular transitioned to APPROVED
        circular = await resumable_db_session.get(Circular, res.circular_id)
        assert circular.processing_state == ProcessingState.APPROVED.value

        status_result = await orchestrator.get_circular_status(resumable_db_session, res.circular_id)
        assert status_result.processing_state == ProcessingState.APPROVED.value
        assert status_result.status == "approved"
        assert status_result.active_rules == 1

        # Check that compliance officer was recorded in state transitions
        approved_trans = next(
            t for t in status_result.state_transitions if t["to_state"] == "APPROVED"
        )
        assert approved_trans["triggered_by"] == "compliance_officer_alice"
