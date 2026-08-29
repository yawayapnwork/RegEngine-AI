"""Tests for the decision-explanation module: trace parsing, deterministic
NLG, orchestration, and the audit-ledger integration point."""
from __future__ import annotations

import datetime as dt

from app.agents.schemas import ComparisonOperator, ExtractedComplianceRule, NumericalThreshold, ObligationType, TargetEntity
from app.compiler.rego_compiler import compile_rule_to_rego
from app.execution.models import Decision, EvaluationResult, PolicyOutcome, SourceChannel, TransactionPayload
from app.explainability.explainer import explain_evaluation_result
from app.explainability.models import ExplanationSource
from app.explainability.nlg_deterministic import build_legal_explanation
from app.explainability.trace_parser import parse_violation
from app.ledger.integration import build_ledger_events


def _margin_rule(value: float = 20, applies_to: str | None = None) -> ExtractedComplianceRule:
    return ExtractedComplianceRule(
        rule_id="a" * 64 + ":4.2.b",
        source_chunk_id="c1",
        source_sha256="a" * 64,
        circular_number="SEBI/HO/MIRSD/DOP/CIR/P/2024/100",
        clause_number="4.2.b",
        target_entities=[TargetEntity(raw_text="stock broker", normalized_entity="Stockbroker", verbatim_evidence="stock broker")],
        deterministic_logic=[
            NumericalThreshold(
                metric="Upfront Margin", operator=ComparisonOperator.GTE, value=value, unit="%",
                applies_to=applies_to, verbatim_evidence=f"{value}%",
            )
        ],
        obligation_type=ObligationType.MANDATORY,
        extraction_confidence=0.95,
    )


def test_margin_rule_compiles_with_the_expected_violation_template() -> None:
    """Anchors this test file's hand-written violation-message fixtures
    (used throughout TestTraceParser/TestDeterministicNLG below) to the
    compiler's actual current output shape -- if app.compiler.rego_compiler's
    sprintf template ever changes, this fails loudly here instead of the
    hand-written fixtures silently drifting from reality."""
    compiled = compile_rule_to_rego(_margin_rule())
    assert 'sprintf("%s is %v %s, which fails the required condition (%s %v %s, clause 4.2.b)"' in compiled.rego_code


class TestTraceParser:
    def test_parses_plain_condition_failure(self) -> None:
        msg = "Upfront Margin is 15 %, which fails the required condition (>= 20 %, clause 4.2.b)"
        v = parse_violation(msg, rule_id="r1", circular_number="SEBI/HO/MIRSD/DOP/CIR/P/2024/100", clause_number="4.2.b")
        assert v is not None
        assert v.metric == "Upfront Margin"
        assert v.observed_value == 15.0
        assert v.required_value == 20.0
        assert v.operator == ">="
        assert v.unit == "%"
        assert v.applies_to is None

    def test_parses_condition_failure_with_scope(self) -> None:
        msg = "Leverage is 8 x, which fails the required condition (<= 5 x for Margin Trading Segment, clause 6.1)"
        v = parse_violation(msg, rule_id="r2", circular_number="SEBI/HO/2024/2", clause_number="6.1")
        assert v is not None
        assert v.applies_to == "Margin Trading Segment"
        assert v.operator == "<="

    def test_parses_range_low_and_high(self) -> None:
        low = parse_violation(
            "Portfolio Leverage is 0.5 x, below the required minimum of 1 x (clause 7.a)",
            rule_id="r3", circular_number="c", clause_number="7.a",
        )
        high = parse_violation(
            "Portfolio Leverage is 8 x, above the allowed maximum of 5 x (clause 7.a)",
            rule_id="r4", circular_number="c", clause_number="7.a",
        )
        assert low is not None and low.operator == "range_low" and low.required_value == 1.0
        assert high is not None and high.operator == "range_high" and high.required_value == 5.0

    def test_unrecognized_shape_returns_none(self) -> None:
        assert parse_violation("Some hand-written policy's custom message", rule_id="r5", circular_number=None, clause_number=None) is None


class TestDeterministicNLG:
    def test_matches_canonical_example_phrasing(self) -> None:
        msg = "Upfront Margin is 15 %, which fails the required condition (>= 20 %, clause 4.2.b)"
        v = parse_violation(msg, rule_id="r1", circular_number="SEBI/HO/MIRSD/DOP/CIR/P/2024/100", clause_number="4.2.b")
        explanation = build_legal_explanation(v)
        assert explanation.source == ExplanationSource.DETERMINISTIC
        assert explanation.confidence == 1.0
        assert "Margin collected (15%)" in explanation.headline
        assert "below the mandatory SEBI threshold" in explanation.headline
        assert "(20%)" in explanation.headline
        assert "SEBI Master Circular Clause 4.2.b" in explanation.headline
        assert "SEBI/HO/MIRSD/DOP/CIR/P/2024/100" in explanation.headline


class TestExplainEvaluationResult:
    def _deny_result(self) -> EvaluationResult:
        return EvaluationResult(
            transaction_id="TXN-001",
            decision=Decision.DENY,
            matched_policies=[
                PolicyOutcome(
                    rule_id="a" * 64 + ":4.2.b",
                    package="sebi.broking.circulars.x.clause_4_2_b",
                    allow=False,
                    violations=["Upfront Margin is 15 %, which fails the required condition (>= 20 %, clause 4.2.b)"],
                    circular_number="SEBI/HO/MIRSD/DOP/CIR/P/2024/100",
                    clause_number="4.2.b",
                )
            ],
            reasons=["Upfront Margin is 15 %, which fails the required condition (>= 20 %, clause 4.2.b)"],
            evaluated_at=dt.datetime.now(dt.timezone.utc),
            latency_ms=1.2,
        )

    def test_deny_result_produces_one_deterministic_explanation(self) -> None:
        bundle = explain_evaluation_result(self._deny_result())
        assert bundle.decision == "deny"
        assert len(bundle.explanations) == 1
        assert bundle.explanations[0].source == ExplanationSource.DETERMINISTIC
        assert "Trade rejected: 1 compliance violation found." == bundle.overall_summary

    def test_allow_result_produces_no_explanations(self) -> None:
        result = EvaluationResult(
            transaction_id="TXN-002", decision=Decision.ALLOW, matched_policies=[],
            evaluated_at=dt.datetime.now(dt.timezone.utc),
        )
        bundle = explain_evaluation_result(result)
        assert bundle.explanations == []
        assert "allowed" in bundle.overall_summary.lower()

    def test_ledger_event_details_carries_the_explanation(self) -> None:
        """The audit-vault integration point: app.ledger.integration.build_ledger_events
        must embed the deterministic explanation into `details`, which
        app.ledger.hash_chain includes in payload_digest -- see that
        function's docstring for why this binds the explanation into the
        cryptographic hash rather than merely storing it "next to" it."""
        result = self._deny_result()
        transaction = TransactionPayload(
            transaction_id="TXN-001", entity_type="Stockbroker", facts={}, broker_id="INZ0001001",
            source_channel=SourceChannel.REST_SYNC,
        )
        events = build_ledger_events(transaction, result)
        assert len(events) == 1
        explanation_texts = events[0].details["explanation"]
        assert len(explanation_texts) == 1
        assert "Margin collected (15%)" in explanation_texts[0]
        assert "SEBI Master Circular Clause 4.2.b" in explanation_texts[0]
