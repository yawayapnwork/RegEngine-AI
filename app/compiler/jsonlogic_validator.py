"""Structural validation for a compiled JSON-Logic AST
(app.compiler.models.JsonLogicRule.logic) before it is ever handed to a
downstream microservice's json-logic evaluator or persisted.

A JSON-Logic node (per the json-logic-js/json-logic-py spec this
compiler's output must interoperate with) is exactly one of:
  - a literal: str, int, float, bool, None
  - a list of nodes (an operator's argument array)
  - an operator object: a dict with EXACTLY ONE key (the operator name,
    e.g. "==", "and", "var"), whose value is itself a node (almost always
    a list of argument nodes).

A dict with zero keys or more than one key is not valid JSON-Logic -- most
real-world evaluators either raise or silently pick one key, neither of
which is a safe way to encode a compliance rule. This is a compiler-bug
detector, not a data-quality check: `app.compiler.jsonlogic_compiler`
should never actually produce a malformed node, so tripping this
validator means the compiler itself has a bug in a threshold/operator
combination it hasn't seen before -- exactly the "malformed JSON AST
output" case app.resilience.exceptions.MalformedASTError exists for.
"""
from __future__ import annotations

import json

from app.resilience.exceptions import MalformedASTError

_LITERAL_TYPES = (str, int, float, bool, type(None))


def validate_json_logic_ast(node: object, *, _path: str = "$") -> None:
    """Raises MalformedASTError with the exact failing path (e.g.
    "$.and[1]") if `node` (or anything nested inside it) is not valid
    JSON-Logic. Returns None on success."""
    if isinstance(node, _LITERAL_TYPES):
        return

    if isinstance(node, list):
        for i, item in enumerate(node):
            validate_json_logic_ast(item, _path=f"{_path}[{i}]")
        return

    if isinstance(node, dict):
        if len(node) != 1:
            raise MalformedASTError(
                f"Invalid JSON-Logic node at {_path}: operator objects must have exactly one key, "
                f"got {len(node)} ({sorted(node.keys())!r})."
            )
        (operator, operand), = node.items()
        if not isinstance(operator, str) or not operator:
            raise MalformedASTError(f"Invalid JSON-Logic node at {_path}: operator key must be a non-empty string, got {operator!r}.")
        validate_json_logic_ast(operand, _path=f"{_path}.{operator}")
        return

    raise MalformedASTError(f"Invalid JSON-Logic node at {_path}: {type(node).__name__} is not a valid node type ({node!r}).")


def validate_json_serializable(node: object, *, context: str = "JSON-Logic AST") -> None:
    """A second, independent check: even a structurally valid node (per
    validate_json_logic_ast above) could contain a value Python's json
    module can't round-trip (NaN/Infinity floats serialize under Python's
    default encoder but are NOT valid JSON per RFC 8259, and silently
    break strict-mode parsers on the receiving end -- exactly the kind of
    "looks fine, fails downstream" bug this whole module exists to catch
    before it ships)."""
    try:
        serialized = json.dumps(node, allow_nan=False)
        json.loads(serialized)
    except (TypeError, ValueError) as exc:
        raise MalformedASTError(f"{context} is not valid JSON: {exc}") from exc
