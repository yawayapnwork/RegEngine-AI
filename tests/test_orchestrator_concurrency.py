"""Tests for bounded concurrency, failure handling, and deterministic ordering in the circular processing pipeline.

Verifies:
1. Bounded concurrency: Semaphore strictly limits in-flight clause extraction/audit tasks.
2. Deterministic ordering: Persisted clauses, rules, and reviews maintain exact chunk sequence.
3. Individual clause failure: A failing clause does not abort the circular; produces an uncompiled,
   non-deployable rule with a blocking HITL review.
4. Non-deployability: Failed or unapproved rules cannot become active or deployable.
5. Multi-clause processing: Successful processing of circulars with multiple clauses.
6. Benchmark: Compares wall-clock throughput between sequential and concurrent execution.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import time
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
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
from app.db.models import Circular, Clause, CompiledRule, HITLReview
from app.models import CircularMetadata, ClauseChunk, ParseResult
from app.services.orchestrator import E2EOrchestrator


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
async def orchestrator_db_session():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    async with session_factory() as session:
        yield session

    await engine.dispose()


# ==============================================================================
# 1. Bounded Concurrency Verification
# ==============================================================================


@pytest.mark.asyncio
class TestBoundedConcurrency:
    async def test_extract_and_audit_circular_strictly_bounds_concurrency(self):
        """Ensures in-flight tasks never exceed the configured max_concurrency limit."""
        concurrency_limit = 3
        in_flight = 0
        max_observed_in_flight = 0
        lock = asyncio.Lock()

        settings = Settings(clause_concurrency=concurrency_limit)
        orchestrator = E2EOrchestrator(settings=settings)

        async def _mock_extract(chunk, siblings):
            nonlocal in_flight, max_observed_in_flight
            async with lock:
                in_flight += 1
                max_observed_in_flight = max(max_observed_in_flight, in_flight)
            await asyncio.sleep(0.02)
            async with lock:
                in_flight -= 1
            return _make_audited_rule(chunk)

        orchestrator.extract_and_audit = _mock_extract

        chunks = [_make_chunk(i) for i in range(10)]
        results = await orchestrator.extract_and_audit_circular(chunks)

        assert len(results) == 10
        assert max_observed_in_flight <= concurrency_limit
        assert max_observed_in_flight > 1  # Proves concurrency was actually exercised

    async def test_empty_chunks_returns_empty_list(self):
        """Edge case: empty chunk list completes cleanly."""
        orchestrator = E2EOrchestrator()
        results = await orchestrator.extract_and_audit_circular([])
        assert results == []


# ==============================================================================
# 2. Deterministic Ordering Preservation
# ==============================================================================


@pytest.mark.asyncio
class TestDeterministicOrdering:
    async def test_out_of_order_completion_preserves_input_order(self):
        """Even if chunk tasks complete out of order (e.g. variable latency),
        the returned sequence strictly mirrors the original chunk order."""
        orchestrator = E2EOrchestrator(clause_concurrency=5)

        # Invert completion times: earlier chunks take longer to complete
        async def _variable_delay_extract(chunk, siblings):
            idx = int(chunk.chunk_id.split("-")[1])
            delay = (5 - idx) * 0.01 if idx < 5 else 0.005
            await asyncio.sleep(delay)
            return _make_audited_rule(chunk)

        orchestrator.extract_and_audit = _variable_delay_extract

        chunks = [_make_chunk(i) for i in range(6)]
        results = await orchestrator.extract_and_audit_circular(chunks)

        returned_chunk_ids = [chunk.chunk_id for chunk, audited, err in results]
        expected_chunk_ids = [chunk.chunk_id for chunk in chunks]

        assert returned_chunk_ids == expected_chunk_ids


# ==============================================================================
# 3. Individual Clause Failure Handling & Non-Deployability
# ==============================================================================


@pytest.mark.asyncio
class TestClauseFailureHandling:
    async def test_individual_clause_failure_does_not_abort_circular(self, orchestrator_db_session):
        """When 1 of 4 clauses fails during LLM extraction:
        - The other 3 clauses succeed and are compiled.
        - The failed clause is recorded as an uncompiled, non-deployable rule with a blocking review.
        - Circular status is 'review_required'.
        - Transaction commits cleanly.
        """
        orchestrator = E2EOrchestrator(clause_concurrency=2)

        # Mock parse_pdf
        chunks = [_make_chunk(i) for i in range(4)]
        mock_parsed = ParseResult(
            chunks=chunks,
            element_count=len(chunks),
            metadata=CircularMetadata(
                title="Circular with One Faulty Clause",
                circular_number="SEBI/CIR/2026/004",
                issue_date=dt.date(2026, 1, 15),
            ),
            source_document_sha256="a" * 64,
            extracted_text_sha256="b" * 64,
        )
        orchestrator.parse_pdf = AsyncMock(return_value=mock_parsed)

        # Mock extract_and_audit: chunk 2 fails with LLM provider error
        async def _mock_extract(chunk, siblings):
            idx = int(chunk.chunk_id.split("-")[1])
            if idx == 2:
                raise RuntimeError("LLM rate limit reached (HTTP 429)")
            return _make_audited_rule(chunk)

        orchestrator.extract_and_audit = _mock_extract

        result = await orchestrator.process_circular_pdf(
            session=orchestrator_db_session,
            file_bytes=b"fake-pdf-content",
            filename="faulty_circular.pdf",
        )

        assert result.clause_count == 4
        # 3 succeeded and compiled, 1 failed
        assert result.rules_compiled == 3
        assert result.status == "review_required"
        assert len(result.compiled_rule_ids) == 4

        # Verify DB records
        rules = (
            await orchestrator_db_session.execute(
                CompiledRule.__table__.select().where(CompiledRule.clause_id.isnot(None))
            )
        ).mappings().all()
        assert len(rules) == 4

        # Find the failed rule
        failed_rule = next(r for r in rules if "FAILED" in r["rule_id"])
        assert failed_rule["is_compiled"] == 0 or failed_rule["is_compiled"] is False
        assert failed_rule["is_active"] == 0 or failed_rule["is_active"] is False
        assert failed_rule["hitl_status"] == "BLOCKING"
        assert failed_rule["rego_policy"] is None

        # Verify the blocking review was created for the failed rule
        reviews = (
            await orchestrator_db_session.execute(
                HITLReview.__table__.select().where(HITLReview.compiled_rule_id == failed_rule["id"])
            )
        ).mappings().all()
        assert len(reviews) >= 1
        failed_review = reviews[0]
        assert failed_review["status"] == "PENDING"
        assert failed_review["severity"] == "blocking"
        assert failed_review["reason_code"] == "audit_not_approved"
        assert "LLM rate limit reached" in failed_review["description"]

    async def test_unapproved_auditor_verdict_cannot_become_deployable(self, orchestrator_db_session):
        """When an Auditor rejects an extracted rule, it must not compile or be deployable."""
        orchestrator = E2EOrchestrator(clause_concurrency=2)

        chunk = _make_chunk(1)
        mock_parsed = ParseResult(
            chunks=[chunk],
            element_count=1,
            metadata=CircularMetadata(
                title="Circular with Rejected Rule",
                circular_number="SEBI/CIR/2026/005",
                issue_date=dt.date(2026, 1, 15),
            ),
            source_document_sha256="c" * 64,
            extracted_text_sha256="d" * 64,
        )
        orchestrator.parse_pdf = AsyncMock(return_value=mock_parsed)

        # Auditor rejects rule
        rule = _make_audited_rule(chunk)
        rule.audit.verdict = AuditVerdict.REJECTED
        rule.audit.findings.append(
            AuditFinding(
                finding_type=FindingType.HALLUCINATED_THRESHOLD,
                field_path="deterministic_logic[0].value",
                description="Threshold invented by model",
                severity=Severity.BLOCKER,
            )
        )
        orchestrator.extract_and_audit = AsyncMock(return_value=rule)

        result = await orchestrator.process_circular_pdf(
            session=orchestrator_db_session,
            file_bytes=b"fake-pdf-content",
            filename="rejected_rule.pdf",
        )

        assert result.rules_compiled == 0
        assert result.status == "review_required"

        rules = (
            await orchestrator_db_session.execute(
                CompiledRule.__table__.select()
            )
        ).mappings().all()
        assert len(rules) == 1
        assert rules[0]["is_compiled"] == 0 or rules[0]["is_compiled"] is False
        assert rules[0]["is_active"] == 0 or rules[0]["is_active"] is False
        assert rules[0]["hitl_status"] == "BLOCKING"


# ==============================================================================
# 4. Multi-Clause End-to-End Processing
# ==============================================================================


@pytest.mark.asyncio
class TestMultiClauseProcessing:
    async def test_successful_multi_clause_processing_and_status(self, orchestrator_db_session):
        """Processes 5 valid clauses concurrently and verifies lifecycle status."""
        orchestrator = E2EOrchestrator(clause_concurrency=4)

        chunks = [_make_chunk(i) for i in range(5)]
        mock_parsed = ParseResult(
            chunks=chunks,
            element_count=len(chunks),
            metadata=CircularMetadata(
                title="SEBI Master Circular on Margins",
                circular_number="SEBI/HO/MIRSD/2026/001",
                issue_date=dt.date(2026, 1, 15),
                department="MIRSD",
            ),
            source_document_sha256="e" * 64,
            extracted_text_sha256="f" * 64,
        )
        orchestrator.parse_pdf = AsyncMock(return_value=mock_parsed)
        orchestrator.extract_and_audit = AsyncMock(side_effect=lambda c, s: _make_audited_rule(c))

        result = await orchestrator.process_circular_pdf(
            session=orchestrator_db_session,
            file_bytes=b"fake-pdf-multi",
            filename="master_circular.pdf",
            source_url="https://www.sebi.gov.in/circulars/master_circular.pdf",
        )

        assert result.clause_count == 5
        assert result.rules_compiled == 5
        assert result.status == "review_required"
        assert len(result.reviews) >= 5

        # Check circular status helper
        status_res = await orchestrator.get_circular_status(orchestrator_db_session, result.circular_id)
        assert status_res is not None
        assert status_res.status == "review_required"
        assert status_res.clause_count == 5
        assert status_res.compiled_rule_count == 5
        assert status_res.active_rules == 0  # Not active prior to approval!


# ==============================================================================
# 5. Benchmark: Sequential vs Bounded Concurrency
# ==============================================================================


@pytest.mark.asyncio
class TestConcurrencyBenchmark:
    async def test_bounded_concurrency_throughput_speedup(self):
        """Benchmarks wall-clock time: 6 clauses with 30ms simulated latency each.
        - Sequential (concurrency=1): ~180ms
        - Bounded Concurrency (concurrency=3): ~60ms
        - Asserts meaningful speedup (> 1.8x).
        """
        simulated_latency = 0.03  # 30ms
        num_chunks = 6
        chunks = [_make_chunk(i) for i in range(num_chunks)]

        async def _slow_extract(chunk, siblings):
            await asyncio.sleep(simulated_latency)
            return _make_audited_rule(chunk)

        # 1. Measure sequential execution (concurrency = 1)
        orchestrator_seq = E2EOrchestrator(clause_concurrency=1)
        orchestrator_seq.extract_and_audit = _slow_extract

        start_seq = time.perf_counter()
        results_seq = await orchestrator_seq.extract_and_audit_circular(chunks)
        duration_seq = time.perf_counter() - start_seq

        # 2. Measure concurrent execution (concurrency = 3)
        orchestrator_conc = E2EOrchestrator(clause_concurrency=3)
        orchestrator_conc.extract_and_audit = _slow_extract

        start_conc = time.perf_counter()
        results_conc = await orchestrator_conc.extract_and_audit_circular(chunks)
        duration_conc = time.perf_counter() - start_conc

        assert len(results_seq) == num_chunks
        assert len(results_conc) == num_chunks

        speedup = duration_seq / max(duration_conc, 0.001)

        print(f"\n[BENCHMARK] Sequential (c=1): {duration_seq * 1000:.1f}ms")
        print(f"[BENCHMARK] Concurrent (c=3): {duration_conc * 1000:.1f}ms")
        print(f"[BENCHMARK] Speedup factor: {speedup:.2f}x")

        # Concurrency 3 over 6 items should achieve at least 1.8x speedup over sequential
        assert speedup >= 1.8, f"Expected at least 1.8x speedup, got {speedup:.2f}x"
