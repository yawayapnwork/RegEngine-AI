"""Compiles ExtractedComplianceRule.deterministic_logic into a JSON-Logic AST
— the fallback representation for microservices that don't run OPA (a Node
service using `json-logic-js`, a Java service using `json-logic-java`, a
simple rules-engine microservice evaluating with `jsonlogic` in Python, etc).

The generated `logic` node uses the exact same `input.facts.*` field-naming
convention as the Rego compiler (`naming.metric_field_name`), so a fact
payload built once can be evaluated against either backend interchangeably.

JSON-Logic has no string-formatting primitive, so unlike Rego we cannot bake
a rendered violation message into the AST itself. Instead we ship a
`violation_message_template` (a plain `str.format()` template) that the
calling service renders locally using the same `facts` dict it fed to the
evaluator, once `logic` comes back false.
"""
from __future__ import annotations

from app.agents.schemas import ComparisonOperator, ExtractedComplianceRule, NumericalThreshold
from app.compiler.models import JsonLogicRule
from app.compiler.naming import metric_field_name


def _var(path: str) -> dict:
    return {"var": path}


def _threshold_var_path(threshold: NumericalThreshold) -> str:
    return f"facts.{metric_field_name(threshold.metric, threshold.unit)}"


def _threshold_to_logic(threshold: NumericalThreshold) -> dict:
    path = _threshold_var_path(threshold)
    if threshold.operator == ComparisonOperator.RANGE:
        assert threshold.value_upper is not None
        return {
            "and": [
                {">=": [_var(path), threshold.value]},
                {"<=": [_var(path), threshold.value_upper]},
            ]
        }
    return {threshold.operator.value: [_var(path), threshold.value]}


def _entity_logic(rule: ExtractedComplianceRule) -> dict | None:
    entities = sorted({e.normalized_entity or e.raw_text for e in rule.target_entities if (e.normalized_entity or e.raw_text)})
    if not entities:
        return None
    if len(entities) == 1:
        return {"==": [_var("entity_type"), entities[0]]}
    return {"in": [_var("entity_type"), entities]}


def _data_schema(rule: ExtractedComplianceRule) -> dict[str, str]:
    schema = {"entity_type": "string"}
    for t in rule.deterministic_logic:
        schema[_threshold_var_path(t)] = "number"
    return schema


def _message_template(rule: ExtractedComplianceRule) -> str:
    clause = rule.clause_number or "unscoped"
    parts: list[str] = []
    for t in rule.deterministic_logic:
        path = _threshold_var_path(t)
        placeholder = "{" + path + "}"
        scope = f" for {t.applies_to}" if t.applies_to else ""
        if t.operator == ComparisonOperator.RANGE:
            parts.append(
                f"{t.metric} is {placeholder} {t.unit}, required to be between "
                f"{t.value} and {t.value_upper} {t.unit}{scope} (clause {clause})"
            )
        else:
            parts.append(
                f"{t.metric} is {placeholder} {t.unit}, required to be {t.operator.value} "
                f"{t.value} {t.unit}{scope} (clause {clause})"
            )
    return " AND ".join(parts)


def compile_rule_to_jsonlogic(rule: ExtractedComplianceRule) -> JsonLogicRule:
    """Compile all NumericalThreshold entries of a rule into a single
    JSON-Logic AST. `logic` evaluates truthy when the rule is SATISFIED
    (i.e. compliant) — invert the result to detect a violation, mirroring
    Rego's `allow` semantics rather than `violation` semantics, so callers
    integrating both backends share one truth-table convention."""
    if not rule.deterministic_logic:
        raise ValueError(f"Rule {rule.rule_id} has no deterministic_logic to compile.")

    threshold_clauses = [_threshold_to_logic(t) for t in rule.deterministic_logic]
    entity_clause = _entity_logic(rule)

    all_clauses = ([entity_clause] if entity_clause else []) + threshold_clauses
    logic = all_clauses[0] if len(all_clauses) == 1 else {"and": all_clauses}

    return JsonLogicRule(
        rule_id=rule.rule_id,
        logic=logic,
        data_schema=_data_schema(rule),
        violation_message_template=_message_template(rule),
        thresholds_compiled=len(rule.deterministic_logic),
    )
