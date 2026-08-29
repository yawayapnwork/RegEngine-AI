"""Orchestrates the full pre-compilation impact analysis for one newly
ingested circular: for every audited, approved clause, find its
historical counterpart, structurally (or, when inconclusive, via LLM)
classify what changed, map the change to internal services, and roll
everything up into a `CircularImpactReport`.

Entry point: `analyze_circular_impact`. Meant to run between the
extraction/audit stage (app.agents.pipeline, produces
`AuditedComplianceRule`) and compilation (app.compiler.pipeline,
produces `CompiledRego`/`JsonLogicRule`) -- see app.diffing.models'
module docstring.
"""
from __future__ import annotations

import logging
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.schemas import AuditedComplianceRule
from app.compiler.naming import metric_field_name
from app.config import Settings
from app.db.models import Circular, Clause, CompiledRule
from app.diffing.llm_classifier import classify_change_with_llm
from app.diffing.models import (
    ChangeType,
    CircularImpactReport,
    ClauseDiffResult,
    ImpactSeverity,
    MatchConfidence,
    ThresholdDelta,
)
from app.diffing.semantic_diff import diff_thresholds, find_best_historical_match, looks_like_deadline
from app.diffing.service_mapping import resolve_service_impacts
from app.diffing.threshold_extraction import extract_thresholds_from_jsonlogic
from app.models import ClauseChunk

logger = logging.getLogger(__name__)

_SEVERITY_ORDER = [ImpactSeverity.LOW, ImpactSeverity.MEDIUM, ImpactSeverity.HIGH, ImpactSeverity.CRITICAL]

# A threshold that moved by more than this is treated as HIGH severity
# even when it isn't a deadline -- a 25% relative swing in a margin/
# capital/solvency requirement is not a routine parameter tweak.
_LARGE_DELTA_PCT_THRESHOLD = 15.0


async def _lookup_historical_compiled_rule(db: AsyncSession, sha256: str | None) -> CompiledRule | None:
    if not sha256:
        return None
    result = await db.execute(
        select(CompiledRule)
        .join(Clause, Clause.id == CompiledRule.clause_id)
        .where(Clause.sha256 == sha256, CompiledRule.is_active.is_(True))
        .limit(1)
    )
    return result.scalar_one_or_none()


def _max_severity(current: ImpactSeverity, candidate: ImpactSeverity) -> ImpactSeverity:
    return candidate if _SEVERITY_ORDER.index(candidate) > _SEVERITY_ORDER.index(current) else current


async def _diff_one_clause(
    chunk: ClauseChunk,
    audited: AuditedComplianceRule,
    settings: Settings,
    db: AsyncSession,
) -> tuple[ClauseDiffResult, str | None]:
    """Returns (result, matched_historical_sha256_or_None) -- the sha256
    is threaded back up to `analyze_circular_impact` for the
    supersession coverage-check pass."""
    rule = audited.rule
    match = await find_best_historical_match(chunk.text, settings)

    base_kwargs = dict(
        new_rule_id=rule.rule_id,
        new_clause_number=rule.clause_number,
        new_circular_number=rule.circular_number,
    )

    if match is None or match.confidence == MatchConfidence.NO_MATCH:
        return (
            ClauseDiffResult(
                **base_kwargs,
                change_type=ChangeType.NEW_OBLIGATION,
                severity=ImpactSeverity.CRITICAL,
                classification_method="structural",
                classification_confidence=1.0,
                narrative_summary="No historical clause found with sufficient semantic similarity; treated as a wholly new compliance obligation.",
                service_impacts=resolve_service_impacts(
                    [f"facts.{metric_field_name(t.metric, t.unit)}" for t in rule.deterministic_logic],
                    rule.regulatory_domain,
                ),
                requires_hitl_review=True,
            ),
            None,
        )

    match_kwargs = dict(
        matched_historical_chunk_id=match.chunk_id,
        matched_historical_clause_number=match.clause_number,
        matched_historical_circular_number=match.circular_number,
        similarity_score=match.similarity,
        match_confidence=match.confidence,
    )

    historical_sha256 = None
    historical_compiled = None
    if match.confidence in (MatchConfidence.IDENTICAL, MatchConfidence.NEAR_DUPLICATE, MatchConfidence.LIKELY_AMENDMENT):
        # Qdrant's payload carries the clause's own sha256 (see
        # app.vectorstore.qdrant_store._chunk_payload) -- used here purely
        # as a join key back into the relational schema to find whatever
        # was actually compiled for it, never re-derived.
        historical_sha256 = match.sha256
        historical_compiled = await _lookup_historical_compiled_rule(db, historical_sha256)

    old_thresholds = extract_thresholds_from_jsonlogic(historical_compiled.jsonlogic_ast) if historical_compiled and historical_compiled.jsonlogic_ast else []

    # --- Structural path: both sides have (or lack) deterministic logic
    # in a way that's mechanically comparable. ---
    if old_thresholds or rule.deterministic_logic:
        deltas, new_only, removed = diff_thresholds(rule.deterministic_logic, old_thresholds)
        return _classify_structurally(base_kwargs, match_kwargs, deltas, new_only, removed, rule, match), historical_sha256

    # --- No numeric thresholds on either side (pure qualitative clauses,
    # or a weak/ambiguous match) -- fall back to the LLM. ---
    llm_result = await classify_change_with_llm(
        settings,
        new_clause_text=chunk.text,
        old_clause_text=match.text,
        new_thresholds=[],
        old_thresholds=old_thresholds or None,
        similarity_score=match.similarity,
    )
    severity = _severity_for(llm_result.change_type, [])
    return (
        ClauseDiffResult(
            **base_kwargs,
            **match_kwargs,
            change_type=llm_result.change_type,
            severity=severity,
            classification_method="llm",
            classification_confidence=llm_result.confidence,
            narrative_summary=llm_result.reasoning,
            service_impacts=resolve_service_impacts([], rule.regulatory_domain),
            requires_hitl_review=llm_result.confidence < 0.7 or severity in (ImpactSeverity.HIGH, ImpactSeverity.CRITICAL),
        ),
        historical_sha256,
    )


def _severity_for(change_type: ChangeType, deltas: list[ThresholdDelta]) -> ImpactSeverity:
    if change_type in (ChangeType.NEW_OBLIGATION, ChangeType.OBLIGATION_REMOVED):
        return ImpactSeverity.CRITICAL
    if change_type == ChangeType.DEADLINE_AMENDMENT:
        return ImpactSeverity.HIGH
    if change_type == ChangeType.THRESHOLD_SHIFT:
        if any(d.delta_pct is not None and abs(d.delta_pct) >= _LARGE_DELTA_PCT_THRESHOLD for d in deltas):
            return ImpactSeverity.HIGH
        return ImpactSeverity.MEDIUM
    if change_type == ChangeType.ENTITY_SCOPE_CHANGE:
        return ImpactSeverity.MEDIUM
    return ImpactSeverity.LOW


def _classify_structurally(base_kwargs: dict, match_kwargs: dict, deltas, new_only, removed, rule, match) -> ClauseDiffResult:
    fields_to_map = [d.field for d in deltas] + [f"facts.{f}" for f in new_only]

    if deltas:
        is_deadline = any(looks_like_deadline(d.metric, d.unit) for d in deltas)
        change_type = ChangeType.DEADLINE_AMENDMENT if is_deadline else ChangeType.THRESHOLD_SHIFT
        summary_bits = [
            f"{d.metric} changed from {d.old_operator} {d.old_value}{d.unit} to {d.new_operator} {d.new_value}{d.unit}"
            + (f" ({d.delta_pct:+.1f}%)" if d.delta_pct is not None else "")
            for d in deltas
        ]
        narrative = "; ".join(summary_bits)
        severity = _severity_for(change_type, deltas)
        requires_hitl = severity in (ImpactSeverity.HIGH, ImpactSeverity.CRITICAL)
    elif new_only:
        change_type = ChangeType.NEW_OBLIGATION
        narrative = f"Matched clause {match.clause_number or 'unscoped'} in {match.circular_number or 'a prior circular'}, but this version adds new deterministic condition(s): {', '.join(new_only)}."
        severity = ImpactSeverity.CRITICAL
        requires_hitl = True
    elif removed:
        change_type = ChangeType.OBLIGATION_REMOVED
        narrative = f"Matched clause {match.clause_number or 'unscoped'}, but previously-compiled condition(s) are absent from the new text: {', '.join(removed)}."
        severity = ImpactSeverity.CRITICAL
        requires_hitl = True
    elif match.confidence in (MatchConfidence.IDENTICAL, MatchConfidence.NEAR_DUPLICATE):
        change_type = ChangeType.UNCHANGED
        narrative = "No numeric threshold changes detected; matched historical clause is a near-verbatim restatement."
        severity = ImpactSeverity.LOW
        requires_hitl = False
    else:
        change_type = ChangeType.WORDING_ONLY
        narrative = "Text differs from the matched historical clause but all numeric thresholds are unchanged."
        severity = ImpactSeverity.LOW
        requires_hitl = False

    return ClauseDiffResult(
        **base_kwargs,
        **match_kwargs,
        change_type=change_type,
        severity=severity,
        classification_method="structural",
        classification_confidence=0.95,
        threshold_deltas=deltas,
        narrative_summary=narrative,
        service_impacts=resolve_service_impacts(fields_to_map, rule.regulatory_domain),
        requires_hitl_review=requires_hitl,
    )


async def analyze_circular_impact(
    clauses_and_rules: list[tuple[ClauseChunk, AuditedComplianceRule]],
    settings: Settings,
    db: AsyncSession,
    supersedes_circular_number: str | None = None,
) -> CircularImpactReport:
    """`clauses_and_rules`: only rules the Logic Auditor Agent APPROVED
    should be passed in -- same trustworthiness filter as
    llm_finetune.dataset.format_instructions._only_trustworthy, for the
    same reason: diffing a rejected/hallucinated extraction against
    history would produce a misleading impact report.

    `supersedes_circular_number`: if the caller knows this circular
    explicitly supersedes an earlier one (a compliance officer's own
    determination, not inferred), every clause of that prior circular
    NOT matched by any clause here is reported in
    `CircularImpactReport.obligations_removed` -- a best-effort coverage
    check, not exhaustive (a clause dropped from a NON-superseded prior
    circular is out of scope; see module docstring)."""
    if not clauses_and_rules:
        return CircularImpactReport(report_id=str(uuid.uuid4()), total_new_clauses=0)

    circular_number = clauses_and_rules[0][1].rule.circular_number
    regulator = clauses_and_rules[0][0].regulator.value

    diffs: list[ClauseDiffResult] = []
    matched_sha256s: set[str] = set()

    for chunk, audited in clauses_and_rules:
        if audited.audit.verdict.value != "approved":
            logger.info("Skipping impact diff for rule %s: audit verdict is '%s', not 'approved'.", audited.rule.rule_id, audited.audit.verdict.value)
            continue
        result, historical_sha256 = await _diff_one_clause(chunk, audited, settings, db)
        diffs.append(result)
        if historical_sha256:
            matched_sha256s.add(historical_sha256)

    obligations_removed: list[str] = []
    if supersedes_circular_number:
        obligations_removed = await _find_unmatched_prior_clauses(db, supersedes_circular_number, matched_sha256s)

    change_type_counts: dict[str, int] = {}
    severity_counts: dict[str, int] = {}
    overall_risk = ImpactSeverity.LOW
    affected_services: set[str] = set()

    for d in diffs:
        change_type_counts[d.change_type.value] = change_type_counts.get(d.change_type.value, 0) + 1
        severity_counts[d.severity.value] = severity_counts.get(d.severity.value, 0) + 1
        overall_risk = _max_severity(overall_risk, d.severity)
        for svc in d.service_impacts:
            affected_services.add(svc.service_name)

    if obligations_removed:
        overall_risk = _max_severity(overall_risk, ImpactSeverity.CRITICAL)

    return CircularImpactReport(
        report_id=str(uuid.uuid4()),
        circular_number=circular_number,
        regulator=regulator,
        total_new_clauses=len(diffs),
        change_type_counts=change_type_counts,
        severity_counts=severity_counts,
        overall_risk_level=overall_risk,
        clause_diffs=diffs,
        affected_services=sorted(affected_services),
        obligations_removed=obligations_removed,
    )


async def _find_unmatched_prior_clauses(db: AsyncSession, supersedes_circular_number: str, matched_sha256s: set[str]) -> list[str]:
    result = await db.execute(
        select(Clause.clause_number, Clause.sha256, Clause.text)
        .join(Circular, Circular.id == Clause.circular_id)
        .where(Circular.circular_number == supersedes_circular_number)
    )
    rows = result.all()
    unmatched = []
    for clause_number, sha256, text in rows:
        if sha256 not in matched_sha256s:
            unmatched.append(f"{clause_number or 'unscoped'}: {text[:120]}{'...' if len(text) > 120 else ''}")
    return unmatched
