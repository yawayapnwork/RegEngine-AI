"""Tests for canonical regulatory fact taxonomy, resolution, and compiler enforcement.

Covers:
* Synonyms
* Capitalization differences
* Wording variations
* Missing mappings
* Hallucinated metrics
* Wrong units
* Wrong thresholds
* Retention of source clause supporting the metric (verbatim_evidence)
* Prohibition of arbitrary LLM strings in generated Rego
"""
from __future__ import annotations

import pytest

from app.agents.schemas import (
    AuditedComplianceRule,
    AuditVerdict,
    ComparisonOperator,
    ComplianceRuleAudit,
    ExtractedComplianceRule,
    NumericalThreshold,
    ObligationType,
    TargetEntity,
)
from app.compiler.hitl import collect_hitl_flags, has_blocking_flags
from app.compiler.models import HITLReasonCode, HITLSeverity
from app.compiler.naming import UndefinedFactError, metric_field_name
from app.compiler.pipeline import compile_audited_rule
from app.compiler.rego_compiler import compile_rule_to_rego
from app.regulatory.facts import (
    FactValidationStatus,
    get_all_canonical_facts,
    get_canonical_fact,
    resolve_canonical_fact,
)


def _approved_audit(rule_id: str) -> ComplianceRuleAudit:
    return ComplianceRuleAudit(
        rule_id=rule_id,
        verdict=AuditVerdict.APPROVED,
        fidelity_score=0.95,
        findings=[],
        verified_quote_count=1,
        unverified_quote_count=0,
    )


def _build_rule(
    metric: str,
    unit: str,
    value: float = 20.0,
    operator: ComparisonOperator = ComparisonOperator.GTE,
    verbatim_evidence: str = "shall collect minimum upfront margin of 20%",
    canonical_fact: str | None = None,
) -> ExtractedComplianceRule:
    threshold = NumericalThreshold(
        metric=metric,
        canonical_fact=canonical_fact,
        operator=operator,
        value=value,
        unit=unit,
        applies_to="Stockbroker",
        verbatim_evidence=verbatim_evidence,
    )
    return ExtractedComplianceRule(
        rule_id="test_sha:1.1",
        source_chunk_id="chk_001",
        source_sha256="test_sha",
        circular_number="SEBI/HO/MRD/2026/01",
        clause_number="1.1",
        target_entities=[
            TargetEntity(
                raw_text="Stockbroker",
                normalized_entity="Stockbroker",
                verbatim_evidence="Stockbrokers",
            )
        ],
        deterministic_logic=[threshold],
        obligation_type=ObligationType.MANDATORY,
        extraction_confidence=0.95,
    )


# ---------------------------------------------------------------------------
# 1. Canonical Taxonomy Coverage & Required Identifiers
# ---------------------------------------------------------------------------


def test_required_canonical_facts_exist() -> None:
    """Taxonomy must define at least upfront_margin_pct, client_collateral,
    peak_margin, collateral_reporting, reporting_deadline."""
    required = [
        "upfront_margin_pct",
        "client_collateral",
        "peak_margin",
        "collateral_reporting",
        "reporting_deadline",
    ]
    all_facts = get_all_canonical_facts()
    for req in required:
        assert req in all_facts, f"Missing required canonical fact: {req}"
        fact = get_canonical_fact(req)
        assert fact is not None
        assert fact.identifier == req
        assert len(fact.allowed_units) > 0


# ---------------------------------------------------------------------------
# 2. Synonyms Mapping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("metric", "unit", "expected_canonical"),
    [
        ("Upfront Margin", "%", "upfront_margin_pct"),
        ("Minimum Upfront Margin", "%", "upfront_margin_pct"),
        ("Initial Margin", "%", "upfront_margin_pct"),
        ("Minimum Initial Margin", "%", "upfront_margin_pct"),
        ("Margin Upfront", "%", "upfront_margin_pct"),
        ("Mandatory Upfront Margin", "%", "upfront_margin_pct"),
        ("Client Collateral", "INR crore", "client_collateral"),
        ("Minimum Client Collateral", "INR crore", "client_collateral"),
        ("Collateral from Clients", "lakh", "client_collateral"),
        ("Client Margin Collateral", "inr", "client_collateral"),
        ("Minimum Collateral", "INR", "client_collateral"),
        ("Peak Margin", "%", "peak_margin"),
        ("Intraday Peak Margin", "%", "peak_margin"),
        ("Minimum Peak Margin", "%", "peak_margin"),
        ("Peak Margin Requirement", "%", "peak_margin"),
        ("Collateral Reporting", "days", "collateral_reporting"),
        ("Client Collateral Reporting", "days", "collateral_reporting"),
        ("Margin Reporting", "days", "collateral_reporting"),
        ("Daily Collateral Reporting", "days", "collateral_reporting"),
        ("Reporting Deadline", "days", "reporting_deadline"),
        ("Deadline for Reporting", "days", "reporting_deadline"),
        ("Submission Deadline", "days", "reporting_deadline"),
        ("Filing Deadline", "hours", "reporting_deadline"),
        ("Compliance Reporting Deadline", "days", "reporting_deadline"),
    ],
)
def test_synonyms_map_to_canonical_fact(
    metric: str, unit: str, expected_canonical: str
) -> None:
    res = resolve_canonical_fact(metric, unit)
    assert res.is_valid is True
    assert res.canonical_identifier == expected_canonical
    assert metric_field_name(metric, unit) == expected_canonical


# ---------------------------------------------------------------------------
# 3. Capitalization Differences
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("metric", "unit", "expected_canonical"),
    [
        ("UPFRONT MARGIN", "%", "upfront_margin_pct"),
        ("upfront margin", "%", "upfront_margin_pct"),
        ("uPfRoNt MaRgIn", "%", "upfront_margin_pct"),
        ("MINIMUM UPFRONT MARGIN", "%", "upfront_margin_pct"),
        ("CLIENT COLLATERAL", "INR", "client_collateral"),
        ("Client Collateral", "INR", "client_collateral"),
        ("client collateral", "INR", "client_collateral"),
        ("PEAK MARGIN", "%", "peak_margin"),
        ("Peak Margin", "%", "peak_margin"),
        ("COLLATERAL REPORTING", "days", "collateral_reporting"),
        ("REPORTING DEADLINE", "days", "reporting_deadline"),
    ],
)
def test_capitalization_differences_map_correctly(
    metric: str, unit: str, expected_canonical: str
) -> None:
    res = resolve_canonical_fact(metric, unit)
    assert res.is_valid is True
    assert res.canonical_identifier == expected_canonical
    assert metric_field_name(metric, unit) == expected_canonical


# ---------------------------------------------------------------------------
# 4. Wording Variations
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("metric", "unit", "expected_canonical"),
    [
        ("up-front margin", "%", "upfront_margin_pct"),
        ("UP-FRONT MARGIN", "%", "upfront_margin_pct"),
        ("Minimum Upfront Margin Requirement", "%", "upfront_margin_pct"),
        ("Mandatory upfront margin collection", "%", "upfront_margin_pct"),
        ("Minimum upfront margin required", "%", "upfront_margin_pct"),
        ("Client collateral collection requirement", "INR", "client_collateral"),
        ("Collateral of client", "INR crore", "client_collateral"),
        ("Intra-day peak margin collection", "%", "peak_margin"),
        ("Collateral reporting frequency", "days", "collateral_reporting"),
        ("Reporting of client collateral", "days", "collateral_reporting"),
        ("Deadline for submission", "days", "reporting_deadline"),
        ("Statutory reporting deadline", "days", "reporting_deadline"),
    ],
)
def test_wording_variations_map_correctly(
    metric: str, unit: str, expected_canonical: str
) -> None:
    res = resolve_canonical_fact(metric, unit)
    assert res.is_valid is True
    assert res.canonical_identifier == expected_canonical
    assert metric_field_name(metric, unit) == expected_canonical


# ---------------------------------------------------------------------------
# 5. Missing Mappings & Hallucinated Metrics
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "hallucinated_metric",
    [
        "quantum_liquidity_coefficient",
        "interstellar_trade_tax",
        "arbitrary_custom_metric",
        "esg_sentiment_score",
        "undefined_compliance_ratio",
        "crypto_leverage_multiplier",
    ],
)
def test_missing_mappings_and_hallucinations_rejected(
    hallucinated_metric: str,
) -> None:
    # 1. Resolver returns MISSING_MAPPING
    res = resolve_canonical_fact(hallucinated_metric, "%")
    assert res.status == FactValidationStatus.MISSING_MAPPING
    assert res.is_valid is False
    assert "does not map to any canonical regulatory fact" in res.error_message

    # 2. metric_field_name raises UndefinedFactError (does not silently invent)
    with pytest.raises(UndefinedFactError):
        metric_field_name(hallucinated_metric, "%")

    # 3. Rule compilation blocked & marked REVIEW_REQUIRED
    evidence_text = f"shall maintain {hallucinated_metric} of at least 15%"
    rule = _build_rule(
        metric=hallucinated_metric,
        unit="%",
        value=15.0,
        verbatim_evidence=evidence_text,
    )
    audited = AuditedComplianceRule(rule=rule, audit=_approved_audit(rule.rule_id))
    flags = collect_hitl_flags(audited)

    assert has_blocking_flags(flags) is True
    unknown_flags = [
        f for f in flags if f.reason_code == HITLReasonCode.UNKNOWN_FACT_METRIC
    ]
    assert len(unknown_flags) == 1
    assert unknown_flags[0].severity == HITLSeverity.BLOCKING
    # Verbatim evidence from source clause is retained on the flag
    assert unknown_flags[0].source_excerpt == evidence_text

    # 4. Compiler refuses to compile Rego with undefined fact
    compilation = compile_audited_rule(audited)
    assert compilation.compiled is False
    assert compilation.rego is None

    # Direct call to compile_rule_to_rego must also fail validation
    with pytest.raises(UndefinedFactError):
        compile_rule_to_rego(rule)


# ---------------------------------------------------------------------------
# 6. Wrong Units
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("metric", "wrong_unit"),
    [
        ("Upfront Margin", "days"),          # Margin cannot be measured in days
        ("Upfront Margin", "INR crore"),     # Percentage margin cannot be currency
        ("Peak Margin", "months"),           # Margin cannot be months
        ("Reporting Deadline", "%"),         # Deadline cannot be percentage
        ("Collateral Reporting", "INR"),     # Reporting schedule cannot be INR
    ],
)
def test_wrong_units_rejected(metric: str, wrong_unit: str) -> None:
    res = resolve_canonical_fact(metric, wrong_unit)
    assert res.status == FactValidationStatus.WRONG_UNIT
    assert res.is_valid is False
    assert "is invalid for canonical fact" in res.error_message

    evidence = f"collect {metric} in {wrong_unit}"
    rule = _build_rule(metric=metric, unit=wrong_unit, value=10.0, verbatim_evidence=evidence)
    audited = AuditedComplianceRule(rule=rule, audit=_approved_audit(rule.rule_id))
    flags = collect_hitl_flags(audited)

    assert has_blocking_flags(flags) is True
    unit_flags = [f for f in flags if f.reason_code == HITLReasonCode.INVALID_METRIC_UNIT]
    assert len(unit_flags) == 1
    assert unit_flags[0].source_excerpt == evidence

    compilation = compile_audited_rule(audited)
    assert compilation.compiled is False

    with pytest.raises(UndefinedFactError):
        compile_rule_to_rego(rule)


# ---------------------------------------------------------------------------
# 7. Wrong Thresholds
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("metric", "unit", "wrong_value"),
    [
        ("Upfront Margin", "%", 150.0),    # > 100%
        ("Upfront Margin", "%", -10.0),    # < 0%
        ("Peak Margin", "%", 105.0),       # > 100%
        ("Client Collateral", "INR", -500.0),  # Negative collateral
        ("Reporting Deadline", "days", -2.0),  # Negative days
    ],
)
def test_wrong_thresholds_rejected(
    metric: str, unit: str, wrong_value: float
) -> None:
    res = resolve_canonical_fact(metric, unit, value=wrong_value)
    assert res.status == FactValidationStatus.WRONG_THRESHOLD
    assert res.is_valid is False
    assert (
        "is below minimum" in res.error_message
        or "exceeds maximum" in res.error_message
    )

    evidence = f"rule with value {wrong_value}"
    rule = _build_rule(metric=metric, unit=unit, value=wrong_value, verbatim_evidence=evidence)
    audited = AuditedComplianceRule(rule=rule, audit=_approved_audit(rule.rule_id))
    flags = collect_hitl_flags(audited)

    assert has_blocking_flags(flags) is True
    thresh_flags = [
        f for f in flags if f.reason_code == HITLReasonCode.INVALID_THRESHOLD_VALUE
    ]
    assert len(thresh_flags) == 1
    assert thresh_flags[0].source_excerpt == evidence

    compilation = compile_audited_rule(audited)
    assert compilation.compiled is False


# ---------------------------------------------------------------------------
# 8. Source Clause Retention and Model Schema Verification
# ---------------------------------------------------------------------------


def test_source_clause_verbatim_evidence_retained() -> None:
    exact_source_text = "Clause 2.1: Stockbrokers shall collect a minimum upfront margin of 20% from clients."
    threshold = NumericalThreshold(
        metric="Minimum Upfront Margin",
        operator=ComparisonOperator.GTE,
        value=20.0,
        unit="%",
        applies_to="Stockbroker",
        verbatim_evidence=exact_source_text,
    )

    # Automatically resolves canonical_fact
    assert threshold.canonical_fact == "upfront_margin_pct"
    assert threshold.verbatim_evidence == exact_source_text

    # Serialization retains both metric and canonical_fact as requested in prompt example
    dumped = threshold.model_dump()
    assert dumped["metric"] == "Minimum Upfront Margin"
    assert dumped["canonical_fact"] == "upfront_margin_pct"
    assert dumped["verbatim_evidence"] == exact_source_text


def test_explicit_canonical_fact_mapping() -> None:
    """Matches prompt example:
    {
        "metric": "Minimum Upfront Margin",
        "canonical_fact": "upfront_margin_pct"
    }
    """
    threshold_dict = {
        "metric": "Minimum Upfront Margin",
        "canonical_fact": "upfront_margin_pct",
        "operator": ">=",
        "value": 20.0,
        "unit": "%",
        "verbatim_evidence": "minimum upfront margin 20%",
    }
    threshold = NumericalThreshold.model_validate(threshold_dict)
    assert threshold.canonical_fact == "upfront_margin_pct"
    assert threshold.metric == "Minimum Upfront Margin"


# ---------------------------------------------------------------------------
# 9. Rego Compiler Only Generates Canonical Identifiers
# ---------------------------------------------------------------------------


def test_compiler_uses_only_canonical_identifiers() -> None:
    """Verifies that even if LLM metric name was 'Minimum Upfront Margin',
    compiled Rego contains only 'input.facts.upfront_margin_pct' and NEVER
    'minimum_upfront_margin_pct'."""
    rule = _build_rule(
        metric="Minimum Upfront Margin",
        unit="%",
        value=20.0,
        verbatim_evidence="minimum upfront margin of 20%",
    )
    compiled = compile_rule_to_rego(rule)
    rego_code = compiled.rego_code

    # MUST contain canonical identifier
    assert "input.facts.upfront_margin_pct >= 20" in rego_code
    assert "input.facts.upfront_margin_pct < 20" in rego_code

    # MUST NOT contain LLM-invented field name
    assert "minimum_upfront_margin" not in rego_code
    assert "minimum_upfront_margin_pct" not in rego_code
