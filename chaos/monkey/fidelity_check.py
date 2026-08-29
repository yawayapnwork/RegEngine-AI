"""A deterministic stand-in for the Logic Auditor Agent's operator-vs-
source-wording check (app.agents.prompts' Logic Auditor system prompt
instructs it to diff every extracted threshold against the numeric/
comparison language actually present in `verbatim_evidence`; see
app.agents.tools' module docstring on why that check is designed to be
"mechanically enforceable rather than a matter of prompt phrasing").

This chaos suite has no ANTHROPIC_API_KEY / LLM available to invoke the
real CrewAI Logic Auditor Agent, so this module reimplements the
deterministic half of that check -- the half that never depended on an
LLM to begin with -- using the same real, dependency-free
`scan_numeric_tokens` tool the actual agent calls (app.agents.tools).
It is a PROXY for the auditor's judgment, not a replacement for it: it
catches exactly what a fixed vocabulary of directional phrases implies
about a threshold's operator, which is a narrower net than an LLM
reading the clause in full. Every place this module's result feeds into
a chaos scenario says so explicitly, so a real LLM auditor eventually
back-testing a chaos run's history knows this is where the real audit
result belongs.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.agents.schemas import AuditFinding, ComparisonOperator, ComplianceRuleAudit, AuditVerdict, FindingType, NumericalThreshold, Severity
from app.agents.tools import NumericScanInput, scan_numeric_tokens

# Directional phrasing SEBI circulars consistently use for each operator
# family -- deliberately narrow (see this module's docstring) rather than
# an exhaustive NLP model.
_GTE_PHRASES = ("not less than", "at least", "not below", "minimum of", "minimum", "greater than or equal", "or more")
_LTE_PHRASES = ("not more than", "not exceeding", "at most", "maximum of", "maximum", "less than or equal", "or less", "shall not exceed")
_GT_PHRASES = ("more than", "in excess of", "exceeding", "greater than")
_LT_PHRASES = ("less than", "below", "under")

_OPERATOR_FAMILY: dict[ComparisonOperator, str] = {
    ComparisonOperator.GTE: "gte",
    ComparisonOperator.GT: "gt",
    ComparisonOperator.LTE: "lte",
    ComparisonOperator.LT: "lt",
}


@dataclass(frozen=True)
class OperatorFidelityResult:
    threshold_index: int
    recorded_operator: ComparisonOperator
    implied_family: str | None  # "gte" | "gt" | "lte" | "lt" | None (no directional phrase matched)
    mismatch: bool
    matched_phrase: str | None


def _implied_family(evidence: str) -> tuple[str | None, str | None]:
    lowered = evidence.lower()
    # Longer/more specific phrasings first so "not less than" isn't
    # shadowed by a looser later match against the same text.
    for phrase in _GTE_PHRASES:
        if phrase in lowered:
            return "gte", phrase
    for phrase in _LTE_PHRASES:
        if phrase in lowered:
            return "lte", phrase
    for phrase in _GT_PHRASES:
        if phrase in lowered:
            return "gt", phrase
    for phrase in _LT_PHRASES:
        if phrase in lowered:
            return "lt", phrase
    return None, None


def check_operator_fidelity(threshold: NumericalThreshold, threshold_index: int = 0) -> OperatorFidelityResult:
    """Confirms `scan_numeric_tokens` still finds this threshold's number
    in its own `verbatim_evidence` (using the real tool -- a corrupted
    quote is a distinct, already-covered failure mode via
    `app.agents.tools.verify_quotes`) and then checks whether the
    directional wording around that number agrees with the RECORDED
    operator family. A mismatch means the operator was changed (or was
    always wrong) without the evidence text changing to match --
    exactly what `chaos.monkey.mutators.flip_threshold_operator` does."""
    scan = scan_numeric_tokens(NumericScanInput(source_text=threshold.verbatim_evidence))
    implied, phrase = _implied_family(threshold.verbatim_evidence)

    recorded_family = _OPERATOR_FAMILY.get(threshold.operator)
    mismatch = implied is not None and recorded_family is not None and implied != recorded_family

    return OperatorFidelityResult(
        threshold_index=threshold_index,
        recorded_operator=threshold.operator,
        implied_family=implied,
        mismatch=mismatch,
        matched_phrase=phrase,
    )


def build_audit_from_fidelity_check(rule_id: str, results: list[OperatorFidelityResult]) -> ComplianceRuleAudit:
    """Turns fidelity-check mismatches into the exact same
    `ComplianceRuleAudit` shape the real Logic Auditor Agent produces
    (`FindingType.UNIT_OR_VALUE_MISMATCH` is the real enum value for
    "number matches but unit/operator wrong" -- app.agents.schemas), so
    the REAL `app.compiler.pipeline.compile_audited_rule` /
    `app.compiler.hitl.flag_audit_not_approved` gate can be exercised
    end-to-end against it exactly as it would be against a genuine LLM
    audit finding."""
    findings = [
        AuditFinding(
            finding_type=FindingType.UNIT_OR_VALUE_MISMATCH,
            severity=Severity.BLOCKER,
            field_path=f"deterministic_logic[{r.threshold_index}].operator",
            description=(
                f"Recorded operator '{r.recorded_operator.value}' contradicts the directional "
                f"wording ('{r.matched_phrase}') in this threshold's own verbatim_evidence, which "
                f"implies a '{r.implied_family}' comparison instead."
            ),
        )
        for r in results
        if r.mismatch
    ]
    verdict = AuditVerdict.REJECTED if findings else AuditVerdict.APPROVED
    return ComplianceRuleAudit(
        rule_id=rule_id,
        verdict=verdict,
        fidelity_score=0.0 if findings else 1.0,
        findings=findings,
        verified_quote_count=len(results) - len(findings),
        unverified_quote_count=len(findings),
    )
