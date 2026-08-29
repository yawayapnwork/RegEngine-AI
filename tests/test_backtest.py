"""Tests for the backtesting service: the JSON-Logic evaluator (verified
against REAL app.compiler.jsonlogic_compiler output, not hand-written
fixtures, so a future compiler change that drifts from what this
evaluator supports is caught here), the candidate evaluator, and the
Pandas-based delta classification/summary math.

app.backtest.replay_engine.fetch_historical_transactions and
app.backtest.tasks (DB/Redis-dependent) are not covered here -- no live
Postgres/Redis in this environment; see those modules' docstrings for
what they do.
"""
from __future__ import annotations

import datetime as dt

import pytest

from app.agents.schemas import ComparisonOperator, ExtractedComplianceRule, NumericalThreshold, ObligationType, TargetEntity
from app.backtest.candidate_evaluator import JsonLogicCandidateEvaluator
from app.backtest.jsonlogic_evaluator import MissingFactError, UnsupportedJsonLogicNodeError, evaluate_jsonlogic
from app.backtest.models import DeltaChangeType, HistoricalTransaction
from app.backtest.reporting import build_outcomes, build_summary
from app.compiler.jsonlogic_compiler import compile_rule_to_jsonlogic
from app.execution.models import Decision


def _margin_rule(value: float = 25, entities: list[str] | None = None) -> ExtractedComplianceRule:
    entities = entities or ["Stockbroker"]
    return ExtractedComplianceRule(
        rule_id="a" * 64 + ":4.2.b", source_chunk_id="c1", source_sha256="a" * 64,
        circular_number="SEBI/HO/1/2025/1", clause_number="4.2.b",
        target_entities=[TargetEntity(raw_text=e, normalized_entity=e, verbatim_evidence=e) for e in entities],
        deterministic_logic=[NumericalThreshold(metric="Upfront Margin", operator=ComparisonOperator.GTE, value=value, unit="%", verbatim_evidence=f"{value}%")],
        obligation_type=ObligationType.MANDATORY, extraction_confidence=0.95,
    )


class TestJsonLogicEvaluatorAgainstRealCompilerOutput:
    def test_single_threshold_pass_and_fail(self) -> None:
        jl = compile_rule_to_jsonlogic(_margin_rule(25))
        assert evaluate_jsonlogic(jl.logic, {"entity_type": "Stockbroker", "facts": {"upfront_margin_pct": 30}}) is True
        assert evaluate_jsonlogic(jl.logic, {"entity_type": "Stockbroker", "facts": {"upfront_margin_pct": 20}}) is False

    def test_wrong_entity_type_fails(self) -> None:
        jl = compile_rule_to_jsonlogic(_margin_rule(25))
        assert evaluate_jsonlogic(jl.logic, {"entity_type": "AssetManager", "facts": {"upfront_margin_pct": 30}}) is False

    def test_multi_entity_uses_in_operator(self) -> None:
        jl = compile_rule_to_jsonlogic(_margin_rule(25, entities=["Stockbroker", "AssetManager"]))
        assert evaluate_jsonlogic(jl.logic, {"entity_type": "AssetManager", "facts": {"upfront_margin_pct": 30}}) is True

    def test_range_threshold(self) -> None:
        rule = _margin_rule()
        rule = rule.model_copy(update={"deterministic_logic": [
            NumericalThreshold(metric="Leverage", operator=ComparisonOperator.RANGE, value=1, value_upper=5, unit="x", verbatim_evidence="1x-5x")
        ]})
        jl = compile_rule_to_jsonlogic(rule)
        assert evaluate_jsonlogic(jl.logic, {"entity_type": "Stockbroker", "facts": {"leverage": 3}}) is True
        assert evaluate_jsonlogic(jl.logic, {"entity_type": "Stockbroker", "facts": {"leverage": 8}}) is False

    def test_missing_fact_raises(self) -> None:
        jl = compile_rule_to_jsonlogic(_margin_rule(25))
        with pytest.raises(MissingFactError):
            evaluate_jsonlogic(jl.logic, {"entity_type": "Stockbroker", "facts": {}})

    def test_unsupported_node_raises(self) -> None:
        with pytest.raises(UnsupportedJsonLogicNodeError):
            evaluate_jsonlogic({"or": [{"==": [1, 1]}]}, {"entity_type": "x", "facts": {}})


@pytest.mark.asyncio
class TestJsonLogicCandidateEvaluator:
    async def test_allow_deny_and_flagged(self) -> None:
        jl = compile_rule_to_jsonlogic(_margin_rule(25))
        evaluator = JsonLogicCandidateEvaluator(jl.logic)

        decision, violations = await evaluator.evaluate("Stockbroker", {"upfront_margin_pct": 30})
        assert decision == Decision.ALLOW.value and violations == []

        decision, violations = await evaluator.evaluate("Stockbroker", {"upfront_margin_pct": 15})
        assert decision == Decision.DENY.value and violations

        decision, violations = await evaluator.evaluate("Stockbroker", {})
        assert decision == Decision.FLAGGED.value and "upfront_margin_pct" in violations[0]


class TestDeltaClassificationAndSummary:
    def _txn(self, txn_id: str, broker_id: str, old_decision: str) -> HistoricalTransaction:
        return HistoricalTransaction(
            transaction_id=txn_id, broker_id=broker_id, entity_type="Stockbroker", facts={},
            evaluated_at=dt.datetime.now(dt.timezone.utc), rule_id="r1", circular_number="c1", clause_number="4.2.b",
            old_decision=old_decision, old_violations=[] if old_decision == "allow" else ["old violation"],
        )

    def test_unchanged_pass_and_fail(self) -> None:
        outcomes = build_outcomes([
            (self._txn("t1", "b1", "allow"), "allow", []),
            (self._txn("t2", "b1", "deny"), "deny", ["still fails"]),
        ])
        assert outcomes[0].change_type == DeltaChangeType.UNCHANGED_PASS
        assert outcomes[1].change_type == DeltaChangeType.UNCHANGED_FAIL

    def test_new_failure_and_newly_passing(self) -> None:
        outcomes = build_outcomes([
            (self._txn("t1", "b1", "allow"), "deny", ["now fails"]),
            (self._txn("t2", "b1", "deny"), "allow", []),
        ])
        assert outcomes[0].change_type == DeltaChangeType.NEW_FAILURE
        assert outcomes[1].change_type == DeltaChangeType.NEWLY_PASSING

    def test_undefined_now(self) -> None:
        outcomes = build_outcomes([(self._txn("t1", "b1", "allow"), "flagged", ["missing fact"])])
        assert outcomes[0].change_type == DeltaChangeType.UNDEFINED_NOW

    def test_summary_projects_failure_rate_shift(self) -> None:
        # 4 transactions: 1 historically failed, 2 unaffected, 1 becomes a NEW failure
        outcomes = build_outcomes([
            (self._txn("t1", "b1", "allow"), "allow", []),          # unchanged pass
            (self._txn("t2", "b1", "allow"), "deny", ["x"]),         # NEW failure
            (self._txn("t3", "b2", "deny"), "deny", ["x"]),          # unchanged fail
            (self._txn("t4", "b2", "allow"), "allow", []),          # unchanged pass
        ])
        summary = build_summary("r1", 30, outcomes)
        assert summary.total_transactions == 4
        assert summary.old_fail_count == 1
        assert summary.new_fail_count == 2
        assert summary.old_failure_rate_pct == 25.0
        assert summary.new_failure_rate_pct == 50.0
        assert summary.delta_failure_rate_pct == 25.0
        assert summary.new_failures == 1
        assert summary.newly_passing == 0

    def test_per_broker_breakdown(self) -> None:
        outcomes = build_outcomes([
            (self._txn("t1", "brokerA", "allow"), "deny", ["x"]),
            (self._txn("t2", "brokerA", "allow"), "allow", []),
            (self._txn("t3", "brokerB", "allow"), "allow", []),
        ])
        summary = build_summary("r1", 30, outcomes)
        by_broker = {b.broker_id: b for b in summary.broker_breakdown}
        assert by_broker["brokerA"].new_failures == 1
        assert by_broker["brokerA"].projected_failure_rate_pct == 50.0
        assert by_broker["brokerB"].new_failures == 0
        assert by_broker["brokerB"].projected_failure_rate_pct == 0.0

    def test_empty_outcomes_produce_zeroed_summary(self) -> None:
        summary = build_summary("r1", 30, [])
        assert summary.total_transactions == 0
        assert summary.new_failure_rate_pct == 0.0
        assert summary.broker_breakdown == []
