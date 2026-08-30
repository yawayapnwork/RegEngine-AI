"""Agent Pipeline test suite: the async bridge into the CrewAI dual-agent
crew, and the contract between what that crew produces
(ExtractedComplianceRule + ComplianceRuleAudit) and the compiler that
consumes it -- verifying a deterministic rule ("Upfront Margin >= 20%")
compiles to a valid Rego/JSON-Logic policy schema, while an ambiguous /
low-confidence / unapproved extraction is routed to HITL instead of
silently compiled.

`app.agents.crew` (and crewai itself) is never imported for real here --
`run_dual_validation` is monkeypatched at the module level `extract_and_audit_clause`
resolves it from, so these tests run without the crewai package or any
Hugging Face Inference API call.
"""
from __future__ import annotations

import asyncio

import pytest

from app.agents import crew as crew_module
from app.agents import pipeline as pipeline_module
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
    QualitativeDirective,
    Severity,
    TargetEntity,
)
from app.compiler.hitl import has_blocking_flags
from app.compiler.models import HITLReasonCode, HITLSeverity
from app.compiler.pipeline import compile_audited_rule
from app.models import ClauseChunk

MARGIN_CLAUSE_TEXT = (
    "2.1.b Every stock broker shall maintain an upfront margin of not less "
    "than 20% of the transaction value, and shall ensure adequate internal "
    "controls are in place at all times, as specified in clause 2.1."
)


def _chunk(**overrides) -> ClauseChunk:
    base = dict(
        chunk_id="chunk-1",
        sha256="a" * 64,
        text=MARGIN_CLAUSE_TEXT,
        clause_number="2.1.b",
        circular_number="SEBI/HO/MRD/2024/1",
    )
    base.update(overrides)
    return ClauseChunk(**base)


def _approved_audit(rule_id: str, **overrides) -> ComplianceRuleAudit:
    base = dict(
        rule_id=rule_id,
        verdict=AuditVerdict.APPROVED,
        fidelity_score=0.98,
        findings=[],
        verified_quote_count=3,
        unverified_quote_count=0,
    )
    base.update(overrides)
    return ComplianceRuleAudit(**base)


def _margin_rule(**overrides) -> ExtractedComplianceRule:
    """The deterministic case: 'Upfront Margin >= 20%' with a clean,
    high-confidence extraction and a fully-scoped entity."""
    base = dict(
        rule_id="a" * 64 + ":2.1.b",
        source_chunk_id="chunk-1",
        source_sha256="a" * 64,
        circular_number="SEBI/HO/MRD/2024/1",
        clause_number="2.1.b",
        section_path=["2", "2.1", "2.1.b"],
        target_entities=[
            TargetEntity(raw_text="stock broker", normalized_entity="Stockbroker", verbatim_evidence="stock broker")
        ],
        deterministic_logic=[
            NumericalThreshold(
                metric="Upfront Margin",
                operator=ComparisonOperator.GTE,
                value=20,
                unit="%",
                applies_to="Stockbroker",
                verbatim_evidence="not less than 20% of the transaction value",
            )
        ],
        obligation_type=ObligationType.MANDATORY,
        extraction_confidence=0.95,
    )
    base.update(overrides)
    return ExtractedComplianceRule(**base)


def _ambiguous_rule(**overrides) -> ExtractedComplianceRule:
    """The ambiguous case: a purely qualitative, principle-based obligation
    with no extractable numeric threshold at all -- must never be silently
    compiled into Rego/JSON-Logic."""
    base = dict(
        rule_id="b" * 64 + ":4.2",
        source_chunk_id="chunk-2",
        source_sha256="b" * 64,
        circular_number="SEBI/HO/MRD/2024/1",
        clause_number="4.2",
        section_path=["4", "4.2"],
        target_entities=[
            TargetEntity(raw_text="stock broker", normalized_entity="Stockbroker", verbatim_evidence="stock broker")
        ],
        deterministic_logic=[],
        qualitative_directives=[
            QualitativeDirective(
                directive_text="Maintain adequate internal controls and risk management systems.",
                verbatim_evidence="shall maintain adequate internal controls and risk management systems",
            )
        ],
        obligation_type=ObligationType.MANDATORY,
        extraction_confidence=0.9,
    )
    base.update(overrides)
    return ExtractedComplianceRule(**base)


# --------------------------------------------------------------------------
# Async agent-invocation bridge (app.agents.pipeline), crewai fully mocked
# --------------------------------------------------------------------------


class TestAgentPipelineBridge:
    @pytest.mark.asyncio
    async def test_extract_and_audit_clause_returns_crew_result(self, monkeypatch):
        rule = _margin_rule()
        expected = AuditedComplianceRule(rule=rule, audit=_approved_audit(rule.rule_id), revision_round=0)

        calls: list[tuple] = []

        def _fake_run_dual_validation(chunk, sibling_chunks, settings):
            calls.append((chunk, sibling_chunks, settings))
            return expected

        monkeypatch.setattr(crew_module, "run_dual_validation", _fake_run_dual_validation)

        result = await pipeline_module.extract_and_audit_clause(_chunk())

        assert result == expected
        assert len(calls) == 1
        assert calls[0][0].chunk_id == "chunk-1"

    @pytest.mark.asyncio
    async def test_extract_and_audit_clause_runs_off_the_event_loop(self, monkeypatch):
        """`run_dual_validation` is synchronous/blocking; the bridge must
        offload it via asyncio.to_thread so it never blocks the loop."""
        main_thread_id = None

        def _fake_run_dual_validation(chunk, sibling_chunks, settings):
            import threading

            nonlocal main_thread_id
            main_thread_id = threading.get_ident()
            rule = _margin_rule()
            return AuditedComplianceRule(rule=rule, audit=_approved_audit(rule.rule_id))

        monkeypatch.setattr(crew_module, "run_dual_validation", _fake_run_dual_validation)

        import threading

        this_thread_id = threading.get_ident()
        await pipeline_module.extract_and_audit_clause(_chunk())

        assert main_thread_id is not None
        assert main_thread_id != this_thread_id

    @pytest.mark.asyncio
    async def test_extract_and_audit_circular_respects_concurrency_limit(self, monkeypatch):
        in_flight = 0
        max_observed = 0
        lock = asyncio.Lock()

        async def _fake_extract_and_audit_clause(chunk, sibling_chunks=None, settings=None):
            nonlocal in_flight, max_observed
            async with lock:
                in_flight += 1
                max_observed = max(max_observed, in_flight)
            await asyncio.sleep(0.01)
            async with lock:
                in_flight -= 1
            rule = _margin_rule(rule_id=f"{chunk.sha256}:{chunk.clause_number}", source_chunk_id=chunk.chunk_id, source_sha256=chunk.sha256)
            return AuditedComplianceRule(rule=rule, audit=_approved_audit(rule.rule_id))

        monkeypatch.setattr(pipeline_module, "extract_and_audit_clause", _fake_extract_and_audit_clause)

        chunks = [_chunk(chunk_id=f"c{i}", sha256=f"{i:064d}", clause_number=f"2.{i}") for i in range(8)]
        results = await pipeline_module.extract_and_audit_circular(chunks, max_concurrency=3)

        assert len(results) == 8
        assert max_observed <= 3


# --------------------------------------------------------------------------
# Deterministic rule -> valid compiled policy schema
# --------------------------------------------------------------------------


class TestDeterministicRuleCompilesCleanly:
    def test_margin_rule_compiles_to_valid_rego_and_jsonlogic(self):
        audited = AuditedComplianceRule(rule=_margin_rule(), audit=_approved_audit(_margin_rule().rule_id))

        result = compile_audited_rule(audited)

        assert result.compiled is True
        assert not has_blocking_flags(result.hitl_flags)

        assert result.rego is not None
        assert result.rego.thresholds_compiled == 1
        assert "input.facts.upfront_margin_pct >= 20" in result.rego.rego_code
        assert 'input.entity_type == "Stockbroker"' in result.rego.rego_code
        assert result.rego.package == "sebi.broking.circulars.sebi_ho_mrd_2024_1.clause_2_1_b"

        assert result.json_logic is not None
        assert result.json_logic.data_schema["facts.upfront_margin_pct"] == "number"
        assert result.json_logic.thresholds_compiled == 1

    def test_margin_rule_still_surfaces_advisory_flags_when_compiled(self):
        """The rule also carries a qualitative directive alongside its
        threshold; that portion is ADVISORY (never blocks compilation of
        the deterministic part) but must still be visible to a reviewer."""
        rule = _margin_rule(
            qualitative_directives=[
                QualitativeDirective(
                    directive_text="Maintain adequate internal controls.",
                    verbatim_evidence="shall ensure adequate internal controls are in place at all times",
                )
            ]
        )
        audited = AuditedComplianceRule(rule=rule, audit=_approved_audit(rule.rule_id))

        result = compile_audited_rule(audited)

        assert result.compiled is True
        advisory_reasons = {f.reason_code for f in result.hitl_flags}
        assert HITLReasonCode.QUALITATIVE_DIRECTIVE in advisory_reasons
        assert all(f.severity == HITLSeverity.ADVISORY for f in result.hitl_flags)


# --------------------------------------------------------------------------
# Ambiguous / unsafe rule -> routed to HITL, never silently compiled
# --------------------------------------------------------------------------


class TestAmbiguousRuleRoutesToHITL:
    def test_purely_qualitative_rule_is_blocked_and_flagged(self):
        rule = _ambiguous_rule()
        audited = AuditedComplianceRule(rule=rule, audit=_approved_audit(rule.rule_id))

        result = compile_audited_rule(audited)

        assert result.compiled is False
        assert result.rego is None
        assert result.json_logic is None
        assert has_blocking_flags(result.hitl_flags)

        reason_codes = {f.reason_code for f in result.hitl_flags}
        assert HITLReasonCode.NO_DETERMINISTIC_LOGIC in reason_codes
        assert HITLReasonCode.QUALITATIVE_DIRECTIVE in reason_codes

        blocking = next(f for f in result.hitl_flags if f.reason_code == HITLReasonCode.NO_DETERMINISTIC_LOGIC)
        assert blocking.severity == HITLSeverity.BLOCKING

    def test_low_confidence_extraction_blocks_compilation_despite_threshold(self):
        """Even a rule WITH a clean numeric threshold must not compile if
        the extraction agent itself was not confident in it."""
        rule = _margin_rule(extraction_confidence=0.5)
        audited = AuditedComplianceRule(rule=rule, audit=_approved_audit(rule.rule_id))

        result = compile_audited_rule(audited)

        assert result.compiled is False
        reason_codes = {f.reason_code for f in result.hitl_flags}
        assert HITLReasonCode.LOW_EXTRACTION_CONFIDENCE in reason_codes
        low_conf_flag = next(f for f in result.hitl_flags if f.reason_code == HITLReasonCode.LOW_EXTRACTION_CONFIDENCE)
        assert low_conf_flag.severity == HITLSeverity.BLOCKING

    def test_auditor_rejected_extraction_blocks_compilation(self):
        """A hallucinated/unfaithful extraction the Logic Auditor Agent
        REJECTED must never reach Rego/JSON-Logic, regardless of its
        apparent threshold quality."""
        rule = _margin_rule()
        rejected_audit = ComplianceRuleAudit(
            rule_id=rule.rule_id,
            verdict=AuditVerdict.REJECTED,
            fidelity_score=0.2,
            findings=[
                AuditFinding(
                    finding_type=FindingType.HALLUCINATED_THRESHOLD,
                    severity=Severity.BLOCKER,
                    field_path="deterministic_logic[0].value",
                    description="20% does not appear anywhere in the source clause.",
                )
            ],
            verified_quote_count=0,
            unverified_quote_count=1,
        )
        audited = AuditedComplianceRule(rule=rule, audit=rejected_audit)

        result = compile_audited_rule(audited)

        assert result.compiled is False
        reason_codes = {f.reason_code for f in result.hitl_flags}
        assert HITLReasonCode.AUDIT_NOT_APPROVED in reason_codes

    def test_conflicting_thresholds_block_compilation(self):
        rule = _margin_rule(
            deterministic_logic=[
                NumericalThreshold(
                    metric="Upfront Margin", operator=ComparisonOperator.GTE, value=25, unit="%",
                    verbatim_evidence="not less than 25%",
                ),
                NumericalThreshold(
                    metric="Upfront Margin", operator=ComparisonOperator.LT, value=15, unit="%",
                    verbatim_evidence="less than 15%",
                ),
            ]
        )
        audited = AuditedComplianceRule(rule=rule, audit=_approved_audit(rule.rule_id))

        result = compile_audited_rule(audited)

        assert result.compiled is False
        reason_codes = {f.reason_code for f in result.hitl_flags}
        assert HITLReasonCode.CONFLICTING_THRESHOLDS in reason_codes

    def test_ambiguous_spans_are_advisory_not_blocking_when_thresholds_exist(self):
        """An ambiguous leftover span alongside a clean threshold must not,
        by itself, block compilation of the deterministic part."""
        rule = _margin_rule(ambiguous_spans=["subject to such further conditions as the Board may specify"])
        audited = AuditedComplianceRule(rule=rule, audit=_approved_audit(rule.rule_id))

        result = compile_audited_rule(audited)

        assert result.compiled is True
        span_flag = next(f for f in result.hitl_flags if f.reason_code == HITLReasonCode.AMBIGUOUS_SPAN)
        assert span_flag.severity == HITLSeverity.ADVISORY
