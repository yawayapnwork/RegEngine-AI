"""Compiles ExtractedComplianceRule.deterministic_logic into production-grade
OPA Rego modules.

Design / input contract
------------------------
Every generated module assumes evaluation-time input of the shape:

    {
        "entity_type": "Stockbroker",
        "facts": {
            "upfront_margin_pct": 18.5,
            ...
        }
    }

`facts` keys are derived deterministically from each threshold's
`metric` + `unit` via `naming.metric_field_name`, so the caller building the
`input` document only needs the same slugging convention, not this module.

Generated structure (modern Rego, `import rego.v1`):

    package sebi.circulars.<circular>.clause_<clause>

    import rego.v1

    default allow := false

    entity_matches if { input.entity_type == "Stockbroker" }

    cond_0 if { input.facts.upfront_margin_pct >= 20 }

    allow if {
        entity_matches
        cond_0
    }

    violation contains msg if {
        entity_matches
        input.facts.upfront_margin_pct < 20
        msg := sprintf("...", [input.facts.upfront_margin_pct])
    }

    deny := violation

    decision := {"allow": allow, "violations": violation, "rule_id": "...", ...}

One threshold with a missing `facts` key simply makes `cond_N` undefined,
which makes `allow` undefined (safe-by-default: OPA's `default allow := false`
means missing data denies, never silently permits).
"""
from __future__ import annotations

import datetime as dt
import json

from app.agents.schemas import ComparisonOperator, ExtractedComplianceRule, NumericalThreshold
from app.compiler.models import CompiledRego
from app.compiler.naming import clause_slug, circular_slug, metric_field_name, rego_package_name

_INDENT = "    "

# (pass_operator, fail_operator) — fail is the logical negation used to build
# the `violation` rule. Kept as an explicit table (not `not <pass>`) so the
# generated Rego reads naturally and is easy for a human reviewer to audit.
_OPERATOR_NEGATION: dict[ComparisonOperator, str] = {
    ComparisonOperator.GTE: "<",
    ComparisonOperator.GT: "<=",
    ComparisonOperator.LTE: ">",
    ComparisonOperator.LT: ">=",
    ComparisonOperator.EQ: "!=",
}


def _rego_scalar(value: float) -> str:
    # Emit integers without a trailing ".0" for readability; Rego treats both identically.
    return str(int(value)) if float(value).is_integer() else repr(float(value))


def _entity_guard(rule: ExtractedComplianceRule) -> str | None:
    entities = sorted({e.normalized_entity or e.raw_text for e in rule.target_entities if (e.normalized_entity or e.raw_text)})
    if not entities:
        return None
    if len(entities) == 1:
        return f'input.entity_type == "{entities[0]}"'
    quoted = ", ".join(f'"{e}"' for e in entities)
    return f"input.entity_type in {{{quoted}}}"


def _threshold_field(threshold: NumericalThreshold) -> str:
    return metric_field_name(threshold.metric, threshold.unit)


def _pass_condition(threshold: NumericalThreshold) -> str:
    field = f"input.facts.{_threshold_field(threshold)}"
    if threshold.operator == ComparisonOperator.RANGE:
        assert threshold.value_upper is not None
        return f"{field} >= {_rego_scalar(threshold.value)}\n{_INDENT}{field} <= {_rego_scalar(threshold.value_upper)}"
    return f"{field} {threshold.operator.value} {_rego_scalar(threshold.value)}"


def _violation_clauses(threshold: NumericalThreshold, index: int) -> list[tuple[str, str]]:
    """Return [(fail_condition, message_expr), ...] — RANGE produces two
    independent violation bodies (below-min, above-max) so the emitted
    message is precise about which bound was breached."""
    field = f"input.facts.{_threshold_field(threshold)}"
    scope = f" for {threshold.applies_to}" if threshold.applies_to else ""

    if threshold.operator == ComparisonOperator.RANGE:
        assert threshold.value_upper is not None
        low_msg = (
            f'sprintf("%s is %v %s, below the required minimum of %v %s{scope} (clause {{clause}})", '
            f'["{threshold.metric}", {field}, "{threshold.unit}", {_rego_scalar(threshold.value)}, "{threshold.unit}"])'
        )
        high_msg = (
            f'sprintf("%s is %v %s, above the allowed maximum of %v %s{scope} (clause {{clause}})", '
            f'["{threshold.metric}", {field}, "{threshold.unit}", {_rego_scalar(threshold.value_upper)}, "{threshold.unit}"])'
        )
        return [
            (f"{field} < {_rego_scalar(threshold.value)}", low_msg),
            (f"{field} > {_rego_scalar(threshold.value_upper)}", high_msg),
        ]

    fail_op = _OPERATOR_NEGATION[threshold.operator]
    msg = (
        f'sprintf("%s is %v %s, which fails the required condition (%s %v %s{scope}, clause {{clause}})", '
        f'["{threshold.metric}", {field}, "{threshold.unit}", "{threshold.operator.value}", '
        f'{_rego_scalar(threshold.value)}, "{threshold.unit}"])'
    )
    return [(f"{field} {fail_op} {_rego_scalar(threshold.value)}", msg)]


def compile_rule_to_rego(rule: ExtractedComplianceRule) -> CompiledRego:
    """Compile all NumericalThreshold entries of a single ExtractedComplianceRule
    into one Rego module. Caller is responsible for having already confirmed
    (via the HITL module) that this rule is safe to compile — this function
    does not itself judge qualitative content."""
    if not rule.deterministic_logic:
        raise ValueError(f"Rule {rule.rule_id} has no deterministic_logic to compile.")

    package = rego_package_name(rule.circular_number, rule.clause_number)
    clause = rule.clause_number or "unscoped"
    entity_guard = _entity_guard(rule)

    lines: list[str] = []

    # --- METADATA annotation block (OPA-native structured metadata) ---
    title = (
        f"{rule.deterministic_logic[0].metric} Compliance Rule"
        if len(rule.deterministic_logic) == 1
        else f"Multi-Condition Compliance Rule ({', '.join(t.metric for t in rule.deterministic_logic)})"
    )
    lines.append("# METADATA")
    lines.append(f"# title: {title}")
    lines.append(f"# description: Auto-compiled from SEBI clause {clause}")
    lines.append("# custom:")
    lines.append(f"#   rule_id: {rule.rule_id}")
    lines.append(f"#   clause_number: {clause}")
    lines.append(f"#   circular_number: {rule.circular_number or 'unknown'}")
    lines.append(f"#   source_sha256: {rule.source_sha256}")
    lines.append(f"#   obligation_type: {rule.obligation_type.value}")
    lines.append(f"#   generated_at: {dt.datetime.utcnow().isoformat()}Z")
    lines.append(f"#   compiler: sebi-rego-compiler/1.0.0")
    lines.append(f"package {package}")
    lines.append("")
    lines.append("import rego.v1")
    lines.append("")
    lines.append("default allow := false")
    lines.append("")

    if entity_guard is not None:
        lines.append(f"entity_matches if {{ {entity_guard} }}")
        lines.append("")

    cond_names: list[str] = []
    for i, threshold in enumerate(rule.deterministic_logic):
        cond_name = f"cond_{i}"
        cond_names.append(cond_name)
        lines.append(f"# {threshold.metric} {threshold.operator.value} {threshold.value}{threshold.unit}")
        lines.append(f"{cond_name} if {{")
        for sub_line in _pass_condition(threshold).splitlines():
            lines.append(f"{_INDENT}{sub_line}")
        lines.append("}")
        lines.append("")

    lines.append("allow if {")
    if entity_guard is not None:
        lines.append(f"{_INDENT}entity_matches")
    for cond_name in cond_names:
        lines.append(f"{_INDENT}{cond_name}")
    lines.append("}")
    lines.append("")

    for i, threshold in enumerate(rule.deterministic_logic):
        for fail_condition, msg_expr in _violation_clauses(threshold, i):
            msg_expr = msg_expr.replace("{clause}", clause)
            lines.append("violation contains msg if {")
            if entity_guard is not None:
                lines.append(f"{_INDENT}entity_matches")
            lines.append(f"{_INDENT}{fail_condition}")
            lines.append(f"{_INDENT}msg := {msg_expr}")
            lines.append("}")
            lines.append("")

    lines.append("deny := violation")
    lines.append("")
    lines.append("decision := {")
    lines.append(f'{_INDENT}"allow": allow,')
    lines.append(f'{_INDENT}"violations": violation,')
    lines.append(f'{_INDENT}"rule_id": "{rule.rule_id}",')
    lines.append(f'{_INDENT}"clause_number": "{clause}",')
    lines.append(f'{_INDENT}"circular_number": {json.dumps(rule.circular_number)},')
    lines.append(f'{_INDENT}"obligation_type": "{rule.obligation_type.value}",')
    lines.append("}")
    lines.append("")

    rego_code = "\n".join(lines)

    return CompiledRego(
        rule_id=rule.rule_id,
        package=package,
        rego_code=rego_code,
        thresholds_compiled=len(rule.deterministic_logic),
    )
