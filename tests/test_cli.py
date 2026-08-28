"""Unit tests for regengine-cli.py's pure logic: the synthetic sample PDF
builder, the offline canned rule, the violating-facts derivation, and the
OPA-result -> Decision/EvaluationOutcome mapping.

The CLI is loaded via importlib (its filename isn't a valid Python
identifier -- `regengine-cli.py`, not `regengine_cli.py`) rather than a
plain `import`, matching how the file itself is meant to be run
(`python regengine-cli.py ...`), not imported as a package module.
"""
from __future__ import annotations

import importlib.util
import io
from pathlib import Path

import pytest

_CLI_PATH = Path(__file__).resolve().parent.parent / "regengine-cli.py"
_spec = importlib.util.spec_from_file_location("regengine_cli", _CLI_PATH)
cli = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cli)

from app.agents.schemas import ComparisonOperator, NumericalThreshold
from app.models import ClauseChunk


class TestSamplePdfBuilder:
    def test_produces_valid_pdf_bytes(self):
        pdf_bytes = cli.build_sample_pdf_bytes()
        assert pdf_bytes.startswith(b"%PDF-1.4")
        assert pdf_bytes.rstrip().endswith(b"%%EOF")

    def test_readable_by_a_real_pdf_parser(self):
        pypdf = pytest.importorskip("pypdf")
        pdf_bytes = cli.build_sample_pdf_bytes()
        reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
        assert len(reader.pages) == 1
        text = reader.pages[0].extract_text()
        assert "SEBI/HO/MRD/DP/CIR/P/2026/45" in text
        assert "20%" in text

    def test_escapes_parentheses_and_backslashes(self):
        # A line containing PDF-special characters must not corrupt the
        # generated content stream (unbalanced parens would break parsing).
        pdf_bytes = cli.build_sample_pdf_bytes(["A clause (with a parenthetical) and a backslash \\ in it."])
        assert pdf_bytes.startswith(b"%PDF-1.4")
        pypdf = pytest.importorskip("pypdf")
        reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
        text = reader.pages[0].extract_text()
        assert "(with a parenthetical)" in text


class TestCannedAuditedRule:
    def test_binds_to_the_actual_ingested_chunk(self):
        chunk = ClauseChunk(
            chunk_id="chunk-xyz", sha256="c" * 64, text="Some margin clause text.",
            clause_number="2.1.b", circular_number="SEBI/HO/MRD/2024/1", section_path=["2", "2.1", "2.1.b"],
        )
        audited = cli._canned_audited_rule(chunk)

        assert audited.rule.source_sha256 == "c" * 64
        assert audited.rule.source_chunk_id == "chunk-xyz"
        assert audited.rule.circular_number == "SEBI/HO/MRD/2024/1"
        assert audited.rule.clause_number == "2.1.b"
        assert audited.rule.rule_id == f"{'c' * 64}:2.1.b"
        assert audited.audit.verdict.value == "approved"
        assert len(audited.rule.deterministic_logic) == 1


class TestSelectTargetChunk:
    def test_prefers_chunk_with_percent_sign(self):
        from app.models import ParseResult, CircularMetadata

        chunks = [
            ClauseChunk(chunk_id="a", sha256="a" * 64, text="Applicability clause, no numbers here.", clause_number="1"),
            ClauseChunk(chunk_id="b", sha256="b" * 64, text="Margin shall be at least 20%.", clause_number="2.1"),
        ]
        parsed = ParseResult(metadata=CircularMetadata(), chunks=chunks, element_count=2)

        target = cli._select_target_chunk(parsed)

        assert target.chunk_id == "b"

    def test_falls_back_to_first_chunk_when_no_percent_found(self):
        from app.models import ParseResult, CircularMetadata

        chunks = [
            ClauseChunk(chunk_id="a", sha256="a" * 64, text="Applicability clause.", clause_number="1"),
            ClauseChunk(chunk_id="b", sha256="b" * 64, text="Another plain clause.", clause_number="2"),
        ]
        parsed = ParseResult(metadata=CircularMetadata(), chunks=chunks, element_count=2)

        assert cli._select_target_chunk(parsed).chunk_id == "a"


class TestDefaultViolatingFacts:
    def test_gte_threshold_gets_a_lower_value(self):
        thresholds = [NumericalThreshold(metric="Upfront Margin", operator=ComparisonOperator.GTE, value=20, unit="%", verbatim_evidence="x")]
        facts = cli._default_violating_facts(thresholds)
        assert facts == {"upfront_margin_pct": 15.0}

    def test_lt_threshold_gets_a_higher_value(self):
        thresholds = [NumericalThreshold(metric="Leverage Ratio", operator=ComparisonOperator.LT, value=10, unit="x", verbatim_evidence="x")]
        facts = cli._default_violating_facts(thresholds)
        assert facts["leverage_ratio"] == 15.0

    def test_range_threshold_uses_upper_bound_plus_margin(self):
        thresholds = [
            NumericalThreshold(
                metric="Net Worth", operator=ComparisonOperator.RANGE, value=5, value_upper=20, unit="INR crore",
                verbatim_evidence="x",
            )
        ]
        facts = cli._default_violating_facts(thresholds)
        assert facts["net_worth_inr_crore"] == 25.0

    def test_empty_thresholds_yields_empty_facts(self):
        assert cli._default_violating_facts([]) == {}


class TestMapOpaResult:
    def test_none_result_is_flagged_for_hitl(self):
        decision, outcome = cli._map_opa_result(None)
        assert decision.value == "flagged"
        assert outcome.value == "HITL_REVIEW"

    def test_violations_present_is_deny(self):
        decision, outcome = cli._map_opa_result({"allow": False, "violations": ["margin too low"]})
        assert decision.value == "deny"
        assert outcome.value == "FAIL"

    def test_no_violations_is_allow(self):
        decision, outcome = cli._map_opa_result({"allow": True, "violations": []})
        assert decision.value == "allow"
        assert outcome.value == "PASS"
