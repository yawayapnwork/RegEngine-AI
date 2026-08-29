"""Minimal, dependency-free JSON-Logic evaluator for backtesting a
candidate policy WITHOUT touching any OPA server (production or
otherwise) -- see app.backtest.candidate_evaluator's module docstring for
why this is the default, safest replay path.

Deliberately NOT a general-purpose JSON-Logic implementation: it supports
EXACTLY the node shapes `app.compiler.jsonlogic_compiler` ever emits
(`var`, `and`, `==`, `in`, `>=`, `>`, `<=`, `<`), enumerated from that
module's `_threshold_to_logic`/`_entity_logic` functions. A node shape
outside that set raises `UnsupportedJsonLogicNodeError` rather than
guessing at semantics a general json-logic library might interpret
differently (e.g. `or`, `!`, string operators) -- this evaluator's whole
purpose is bit-for-bit fidelity with what the compiler actually generates
and what OPA would actually evaluate, not general JSON-Logic coverage.
"""
from __future__ import annotations

from typing import Any

_COMPARISON_OPS = {
    "==": lambda a, b: a == b,
    ">=": lambda a, b: a >= b,
    ">": lambda a, b: a > b,
    "<=": lambda a, b: a <= b,
    "<": lambda a, b: a < b,
}


class UnsupportedJsonLogicNodeError(ValueError):
    pass


class MissingFactError(KeyError):
    """Raised when a `{"var": path}` node references a fact key absent
    from the input document -- mirrors OPA's own "undefined" semantics
    for a missing `input.facts.*` reference (app.execution.opa_engine's
    module docstring: "None means OPA reports the result as undefined").
    The caller (app.backtest.candidate_evaluator) maps this to a FLAGGED/
    undefined outcome, never to an implicit True or False."""


def _resolve_var(path: str, data: dict[str, Any]) -> Any:
    node: Any = data
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            raise MissingFactError(path)
        node = node[part]
    return node


def evaluate_jsonlogic(node: Any, data: dict[str, Any]) -> Any:
    """`data` is the same `{"entity_type": ..., "facts": {...}}` shape
    OPA's `input` document uses (app.execution.opa_engine's module
    docstring), so a `JsonLogicRule` and a compiled Rego module are
    interchangeable given identical input -- exactly the guarantee
    app.compiler.jsonlogic_compiler's module docstring promises."""
    if isinstance(node, (bool, int, float, str)) or node is None:
        return node
    if isinstance(node, list):
        return [evaluate_jsonlogic(item, data) for item in node]
    if not isinstance(node, dict):
        raise UnsupportedJsonLogicNodeError(f"Unrecognized JSON-Logic node: {node!r}")
    if len(node) != 1:
        raise UnsupportedJsonLogicNodeError(f"Expected exactly one operator key, got: {list(node.keys())}")

    (operator, operands), = node.items()

    if operator == "var":
        return _resolve_var(operands, data)

    if operator == "and":
        if not isinstance(operands, list):
            raise UnsupportedJsonLogicNodeError("'and' operands must be a list.")
        return all(evaluate_jsonlogic(child, data) for child in operands)

    if operator == "in":
        if not (isinstance(operands, list) and len(operands) == 2):
            raise UnsupportedJsonLogicNodeError("'in' requires exactly two operands: [needle, haystack].")
        needle = evaluate_jsonlogic(operands[0], data)
        haystack = evaluate_jsonlogic(operands[1], data)
        return needle in haystack

    if operator in _COMPARISON_OPS:
        if not (isinstance(operands, list) and len(operands) == 2):
            raise UnsupportedJsonLogicNodeError(f"'{operator}' requires exactly two operands.")
        left = evaluate_jsonlogic(operands[0], data)
        right = evaluate_jsonlogic(operands[1], data)
        return _COMPARISON_OPS[operator](left, right)

    raise UnsupportedJsonLogicNodeError(f"Unsupported JSON-Logic operator: {operator!r}")
