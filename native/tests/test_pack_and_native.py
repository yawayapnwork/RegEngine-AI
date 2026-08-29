"""End-to-end correctness proof for the native policy kernel: real
`app.compiler.jsonlogic_compiler` output -> `native.tools.pack_policy` ->
the compiled `regengine_native` extension -> compared, input-by-input,
against the real `app.backtest.jsonlogic_evaluator.evaluate_jsonlogic`
(the same evaluator app.backtest already trusts for historical replay).
A native engine that agrees with that evaluator on every input IS, by
this system's own definition, computing the correct answer -- there is
no separate "native semantics" to get right independently.

Requires the compiled extension: run
    cd native && python setup.py build_ext --inplace
first (see native/setup.py's docstring). Skipped automatically if the
extension isn't built, so the rest of this repo's test suite is
unaffected by whether a C++ toolchain is available.
"""
from __future__ import annotations

import random
import sys
from pathlib import Path

import pytest

_NATIVE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_NATIVE_DIR / "src"))
sys.path.insert(0, str(_NATIVE_DIR / "tools"))

regengine_native = pytest.importorskip("regengine_native", reason="native extension not built -- see native/setup.py")
from pack_policy import UnsupportedPolicyShapeError, pack_policy  # noqa: E402

from app.agents.schemas import (  # noqa: E402
    ComparisonOperator,
    ExtractedComplianceRule,
    NumericalThreshold,
    ObligationType,
    TargetEntity,
)
from app.backtest.jsonlogic_evaluator import evaluate_jsonlogic  # noqa: E402
from app.compiler.jsonlogic_compiler import compile_rule_to_jsonlogic  # noqa: E402


def _margin_rule() -> ExtractedComplianceRule:
    return ExtractedComplianceRule(
        rule_id="a" * 64 + ":3.2.1", source_chunk_id="c1", source_sha256="a" * 64,
        circular_number="SEBI/HO/MIRSD/2026/01", clause_number="3.2.1",
        target_entities=[TargetEntity(raw_text="stock broker", normalized_entity="Stockbroker", verbatim_evidence="stock broker")],
        deterministic_logic=[NumericalThreshold(metric="Upfront Margin", operator=ComparisonOperator.GTE, value=20, unit="%", verbatim_evidence="not less than 20%")],
        obligation_type=ObligationType.MANDATORY, extraction_confidence=0.95,
    )


def _multi_threshold_rule() -> ExtractedComplianceRule:
    return ExtractedComplianceRule(
        rule_id="b" * 64 + ":5.1.0", source_chunk_id="c2", source_sha256="b" * 64,
        circular_number="SEBI/HO/MIRSD/2026/02", clause_number="5.1.0",
        target_entities=[TargetEntity(raw_text="stock broker", normalized_entity="Stockbroker", verbatim_evidence="stock broker")],
        deterministic_logic=[
            NumericalThreshold(metric="Upfront Margin", operator=ComparisonOperator.GTE, value=20, unit="%", verbatim_evidence="not less than 20%"),
            NumericalThreshold(metric="Net Worth", operator=ComparisonOperator.GTE, value=5, unit="INR crore", verbatim_evidence="not less than 5 crore"),
        ],
        obligation_type=ObligationType.MANDATORY, extraction_confidence=0.95,
    )


class TestPackPolicy:
    def test_pack_and_load_single_threshold_rule(self):
        compiled = compile_rule_to_jsonlogic(_margin_rule())
        data, slots = pack_policy(_margin_rule().rule_id, compiled.logic)
        policy = regengine_native.CompiledPolicy(data, slots)
        assert policy.rule_id == _margin_rule().rule_id
        assert policy.num_checks == 1
        assert slots == {"upfront_margin_pct": 0}

    def test_unsupported_shape_raises(self):
        with pytest.raises(UnsupportedPolicyShapeError):
            pack_policy("r1", {"or": [{"==": [1, 1]}]})

    def test_rule_id_too_long_raises(self):
        with pytest.raises(UnsupportedPolicyShapeError):
            pack_policy("x" * 200, {">=": [{"var": "facts.a"}, 1]})


class TestNativeMatchesPythonEvaluator:
    @pytest.mark.parametrize("entity_type,margin,expected", [
        ("Stockbroker", 25.0, True),
        ("Stockbroker", 20.0, True),  # boundary: >= is inclusive
        ("Stockbroker", 19.99, False),
        ("Stockbroker", 0.0, False),
        ("DepositoryParticipant", 99.0, False),  # entity mismatch -> AND is false, matching evaluate_jsonlogic's literal semantics
    ])
    def test_known_cases_match(self, entity_type, margin, expected):
        rule = _margin_rule()
        compiled = compile_rule_to_jsonlogic(rule)
        data, slots = pack_policy(rule.rule_id, compiled.logic)
        policy = regengine_native.CompiledPolicy(data, slots)

        doc = {"entity_type": entity_type, "facts": {"upfront_margin_pct": margin}}
        py_result = bool(evaluate_jsonlogic(compiled.logic, doc))
        native_result = policy.evaluate_facts(doc)

        assert py_result == expected
        assert native_result == expected

    def test_random_fuzz_agrees_with_python_evaluator(self):
        rule = _margin_rule()
        compiled = compile_rule_to_jsonlogic(rule)
        data, slots = pack_policy(rule.rule_id, compiled.logic)
        policy = regengine_native.CompiledPolicy(data, slots)

        rng = random.Random(1234)  # deterministic across runs
        for _ in range(5000):
            margin = round(rng.uniform(-10, 50), 2)
            entity = rng.choice(["Stockbroker", "DepositoryParticipant", "Custodian"])
            doc = {"entity_type": entity, "facts": {"upfront_margin_pct": margin}}
            assert policy.evaluate_facts(doc) == bool(evaluate_jsonlogic(compiled.logic, doc)), doc

    def test_multi_threshold_rule_fuzz_agrees(self):
        rule = _multi_threshold_rule()
        compiled = compile_rule_to_jsonlogic(rule)
        data, slots = pack_policy(rule.rule_id, compiled.logic)
        policy = regengine_native.CompiledPolicy(data, slots)
        assert policy.num_checks == 2

        rng = random.Random(5678)
        for _ in range(5000):
            margin = round(rng.uniform(-10, 50), 2)
            net_worth = round(rng.uniform(-2, 15), 2)
            doc = {"entity_type": "Stockbroker", "facts": {"upfront_margin_pct": margin, "net_worth_inr_crore": net_worth}}
            assert policy.evaluate_facts(doc) == bool(evaluate_jsonlogic(compiled.logic, doc)), doc

    def test_missing_fact_denies_matching_undefined_semantics(self):
        """app.backtest.jsonlogic_evaluator raises MissingFactError for a
        genuinely absent fact, which app.backtest.candidate_evaluator maps
        to FLAGGED/undefined, never an implicit allow. The native engine
        has no exception channel on its hot path, so it maps the same
        "fact not supplied" condition directly to DENY -- the safe side
        of that same undefined-input contract (app.compiler.rego_compiler's
        module docstring: "missing data denies, never silently permits")."""
        rule = _margin_rule()
        compiled = compile_rule_to_jsonlogic(rule)
        data, slots = pack_policy(rule.rule_id, compiled.logic)
        policy = regengine_native.CompiledPolicy(data, slots)

        assert policy.evaluate_facts({"entity_type": "Stockbroker", "facts": {}}) is False

    def test_direct_evaluate_matches_evaluate_facts_when_pre_resolved(self):
        rule = _margin_rule()
        compiled = compile_rule_to_jsonlogic(rule)
        data, slots = pack_policy(rule.rule_id, compiled.logic)
        policy = regengine_native.CompiledPolicy(data, slots)

        values = [0.0] * len(slots)
        values[slots["upfront_margin_pct"]] = 25.0
        entity_hash = regengine_native.hash_entity_type("Stockbroker")

        assert policy.evaluate(values, entity_hash) is True
        assert policy.evaluate_facts({"entity_type": "Stockbroker", "facts": {"upfront_margin_pct": 25.0}}) is True
