"""Scenario generators: build the fixtures each chaos scenario injects
its fault into. Kept separate from chaos/monkey/validators.py so a
scenario's "what does a normal, healthy artifact look like before I
break it" is reusable across multiple checks (e.g. both the fidelity
check and the regression check mutate the same margin rule fixture).
"""
from __future__ import annotations

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
from app.regulatory.taxonomy import Regulator


def approved_margin_rule() -> AuditedComplianceRule:
    """A realistic, already-approved SEBI upfront-margin rule -- the
    "known good" artifact Scenario 1 corrupts. Mirrors
    tests/test_graph.py's `_approved_margin_rule` fixture shape (the
    same kind of rule this session's compiler/graph/backtest tests all
    use), so a chaos run exercises the same code paths those tests do."""
    rule = ExtractedComplianceRule(
        rule_id="c" * 64 + ":4.2.b",
        source_chunk_id="chaos-chunk-1",
        source_sha256="c" * 64,
        circular_number="SEBI/HO/MIRSD/DOP/CIR/P/2026/042",
        clause_number="4.2.b",
        target_entities=[TargetEntity(raw_text="stock broker", normalized_entity="Stockbroker", verbatim_evidence="stock broker")],
        deterministic_logic=[
            NumericalThreshold(
                metric="Upfront Margin",
                operator=ComparisonOperator.GTE,
                value=20,
                unit="%",
                verbatim_evidence="Every stock broker shall maintain upfront margin of not less than 20% of the transaction value.",
            )
        ],
        obligation_type=ObligationType.MANDATORY,
        extraction_confidence=0.97,
        regulator=Regulator.SEBI,
        regulatory_domain="broking",
    )
    audit = ComplianceRuleAudit(
        rule_id=rule.rule_id,
        verdict=AuditVerdict.APPROVED,
        fidelity_score=0.98,
        verified_quote_count=1,
        unverified_quote_count=0,
    )
    return AuditedComplianceRule(rule=rule, audit=audit)
