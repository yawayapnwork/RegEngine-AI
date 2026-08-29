"""Parses OPA violation message strings back into structured facts.

The messages parsed here are NOT free-form LLM prose -- they are
generated deterministically by `app.compiler.rego_compiler._violation_clauses`
via Rego's `sprintf`, from the exact same `NumericalThreshold` fields this
module recovers. That means parsing them back is a structural inverse of
a known, versioned template (three shapes: a plain comparison failure, a
range-below-minimum, and a range-above-maximum), not a fuzzy NLP task --
if a future compiler version changes the template, `parse_violation`
returns `None` for that shape rather than a wrong structured guess, and
the caller (app.explainability.explainer) falls through to the LLM path.
"""
from __future__ import annotations

import re

from app.explainability.models import StructuredViolation

_NUM = r"-?\d+(?:\.\d+)?"

# "{metric} is {value} {unit}, which fails the required condition
#  ({operator} {required} {unit}[ for {applies_to}], clause {clause})"
_RE_CONDITION_FAIL = re.compile(
    rf"^(?P<metric>.+?) is (?P<value>{_NUM}) (?P<unit>.+?), "
    rf"which fails the required condition \("
    rf"(?P<operator>>=|<=|==|>|<) (?P<required>{_NUM}) (?P<req_unit>.+?)"
    rf"(?: for (?P<applies_to>.+?))?"
    rf", clause (?P<clause>.+?)\)$"
)

# "{metric} is {value} {unit}, below the required minimum of {required}
#  {unit}[ for {applies_to}] (clause {clause})"
_RE_RANGE_LOW = re.compile(
    rf"^(?P<metric>.+?) is (?P<value>{_NUM}) (?P<unit>.+?), "
    rf"below the required minimum of (?P<required>{_NUM}) (?P<req_unit>.+?)"
    rf"(?: for (?P<applies_to>.+?))?"
    rf" \(clause (?P<clause>.+?)\)$"
)

# "{metric} is {value} {unit}, above the allowed maximum of {required}
#  {unit}[ for {applies_to}] (clause {clause})"
_RE_RANGE_HIGH = re.compile(
    rf"^(?P<metric>.+?) is (?P<value>{_NUM}) (?P<unit>.+?), "
    rf"above the allowed maximum of (?P<required>{_NUM}) (?P<req_unit>.+?)"
    rf"(?: for (?P<applies_to>.+?))?"
    rf" \(clause (?P<clause>.+?)\)$"
)

_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (_RE_CONDITION_FAIL, "condition_fail"),
    (_RE_RANGE_LOW, "range_low"),
    (_RE_RANGE_HIGH, "range_high"),
)


def parse_violation(
    raw_text: str,
    *,
    rule_id: str,
    circular_number: str | None,
    clause_number: str | None,
    regulator: str = "sebi",
) -> StructuredViolation | None:
    """Returns a `StructuredViolation` if `raw_text` matches one of the
    three known compiler-generated shapes, else `None`. `clause_number`
    (from the OPA decision object, i.e. ground truth) always wins over
    whatever clause substring the regex captured from the message text
    itself -- the message's embedded clause is redundant with it by
    construction, and trusting the decision object's own field is one
    fewer thing that could disagree."""
    for pattern, kind in _PATTERNS:
        m = pattern.match(raw_text.strip())
        if not m:
            continue
        groups = m.groupdict()

        operator = groups.get("operator")
        if kind == "range_low":
            operator = "range_low"
        elif kind == "range_high":
            operator = "range_high"

        try:
            observed_value = float(groups["value"])
            required_value = float(groups["required"])
        except (TypeError, ValueError):
            continue

        return StructuredViolation(
            rule_id=rule_id,
            circular_number=circular_number,
            clause_number=clause_number or groups.get("clause"),
            regulator=regulator,
            metric=groups["metric"].strip(),
            observed_value=observed_value,
            unit=groups["unit"].strip(),
            operator=operator,
            required_value=required_value,
            applies_to=groups.get("applies_to"),
            raw_violation_text=raw_text,
        )
    return None
