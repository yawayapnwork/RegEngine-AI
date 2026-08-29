"""Pure functions for extracting comparable (field, operator, value) facts
out of a persisted JSON-Logic AST (`app.db.models.CompiledRule.jsonlogic_ast`,
produced by `app.compiler.jsonlogic_compiler.compile_rule_to_jsonlogic`).

This is what lets the diff engine compare a NEW rule's
`ExtractedComplianceRule.deterministic_logic` (fresh Pydantic objects, not
yet compiled) against the OLD rule's already-compiled threshold -- the
relational schema deliberately does not persist the historical
`ExtractedComplianceRule` JSON (see app.regulatory.taxonomy's sibling
discussion in app.agents.schemas), but it DOES persist `jsonlogic_ast`,
which contains everything needed to recover the old numeric thresholds.

No I/O, no imports from app.agents/app.compiler beyond pure data shapes --
kept trivially unit-testable in isolation, mirroring
app.ledger.hash_chain's "pure primitives, no DB" design principle.
"""
from __future__ import annotations

from dataclasses import dataclass

_COMPARISON_OPERATORS = {">=", ">", "<=", "<", "=="}


@dataclass(frozen=True)
class ExtractedThreshold:
    field: str          # e.g. "facts.upfront_margin_pct"
    operator: str       # ">=" | ">" | "<=" | "<" | "==" | "range"
    value: float
    value_upper: float | None = None  # set only when operator == "range"


def _var_path(node: dict) -> str | None:
    if isinstance(node, dict) and "var" in node:
        var = node["var"]
        return var if isinstance(var, str) else None
    return None


def _flatten_and_leaves(node: object) -> list[dict]:
    """Recursively flattens arbitrarily-nested `{"and": [...]}` wrappers
    into a flat list of leaf comparison nodes. compile_rule_to_jsonlogic
    nests an `and` inside another `and` whenever both an entity guard AND
    a RANGE threshold are present in the same rule (the range threshold's
    own `{"and": [gte, lte]}` becomes one child of the outer entity+
    thresholds `and`) -- a single-level scan would silently miss the
    range's two leaves entirely, which is why this recurses rather than
    only checking `ast["and"]`'s immediate children."""
    if isinstance(node, dict) and "and" in node and isinstance(node["and"], list):
        leaves: list[dict] = []
        for child in node["and"]:
            leaves.extend(_flatten_and_leaves(child))
        return leaves
    return [node] if isinstance(node, dict) else []


def extract_thresholds_from_jsonlogic(ast: dict) -> list[ExtractedThreshold]:
    """Walks a JSON-Logic AST of the exact shape
    app.compiler.jsonlogic_compiler.compile_rule_to_jsonlogic produces and
    returns every `(field, operator, value)` it can recognize.

    A RANGE threshold compiles to `{">=" : [var, lo]}` and `{"<=": [var,
    hi]}` on the SAME var, possibly several `and`-levels deep (see
    `_flatten_and_leaves`) -- this function groups flattened leaves by
    field and folds a `{">=", "<="}` pair on the same field back into one
    `ExtractedThreshold(operator="range", value=lo, value_upper=hi)`
    rather than reporting two independent thresholds, so a round-trip
    through compile -> extract recovers the original NumericalThreshold
    shape.

    Any node this function doesn't recognize (a future compiler feature,
    a hand-written policy with unusual structure) is silently skipped,
    never raised -- an incomplete historical comparison is far better
    than a crashed diff report."""
    if not isinstance(ast, dict):
        return []

    by_field: dict[str, list[tuple[str, float]]] = {}
    for leaf in _flatten_and_leaves(ast):
        for op, operands in _single_comparisons(leaf):
            if not (isinstance(operands, list) and len(operands) == 2):
                continue
            field = _var_path(operands[0])
            value = operands[1]
            if field is None or field == "entity_type" or not isinstance(value, (int, float)):
                continue
            by_field.setdefault(field, []).append((op, float(value)))

    result: list[ExtractedThreshold] = []
    for field, pairs in by_field.items():
        ops = {op for op, _ in pairs}
        if ops == {">=", "<="} and len(pairs) == 2:
            lo = next(v for op, v in pairs if op == ">=")
            hi = next(v for op, v in pairs if op == "<=")
            result.append(ExtractedThreshold(field=field, operator="range", value=lo, value_upper=hi))
        else:
            for op, value in pairs:
                if op in _COMPARISON_OPERATORS:
                    result.append(ExtractedThreshold(field=field, operator=op, value=value))
    return result


def _single_comparisons(node: object) -> list[tuple[str, object]]:
    if not isinstance(node, dict) or len(node) != 1:
        return []
    (op, operands), = node.items()
    if op in _COMPARISON_OPERATORS:
        return [(op, operands)]
    return []
