"""Scenario 1 fault injection: subtle logical-error mutations, at both
the levels a real corruption could enter the system --

  * source-level: `ExtractedComplianceRule.deterministic_logic[i].operator`
    flipped (e.g. GTE -> LTE) while `verbatim_evidence` is left untouched
    -- this is what a bit-flip, a bad manual edit, or an extraction-agent
    hallucination that the auditor missed would actually look like: the
    recorded operator no longer matches what the source text says.
  * compiled-AST level: the already-compiled JSON-Logic `logic` dict
    (app.compiler.jsonlogic_compiler's output -- literally an AST) has
    its comparison operator keys swapped in place, simulating corruption
    introduced AFTER compilation (a storage bit-flip, a bad hot-reload
    payload) that the extraction/audit pipeline never had a chance to
    see at all.

Both mutations are semantically inverting but structurally valid, which
is the whole point: neither `app.compiler.jsonlogic_validator` (checks
shape, not meaning) nor OPA itself would reject either one. Catching
them requires the checks in chaos.monkey.fidelity_check and
chaos.monkey.regression, not structural validation -- see
chaos/monkey/validators.py for how each scenario proves that.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.agents.schemas import AuditedComplianceRule, ComparisonOperator, ExtractedComplianceRule, NumericalThreshold

_OPERATOR_FLIP: dict[ComparisonOperator, ComparisonOperator] = {
    ComparisonOperator.GTE: ComparisonOperator.LTE,
    ComparisonOperator.LTE: ComparisonOperator.GTE,
    ComparisonOperator.GT: ComparisonOperator.LT,
    ComparisonOperator.LT: ComparisonOperator.GT,
}

_JSONLOGIC_OPERATOR_FLIP: dict[str, str] = {">=": "<=", "<=": ">=", ">": "<", "<": ">"}


class UnflippableOperatorError(ValueError):
    """Raised when asked to flip an operator (EQ, RANGE) this chaos
    scenario has no defined inversion for -- those need a different
    mutation shape (e.g. widening/narrowing a range) that this module
    deliberately doesn't attempt, to keep the injected fault the exact
    one Requirement 1 names (`>=` <-> `<=`, and its `>`/`<` analogue)."""


@dataclass(frozen=True)
class OperatorFlipMutation:
    """Records what was changed, so validators and the post-mortem can
    report the exact before/after without re-deriving it."""

    threshold_index: int
    metric: str
    original_operator: ComparisonOperator
    mutated_operator: ComparisonOperator
    verbatim_evidence: str


def flip_threshold_operator(rule: ExtractedComplianceRule, threshold_index: int = 0) -> tuple[ExtractedComplianceRule, OperatorFlipMutation]:
    """Returns a mutated COPY of `rule` with one threshold's operator
    flipped -- the original `rule` (and its `deterministic_logic` list)
    are never modified in place, so a chaos run can always compare
    mutant against original."""
    if not rule.deterministic_logic:
        raise ValueError(f"Rule {rule.rule_id} has no deterministic_logic to mutate.")
    threshold = rule.deterministic_logic[threshold_index]
    try:
        flipped = _OPERATOR_FLIP[threshold.operator]
    except KeyError as exc:
        raise UnflippableOperatorError(f"No defined flip for operator {threshold.operator!r}.") from exc

    new_thresholds = list(rule.deterministic_logic)
    new_thresholds[threshold_index] = threshold.model_copy(update={"operator": flipped})
    mutated_rule = rule.model_copy(update={"deterministic_logic": new_thresholds})

    mutation = OperatorFlipMutation(
        threshold_index=threshold_index,
        metric=threshold.metric,
        original_operator=threshold.operator,
        mutated_operator=flipped,
        verbatim_evidence=threshold.verbatim_evidence,
    )
    return mutated_rule, mutation


def flip_audited_rule_operator(audited: AuditedComplianceRule, threshold_index: int = 0) -> tuple[AuditedComplianceRule, OperatorFlipMutation]:
    """Same mutation, applied to a full `AuditedComplianceRule` (rule +
    its prior audit) -- the audit itself is left untouched, since the
    whole point of this scenario is asking "if the LOGIC changes after
    the audit approved it, does anything downstream still catch that?"."""
    mutated_rule, mutation = flip_threshold_operator(audited.rule, threshold_index)
    return audited.model_copy(update={"rule": mutated_rule}), mutation


def corrupt_compiled_jsonlogic_operators(logic: dict) -> dict:
    """Recursively swaps every comparison-operator key
    (`>=`/`<=`/`>`/`<`) found in a compiled JSON-Logic AST dict (as
    produced by `app.compiler.jsonlogic_compiler.compile_rule_to_jsonlogic`),
    returning a NEW dict -- `logic` itself is never mutated in place.
    Structurally identical to the input (same shape, same operand
    lists), which is exactly why `app.compiler.jsonlogic_validator`
    cannot tell the two apart -- see this module's docstring."""

    def _walk(node):
        if isinstance(node, dict):
            if len(node) == 1:
                (op, operands), = node.items()
                if op in _JSONLOGIC_OPERATOR_FLIP:
                    return {_JSONLOGIC_OPERATOR_FLIP[op]: _walk(operands)}
                return {op: _walk(operands)}
            return {k: _walk(v) for k, v in node.items()}
        if isinstance(node, list):
            return [_walk(item) for item in node]
        return node

    return _walk(logic)
