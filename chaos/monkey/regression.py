"""Golden-fixture regression check for a compiled JSON-Logic AST -- the
defense that actually matters for `chaos.monkey.mutators.corrupt_compiled_jsonlogic_operators`:
a post-compile bit-flip never passed through the extraction/audit
pipeline at all, so `chaos.monkey.fidelity_check` (which only ever sees
`verbatim_evidence`) cannot catch it. What can is the same idea a
pre-publish CI gate for compiled policies should already run: replay a
handful of known input/expected-outcome fixtures through the policy
before it's trusted, using `app.backtest.jsonlogic_evaluator.evaluate_jsonlogic`
-- the same dependency-free evaluator app.backtest uses to replay real
historical transactions, so a fixture "pass" here means the exact same
evaluation OPA or app.backtest would perform is also being exercised.

A single well-chosen boundary fixture (facts value sitting between the
original threshold and nothing) is enough: `>=` and `<=` disagree on
every value except the threshold itself, so any operator-family flip
`chaos.monkey.mutators` performs necessarily flips this fixture's
outcome too.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.agents.schemas import ComparisonOperator, NumericalThreshold
from app.backtest.jsonlogic_evaluator import evaluate_jsonlogic
from app.compiler.naming import metric_field_name


@dataclass(frozen=True)
class GoldenFixture:
    description: str
    facts: dict[str, float]
    expect_compliant: bool


@dataclass(frozen=True)
class RegressionCheckResult:
    fixture: GoldenFixture
    original_result: bool
    mutated_result: bool
    original_matches_expected: bool
    regression_detected: bool  # True iff original and mutated logic disagree on this fixture


def build_boundary_fixture(threshold: NumericalThreshold) -> GoldenFixture:
    """One fact value just past the threshold on the "still compliant"
    side, e.g. for `Upfront Margin >= 20%` -> `upfront_margin_pct = 21`,
    `expect_compliant=True`. `>=`'s violation-negation counterpart
    (`app.compiler.rego_compiler._OPERATOR_NEGATION`) and `<=` disagree
    on every such value, so a family-flip mutation is guaranteed to be
    visible here."""
    field = metric_field_name(threshold.metric, threshold.unit)
    epsilon = max(abs(threshold.value) * 0.01, 0.01)

    if threshold.operator in (ComparisonOperator.GTE, ComparisonOperator.GT):
        value = threshold.value + epsilon
        expect_compliant = True
    elif threshold.operator in (ComparisonOperator.LTE, ComparisonOperator.LT):
        value = threshold.value - epsilon
        expect_compliant = True
    else:
        raise ValueError(f"No boundary-fixture rule for operator {threshold.operator!r}.")

    return GoldenFixture(
        description=f"{threshold.metric} = {value}{threshold.unit} (just past the compliant side of the original threshold)",
        facts={field: value},
        expect_compliant=expect_compliant,
    )


def run_regression_check(
    original_logic: dict,
    mutated_logic: dict,
    fixture: GoldenFixture,
    entity_type: str = "Stockbroker",
) -> RegressionCheckResult:
    data = {"entity_type": entity_type, "facts": fixture.facts}
    original_result = bool(evaluate_jsonlogic(original_logic, data))
    mutated_result = bool(evaluate_jsonlogic(mutated_logic, data))

    return RegressionCheckResult(
        fixture=fixture,
        original_result=original_result,
        mutated_result=mutated_result,
        original_matches_expected=original_result == fixture.expect_compliant,
        regression_detected=original_result != mutated_result,
    )
