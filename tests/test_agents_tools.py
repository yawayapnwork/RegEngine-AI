"""Unit tests for the guardrail tools and schemas backing the dual-agent
extraction/audit pipeline. No crewai or LLM calls involved."""
from __future__ import annotations

import pytest

from app.agents.schemas import (
    ComparisonOperator,
    ExtractedComplianceRule,
    NumericalThreshold,
    ObligationType,
    TargetEntity,
)
from app.agents.tools import (
    ClauseContextInput,
    EntityLookupInput,
    NumericScanInput,
    QuoteCheckInput,
    build_clause_context,
    lookup_entity,
    scan_numeric_tokens,
    verify_quotes,
)

SOURCE = (
    "2.1.b Every stock broker shall maintain an upfront margin of not less "
    "than 20% of the transaction value, and shall ensure adequate internal "
    "controls are in place at all times, as specified in clause 2.1."
)


def test_verify_quotes_exact_match() -> None:
    out = verify_quotes(QuoteCheckInput(quotes=["shall maintain an upfront margin of not less"], source_text=SOURCE))
    assert out.verified_count == 1
    assert out.results[0].exact_match is True


def test_verify_quotes_rejects_fabricated_quote() -> None:
    out = verify_quotes(QuoteCheckInput(quotes=["shall maintain a margin of exactly 50%"], source_text=SOURCE))
    assert out.unverified_count == 1
    assert out.results[0].verified is False


def test_verify_quotes_tolerates_whitespace_noise() -> None:
    out = verify_quotes(QuoteCheckInput(quotes=["shall  maintain   an upfront margin"], source_text=SOURCE))
    assert out.results[0].verified is True


def test_scan_numeric_tokens_finds_percentage() -> None:
    out = scan_numeric_tokens(NumericScanInput(source_text=SOURCE))
    values = {t.value for t in out.tokens}
    assert 20.0 in values
    pct_token = next(t for t in out.tokens if t.value == 20.0)
    assert pct_token.unit == "%"


def test_scan_numeric_tokens_empty_on_no_numbers() -> None:
    out = scan_numeric_tokens(NumericScanInput(source_text="Entities shall ensure adequate controls."))
    assert out.tokens == []


def test_lookup_entity_resolves_alias() -> None:
    out = lookup_entity(EntityLookupInput(entity_phrase="stock broker"))
    assert out.resolved is True
    assert out.normalized_entity == "Stockbroker"


def test_lookup_entity_unresolved_for_unknown_phrase() -> None:
    out = lookup_entity(EntityLookupInput(entity_phrase="interplanetary freight forwarder"))
    assert out.resolved is False
    assert out.normalized_entity is None


def test_build_clause_context_finds_cross_reference() -> None:
    out = build_clause_context(
        ClauseContextInput(clause_number="2.1.b", section_path=["2", "2.1", "2.1.b"], all_chunks=[]),
        current_text=SOURCE,
    )
    assert "2.1" in out.cross_reference_hits


def test_extracted_rule_schema_valid() -> None:
    rule = ExtractedComplianceRule(
        rule_id="abc123:2.1.b",
        source_chunk_id="chunk-1",
        source_sha256="abc123",
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
    assert rule.deterministic_logic[0].value == 20


def test_numerical_threshold_rejects_upper_bound_without_range_operator() -> None:
    with pytest.raises(ValueError):
        NumericalThreshold(
            metric="Margin",
            operator=ComparisonOperator.GTE,
            value=10,
            value_upper=20,
            unit="%",
            verbatim_evidence="between 10% and 20%",
        )
