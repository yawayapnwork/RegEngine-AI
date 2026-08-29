"""Unit tests for the Rego / JSON-Logic / HITL compilation pipeline."""
from __future__ import annotations

import datetime as dt

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
from app.compiler.hitl import collect_hitl_flags, has_blocking_flags
from app.compiler.jsonlogic_compiler import compile_rule_to_jsonlogic
from app.compiler.models import HITLReasonCode, HITLSeverity
from app.compiler.naming import metric_field_name, rego_package_name
from app.compiler.pipeline import compile_audited_rule
from app.compiler.rego_compiler import compile_rule_to_rego
from app.regulatory.taxonomy import Regulator


def _approved_audit(rule_id: str) -> ComplianceRuleAudit:
    return ComplianceRuleAudit(
        rule_id=rule_id,
        verdict=AuditVerdict.APPROVED,
        fidelity_score=0.98,
        findings=[],
        verified_quote_count=3,
        unverified_quote_count=0,
    )


def _margin_rule(**overrides) -> ExtractedComplianceRule:
    base = dict(
        rule_id="abc123:2.1.b",
        source_chunk_id="chunk-1",
        source_sha256="abc123",
        circular_number="SEBI/HO/MRD/2024/1",
        clause_number="2.1.b",
        section_path=["2", "2.1", "2.1.b"],
        target_entities=[TargetEntity(raw_text="stock broker", normalized_entity="Stockbroker", verbatim_evidence="stock broker")],
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


def test_metric_field_name_derivation() -> None:
    assert metric_field_name("Upfront Margin", "%") == "upfront_margin_pct"
    assert metric_field_name("Net Worth", "INR crore") == "net_worth_inr_crore"


def test_rego_package_name() -> None:
    assert (
        rego_package_name(Regulator.SEBI, "broking", "SEBI/HO/MRD/2024/1", "2.1.b")
        == "sebi.broking.circulars.sebi_ho_mrd_2024_1.clause_2_1_b"
    )


def test_rego_package_name_rbi_namespace() -> None:
    assert (
        rego_package_name(Regulator.RBI, "nbfc", "RBI/2024-25/45", "3.a")
        == "rbi.nbfc.circulars.rbi_2024_25_45.clause_3_a"
    )


def test_compile_rule_to_rego_basic_structure() -> None:
    rule = _margin_rule()
    compiled = compile_rule_to_rego(rule)

    assert compiled.package == "sebi.broking.circulars.sebi_ho_mrd_2024_1.clause_2_1_b"
    assert compiled.thresholds_compiled == 1
    code = compiled.rego_code
    assert "package sebi.broking.circulars.sebi_ho_mrd_2024_1.clause_2_1_b" in code
    assert "import rego.v1" in code
    assert "default allow := false" in code
    assert 'input.entity_type == "Stockbroker"' in code
    assert "input.facts.upfront_margin_pct >= 20" in code
    assert "input.facts.upfront_margin_pct < 20" in code  # negated violation condition
    assert "violation contains msg if {" in code
    assert "deny := violation" in code
    assert '"rule_id": "abc123:2.1.b"' in code


def test_compile_rego_range_operator_produces_two_violation_bodies() -> None:
    rule = _margin_rule(
        deterministic_logic=[
            NumericalThreshold(
                metric="Net Worth",
                operator=ComparisonOperator.RANGE,
                value=10,
                value_upper=50,
                unit="INR crore",
                verbatim_evidence="between 10 and 50 INR crore",
            )
        ]
    )
    code = compile_rule_to_rego(rule).rego_code
    assert "input.facts.net_worth_inr_crore >= 10" in code
    assert "input.facts.net_worth_inr_crore <= 50" in code
    assert "input.facts.net_worth_inr_crore < 10" in code
    assert "input.facts.net_worth_inr_crore > 50" in code


def test_compile_rule_to_jsonlogic_matches_rego_field_naming() -> None:
    rule = _margin_rule()
    compiled = compile_rule_to_jsonlogic(rule)

    assert compiled.logic == {
        "and": [
            {"==": [{"var": "entity_type"}, "Stockbroker"]},
            {">=": [{"var": "facts.upfront_margin_pct"}, 20]},
        ]
    }
    assert compiled.data_schema["facts.upfront_margin_pct"] == "number"
    assert "{facts.upfront_margin_pct}" in compiled.violation_message_template


def test_jsonlogic_range_uses_and_of_bounds() -> None:
    rule = _margin_rule(
        target_entities=[],
        deterministic_logic=[
            NumericalThreshold(
                metric="Net Worth", operator=ComparisonOperator.RANGE, value=10, value_upper=50, unit="INR crore",
                verbatim_evidence="between 10 and 50 INR crore",
            )
        ],
    )
    compiled = compile_rule_to_jsonlogic(rule)
    assert compiled.logic == {
        "and": [
            {">=": [{"var": "facts.net_worth_inr_crore"}, 10]},
            {"<=": [{"var": "facts.net_worth_inr_crore"}, 50]},
        ]
    }


def test_hitl_flags_qualitative_directive_as_advisory() -> None:
    rule = _margin_rule(
        qualitative_directives=[
            QualitativeDirective(directive_text="maintain adequate internal controls", verbatim_evidence="adequate internal controls")
        ]
    )
    audited = AuditedComplianceRule(rule=rule, audit=_approved_audit(rule.rule_id))
    flags = collect_hitl_flags(audited)

    qual_flags = [f for f in flags if f.reason_code == HITLReasonCode.QUALITATIVE_DIRECTIVE]
    assert len(qual_flags) == 1
    assert qual_flags[0].severity == HITLSeverity.ADVISORY
    assert has_blocking_flags(flags) is False


def test_hitl_flags_no_deterministic_logic_as_blocking() -> None:
    rule = _margin_rule(deterministic_logic=[])
    audited = AuditedComplianceRule(rule=rule, audit=_approved_audit(rule.rule_id))
    flags = collect_hitl_flags(audited)

    assert any(f.reason_code == HITLReasonCode.NO_DETERMINISTIC_LOGIC and f.severity == HITLSeverity.BLOCKING for f in flags)
    assert has_blocking_flags(flags) is True


def test_hitl_flags_low_confidence_as_blocking() -> None:
    rule = _margin_rule(extraction_confidence=0.4)
    audited = AuditedComplianceRule(rule=rule, audit=_approved_audit(rule.rule_id))
    flags = collect_hitl_flags(audited)

    assert any(f.reason_code == HITLReasonCode.LOW_EXTRACTION_CONFIDENCE for f in flags)
    assert has_blocking_flags(flags) is True


def test_hitl_flags_conflicting_thresholds() -> None:
    rule = _margin_rule(
        deterministic_logic=[
            NumericalThreshold(metric="Margin", operator=ComparisonOperator.GTE, value=30, unit="%", verbatim_evidence="at least 30%"),
            NumericalThreshold(metric="Margin", operator=ComparisonOperator.LTE, value=10, unit="%", verbatim_evidence="not exceeding 10%"),
        ]
    )
    audited = AuditedComplianceRule(rule=rule, audit=_approved_audit(rule.rule_id))
    flags = collect_hitl_flags(audited)

    assert any(f.reason_code == HITLReasonCode.CONFLICTING_THRESHOLDS for f in flags)


def test_hitl_flags_audit_not_approved() -> None:
    rule = _margin_rule()
    audit = ComplianceRuleAudit(
        rule_id=rule.rule_id,
        verdict=AuditVerdict.REJECTED,
        fidelity_score=0.2,
        findings=[
            AuditFinding(
                finding_type=FindingType.HALLUCINATED_THRESHOLD,
                severity=Severity.BLOCKER,
                field_path="deterministic_logic[0].value",
                description="Value 20 not found in source text.",
            )
        ],
        verified_quote_count=0,
        unverified_quote_count=1,
    )
    audited = AuditedComplianceRule(rule=rule, audit=audit)
    flags = collect_hitl_flags(audited)

    assert any(f.reason_code == HITLReasonCode.AUDIT_NOT_APPROVED for f in flags)
    assert has_blocking_flags(flags) is True


def test_pipeline_skips_compilation_when_blocking_flags_present() -> None:
    rule = _margin_rule()
    audit = ComplianceRuleAudit(
        rule_id=rule.rule_id, verdict=AuditVerdict.REJECTED, fidelity_score=0.1, findings=[], verified_quote_count=0, unverified_quote_count=1
    )
    result = compile_audited_rule(AuditedComplianceRule(rule=rule, audit=audit))

    assert result.compiled is False
    assert result.rego is None
    assert result.json_logic is None
    assert len(result.hitl_flags) >= 1


def test_pipeline_compiles_when_approved_and_deterministic() -> None:
    rule = _margin_rule()
    audited = AuditedComplianceRule(rule=rule, audit=_approved_audit(rule.rule_id))
    result = compile_audited_rule(audited)

    assert result.compiled is True
    assert result.rego is not None
    assert result.json_logic is not None
    assert "package sebi.broking.circulars" in result.rego.rego_code


def test_pipeline_partial_compile_with_advisory_qualitative_flag() -> None:
    rule = _margin_rule(
        qualitative_directives=[
            QualitativeDirective(directive_text="maintain adequate internal controls", verbatim_evidence="adequate internal controls")
        ]
    )
    audited = AuditedComplianceRule(rule=rule, audit=_approved_audit(rule.rule_id))
    result = compile_audited_rule(audited)

    # Deterministic threshold still compiles even though a qualitative directive
    # in the same clause is flagged for HITL.
    assert result.compiled is True
    assert result.rego is not None
    assert any(f.reason_code == HITLReasonCode.QUALITATIVE_DIRECTIVE for f in result.hitl_flags)
