"""Flags portions of an extracted compliance rule that cannot be safely
compiled into deterministic policy code, routing them to Human-in-the-Loop
review instead of silently dropping them or guessing at logic.

This module is the safety valve for the whole compilation pipeline: it is
called BEFORE Rego/JSON-Logic generation and again used to gate whether
generation runs at all. The guiding principle is "compile what is provably
deterministic, flag everything else" — a rule is never forced into Rego by
inventing a threshold that was not in the source text.
"""
from __future__ import annotations

import uuid

from app.agents.schemas import AuditedComplianceRule, AuditVerdict, ExtractedComplianceRule, ObligationType
from app.compiler.models import HITLFlag, HITLReasonCode, HITLSeverity

# Below this, even an "approved" extraction is routed for human sign-off
# before its Rego/JSON-Logic output is trusted in production.
LOW_CONFIDENCE_THRESHOLD = 0.75


def _new_flag_id() -> str:
    return str(uuid.uuid4())


def flag_qualitative_directives(rule: ExtractedComplianceRule) -> list[HITLFlag]:
    """Principle-based language ('adequate internal controls', 'reasonable
    efforts') is, by definition, not reducible to a deterministic threshold.
    Each one is flagged individually so a human can decide whether it
    warrants a manual policy, a checklist control, or no automation at all."""
    return [
        HITLFlag(
            flag_id=_new_flag_id(),
            rule_id=rule.rule_id,
            reason_code=HITLReasonCode.QUALITATIVE_DIRECTIVE,
            severity=HITLSeverity.ADVISORY,
            description=(
                f"Qualitative directive cannot be programmatically enforced: "
                f'"{qd.directive_text}". Requires a human-authored control (policy '
                f"attestation, manual checklist, or narrative audit procedure)."
            ),
            source_excerpt=qd.verbatim_evidence,
            field_path=f"qualitative_directives[{i}]",
        )
        for i, qd in enumerate(rule.qualitative_directives)
    ]


def flag_ambiguous_spans(rule: ExtractedComplianceRule) -> list[HITLFlag]:
    """The Extraction Agent already declined to structure these spans
    (see EXTRACTION_AGENT_SYSTEM_PROMPT rule 6) — this makes that decision
    visible and actionable to a reviewer rather than letting it silently
    vanish from the compiled output."""
    return [
        HITLFlag(
            flag_id=_new_flag_id(),
            rule_id=rule.rule_id,
            reason_code=HITLReasonCode.AMBIGUOUS_SPAN,
            severity=HITLSeverity.ADVISORY,
            description=(
                "Extraction agent flagged this span as too ambiguous to structure "
                "automatically. Review the source clause to determine whether it "
                "encodes an additional obligation, exception, or is non-normative."
            ),
            source_excerpt=span,
            field_path=f"ambiguous_spans[{i}]",
        )
        for i, span in enumerate(rule.ambiguous_spans)
    ]


def flag_low_confidence(rule: ExtractedComplianceRule) -> list[HITLFlag]:
    if rule.extraction_confidence >= LOW_CONFIDENCE_THRESHOLD:
        return []
    return [
        HITLFlag(
            flag_id=_new_flag_id(),
            rule_id=rule.rule_id,
            reason_code=HITLReasonCode.LOW_EXTRACTION_CONFIDENCE,
            severity=HITLSeverity.BLOCKING,
            description=(
                f"Extraction confidence ({rule.extraction_confidence:.2f}) is below the "
                f"{LOW_CONFIDENCE_THRESHOLD:.2f} threshold required for automated compilation. "
                "A human must confirm the extraction before any Rego/JSON-Logic output is trusted."
            ),
            field_path="extraction_confidence",
        )
    ]


def flag_unresolved_entities(rule: ExtractedComplianceRule) -> list[HITLFlag]:
    return [
        HITLFlag(
            flag_id=_new_flag_id(),
            rule_id=rule.rule_id,
            reason_code=HITLReasonCode.UNRESOLVED_ENTITY,
            severity=HITLSeverity.ADVISORY,
            description=(
                f'Entity phrase "{e.raw_text}" could not be normalized against the SEBI entity '
                "taxonomy. The compiled policy will match on this raw entity string, which may "
                "not align with how entity_type is populated in production input payloads."
            ),
            source_excerpt=e.verbatim_evidence,
            field_path=f"target_entities[{i}]",
        )
        for i, e in enumerate(rule.target_entities)
        if e.normalized_entity is None
    ]


def flag_no_deterministic_logic(rule: ExtractedComplianceRule) -> list[HITLFlag]:
    """A mandatory or prohibited obligation with zero extracted numeric
    thresholds most likely encodes a qualitative standard, a procedural
    step, or a cross-referenced condition that needs a human to design the
    enforcement mechanism — there is nothing here to compile."""
    if rule.deterministic_logic:
        return []
    if rule.obligation_type not in (ObligationType.MANDATORY, ObligationType.PROHIBITED, ObligationType.CONDITIONAL):
        return []
    return [
        HITLFlag(
            flag_id=_new_flag_id(),
            rule_id=rule.rule_id,
            reason_code=HITLReasonCode.NO_DETERMINISTIC_LOGIC,
            severity=HITLSeverity.BLOCKING,
            description=(
                f"Obligation is classified '{rule.obligation_type.value}' but no numerical "
                "thresholds were extracted. Manual authoring of the enforcement rule is required."
            ),
            field_path="deterministic_logic",
        )
    ]


def flag_conflicting_thresholds(rule: ExtractedComplianceRule) -> list[HITLFlag]:
    """Two thresholds on the same metric/field that cannot both be true
    simultaneously (e.g. 'Margin >= 20%' and 'Margin < 15%' extracted from
    the same clause) indicate an extraction error or a genuinely
    conditional rule the schema didn't capture — either way, do not compile
    silently contradictory Rego."""
    by_field: dict[str, list] = {}
    for t in rule.deterministic_logic:
        by_field.setdefault(f"{t.metric}|{t.unit}", []).append(t)

    flags: list[HITLFlag] = []
    for key, thresholds in by_field.items():
        if len(thresholds) < 2:
            continue
        lower_bounds = [t.value for t in thresholds if t.operator.value in (">=", ">")]
        upper_bounds = [t.value for t in thresholds if t.operator.value in ("<=", "<")]
        if lower_bounds and upper_bounds and min(lower_bounds) > max(upper_bounds):
            flags.append(
                HITLFlag(
                    flag_id=_new_flag_id(),
                    rule_id=rule.rule_id,
                    reason_code=HITLReasonCode.CONFLICTING_THRESHOLDS,
                    severity=HITLSeverity.BLOCKING,
                    description=(
                        f"Conflicting thresholds extracted for '{key}': lower bound(s) "
                        f"{lower_bounds} exceed upper bound(s) {upper_bounds}. This combination "
                        "can never be satisfied and likely indicates an extraction error."
                    ),
                    field_path="deterministic_logic",
                )
            )
    return flags


def flag_audit_not_approved(audited: AuditedComplianceRule) -> list[HITLFlag]:
    """Defense in depth: even though the compiler pipeline should already
    gate on audit verdict before calling this stage, an explicit flag is
    still emitted so the HITL queue itself carries the reason, independent
    of upstream call-site correctness."""
    if audited.audit.verdict == AuditVerdict.APPROVED:
        return []
    findings_summary = "; ".join(f"[{f.severity.value}] {f.finding_type.value}: {f.description}" for f in audited.audit.findings) or "no findings listed"
    return [
        HITLFlag(
            flag_id=_new_flag_id(),
            rule_id=audited.rule.rule_id,
            reason_code=HITLReasonCode.AUDIT_NOT_APPROVED,
            severity=HITLSeverity.BLOCKING,
            description=(
                f"Logic Auditor Agent verdict was '{audited.audit.verdict.value}' "
                f"(fidelity_score={audited.audit.fidelity_score:.2f}). Findings: {findings_summary}"
            ),
        )
    ]


def collect_hitl_flags(audited: AuditedComplianceRule) -> list[HITLFlag]:
    """Run every flagging check and return the combined list. Order is
    significant only for readability in a review UI (blocking-cause checks
    first)."""
    rule = audited.rule
    flags: list[HITLFlag] = []
    flags += flag_audit_not_approved(audited)
    flags += flag_low_confidence(rule)
    flags += flag_conflicting_thresholds(rule)
    flags += flag_no_deterministic_logic(rule)
    flags += flag_qualitative_directives(rule)
    flags += flag_ambiguous_spans(rule)
    flags += flag_unresolved_entities(rule)
    return flags


def has_blocking_flags(flags: list[HITLFlag]) -> bool:
    return any(f.severity == HITLSeverity.BLOCKING for f in flags)
