"""Requirement 1's "packaging" step: turns a real
`app.compiler.jsonlogic_compiler.compile_rule_to_jsonlogic` output
(a `JsonLogicRule`) into RegEngine AI's compact native policy binary
format ("RPKB1") -- the artifact `native/include/regengine/policy_loader.h`
loads on the C++ side.

This intentionally reuses the SAME restricted grammar
`app.backtest.jsonlogic_evaluator` already documents as the only shapes
`app.compiler.jsonlogic_compiler` ever emits (`var`, `and`, `==`, `>=`,
`>`, `<=`, `<`) -- packaging is a pure, mechanical AST-to-binary
flattening, never a second policy compiler with its own opinions about
what a rule means. A shape outside that set raises `UnsupportedPolicyShapeError`
rather than guessing, exactly like the Python evaluator it mirrors.

Binary format (RPKB1, little-endian, fixed-size records so the C++ side
never parses anything beyond the header):

    offset  size  field
    0       4     magic = b"RPK1"
    4       2     format_version (uint16) = 1
    6       2     rule_id_len (uint16), bytes, UTF-8, NOT null-terminated on disk
    8       4     entity_type_hash (uint32, FNV-1a of the required entity_type, 0 = none)
    12      2     num_checks (uint16)
    14      2     reserved = 0
    16      *     rule_id bytes (rule_id_len bytes, UTF-8)
    16+N    *     num_checks * 16-byte ThresholdCheck records:
                      2   field_slot (uint16)
                      1   operator (uint8: 0=GTE 1=GT 2=LTE 3=LT 4=EQ)
                      5   padding = 0
                      8   threshold (float64)
"""
from __future__ import annotations

import struct
from typing import Any

_MAGIC = b"RPK1"
_FORMAT_VERSION = 1
_HEADER = struct.Struct("<4sHHIHH")  # magic, format_version, rule_id_len, entity_type_hash, num_checks, reserved
_CHECK_RECORD = struct.Struct("<HB5xd")  # field_slot, operator, 5 pad bytes, threshold

_OPERATOR_CODES = {">=": 0, ">": 1, "<=": 2, "<": 3, "==": 4}

MAX_CHECKS_PER_POLICY = 32  # must match native/include/regengine/policy_types.h's kMaxChecksPerPolicy
MAX_FACT_SLOTS = 64  # must match kMaxFactSlots
RULE_ID_MAX_LEN = 80  # must match kRuleIdMaxLen


class UnsupportedPolicyShapeError(ValueError):
    """A JSON-Logic node outside the restricted grammar this packager
    (and the C++ loader) supports -- see this module's docstring."""


def _fnv1a(data: bytes) -> int:
    h = 0x811C9DC5
    for byte in data:
        h ^= byte
        h = (h * 0x01000193) & 0xFFFFFFFF
    return h


class _FieldSlotResolver:
    """Resolves a `facts.<field>` var path to a fixed slot index,
    assigning slots in first-seen order -- this order (and the
    resulting slot numbering) is exactly what the OMS integration's
    `FactSchema` on the C++/Python binding side must reproduce when it
    fills in `OrderFacts.values`, so it is returned to the caller
    alongside the packed bytes rather than left implicit."""

    def __init__(self) -> None:
        self.slots: dict[str, int] = {}

    def resolve(self, var_path: str) -> int:
        if not var_path.startswith("facts."):
            raise UnsupportedPolicyShapeError(f"Expected a 'facts.<field>' var path, got {var_path!r}.")
        field = var_path[len("facts.") :]
        if field not in self.slots:
            if len(self.slots) >= MAX_FACT_SLOTS:
                raise UnsupportedPolicyShapeError(f"Policy references more than {MAX_FACT_SLOTS} distinct fact fields.")
            self.slots[field] = len(self.slots)
        return self.slots[field]


def _extract_entity_type_hash(node: dict[str, Any], resolver: _FieldSlotResolver) -> tuple[int, dict | None]:
    """Recognizes `{"==": [{"var": "entity_type"}, "<value>"]}` --
    app.compiler.jsonlogic_compiler._entity_logic's single-entity shape.
    The multi-entity `{"in": [...]}` shape isn't packable into a single
    uint32 hash constraint; policies using it are left for the LLM/HITL-
    reviewed path, not this ultra-low-latency fast path -- see this
    module's docstring on staying a mechanical flattener, not a second
    compiler with new opinions."""
    if node.get("==") and isinstance(node["=="], list) and len(node["=="]) == 2:
        left, right = node["=="]
        if isinstance(left, dict) and left.get("var") == "entity_type" and isinstance(right, str):
            return _fnv1a(right.encode("utf-8")), None
    return 0, node


def _flatten_checks(node: dict[str, Any], resolver: _FieldSlotResolver, out: list[tuple[int, int, float]]) -> None:
    if "and" in node:
        for child in node["and"]:
            _flatten_checks(child, resolver, out)
        return

    for op_symbol, code in _OPERATOR_CODES.items():
        if op_symbol in node:
            operands = node[op_symbol]
            if not (isinstance(operands, list) and len(operands) == 2):
                raise UnsupportedPolicyShapeError(f"'{op_symbol}' requires exactly two operands, got {operands!r}.")
            left, right = operands
            if not (isinstance(left, dict) and "var" in left):
                raise UnsupportedPolicyShapeError(f"Left operand of '{op_symbol}' must be a {{'var': ...}} node, got {left!r}.")
            if not isinstance(right, (int, float)):
                raise UnsupportedPolicyShapeError(f"Right operand of '{op_symbol}' must be numeric, got {right!r}.")
            slot = resolver.resolve(left["var"])
            out.append((slot, code, float(right)))
            return

    raise UnsupportedPolicyShapeError(f"Unsupported JSON-Logic node for the native fast path: {node!r}")


def pack_policy(rule_id: str, logic: dict[str, Any]) -> tuple[bytes, dict[str, int]]:
    """Returns `(rpkb1_bytes, field_slots)` -- `field_slots` maps each
    `facts.<field>` name to the slot index the packed policy's checks
    reference, in the exact order the OMS-side `OrderFacts.values` array
    must be filled."""
    rule_id_bytes = rule_id.encode("utf-8")
    if len(rule_id_bytes) > RULE_ID_MAX_LEN:
        raise UnsupportedPolicyShapeError(f"rule_id {rule_id!r} ({len(rule_id_bytes)} bytes) exceeds RULE_ID_MAX_LEN={RULE_ID_MAX_LEN}.")

    resolver = _FieldSlotResolver()
    entity_type_hash, remainder = _extract_entity_type_hash(logic, resolver) if not ("and" in logic) else (0, logic)

    checks: list[tuple[int, int, float]] = []
    if "and" in logic:
        # Scan the AND's children for an entity_type equality clause
        # first (app.compiler.jsonlogic_compiler always puts it first
        # when present, but this doesn't assume that ordering).
        threshold_children = []
        for child in logic["and"]:
            h, rest = _extract_entity_type_hash(child, resolver)
            if h and rest is None:
                entity_type_hash = h
            else:
                threshold_children.append(child)
        for child in threshold_children:
            _flatten_checks(child, resolver, checks)
    elif remainder is not None:
        _flatten_checks(remainder, resolver, checks)

    if len(checks) > MAX_CHECKS_PER_POLICY:
        raise UnsupportedPolicyShapeError(f"Policy has {len(checks)} threshold checks, exceeding MAX_CHECKS_PER_POLICY={MAX_CHECKS_PER_POLICY}.")

    header = _HEADER.pack(_MAGIC, _FORMAT_VERSION, len(rule_id_bytes), entity_type_hash, len(checks), 0)
    check_bytes = b"".join(_CHECK_RECORD.pack(slot, code, value) for slot, code, value in checks)
    return header + rule_id_bytes + check_bytes, dict(resolver.slots)


def unpack_policy_header(data: bytes) -> dict[str, Any]:
    """Debug/inspection helper -- not used by the C++ loader (which
    parses the same header independently in native code), but useful
    for a Python-side sanity check or a CLI dump tool without needing
    the compiled extension built."""
    magic, version, rule_id_len, entity_type_hash, num_checks, _reserved = _HEADER.unpack_from(data, 0)
    if magic != _MAGIC:
        raise UnsupportedPolicyShapeError(f"Bad magic bytes: {magic!r}")
    rule_id = data[_HEADER.size : _HEADER.size + rule_id_len].decode("utf-8")
    return {"format_version": version, "rule_id": rule_id, "entity_type_hash": entity_type_hash, "num_checks": num_checks}
