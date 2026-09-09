"""Comparison engine for M&A compliance due-diligence.

Executes deterministic multi-pass diffing and advisory semantic LLM analysis.
Strictly read-only: never mutates production policies or merges rules.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import re
from typing import Any

from app.config import Settings
from app.mna_due_diligence.models import (
    EntityComplianceSnapshot,
    EntityRuleSnapshot,
    MNADifferenceType,
    MNADueDiligenceReport,
    MNAFinding,
    MNAFindingProvenance,
    MNAFindingSeverity,
)
from app.mna_due_diligence.prompts import (
    MNA_SEMANTIC_SYSTEM_PROMPT,
    build_semantic_comparison_prompt,
)

logger = logging.getLogger(__name__)


def _extract_ast_thresholds(ast: dict[str, Any] | None) -> tuple[str | None, float | None]:
    """Helper to extract operator and numeric threshold from a JSON-Logic AST."""
    if not ast or not isinstance(ast, dict):
        return None, None
    for op in (">=", ">", "<=", "<", "==", "!="):
        if op in ast and isinstance(ast[op], list) and len(ast[op]) >= 2:
            left, right = ast[op][0], ast[op][1]
            val = None
            if isinstance(right, (int, float)):
                val = float(right)
            elif isinstance(left, (int, float)):
                val = float(left)
            return op, val
    return None, None


def _extract_rego_thresholds(rego: str | None) -> tuple[str | None, float | None]:
    """Fallback regex extraction of operator and threshold from Rego policy text."""
    if not rego:
        return None, None
    match = re.search(r"(>=|<=|>|<|==|!=)\s*([0-9]+(?:\.[0-9]+)?)", rego)
    if match:
        return match.group(1), float(match.group(2))
    return None, None


async def call_advisory_llm_comparison(
    settings: Settings,
    title: str,
    text_a: str | None,
    text_b: str | None,
    context: str = "",
) -> tuple[MNADifferenceType, MNAFindingSeverity, str, str, str | None, bool]:
    """Calls LiteLLM for advisory semantic evaluation with strict safety invariants.

    Returns:
        (diff_type, severity, evidence_quote, reasoning, recommendation, is_equivalent)
    """
    if not settings.mna_llm_advisory_enabled:
        return (
            MNADifferenceType.UNRESOLVED_AMBIGUITY,
            MNAFindingSeverity.MEDIUM,
            "",
            "LLM advisory evaluation disabled by configuration; manual review required.",
            "Conduct human compliance review of differing qualitative policies.",
            False,
        )

    try:
        import litellm

        prompt = build_semantic_comparison_prompt(title, text_a, text_b, context)
        response = await litellm.acompletion(
            model=settings.llm_router_frontier_model,
            api_key=settings.hf_api_token,
            temperature=0.0,
            max_tokens=1024,
            messages=[
                {"role": "system", "content": MNA_SEMANTIC_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
        )
        content = response.choices[0].message.content
        data = json.loads(content)

        diff_type_str = data.get("difference_type", "unresolved_ambiguity")
        try:
            diff_type = MNADifferenceType(diff_type_str)
        except ValueError:
            diff_type = MNADifferenceType.UNRESOLVED_AMBIGUITY

        sev_str = data.get("severity", "MEDIUM")
        try:
            severity = MNAFindingSeverity(sev_str)
        except ValueError:
            severity = MNAFindingSeverity.MEDIUM

        evidence_quote = str(data.get("evidence_quote", "")).strip()
        reasoning = str(data.get("reasoning", "")).strip()
        recommendation = data.get("recommendation")
        is_equivalent = bool(data.get("is_equivalent", False))

        # CRITICAL INVARIANT 11: Never let the LLM declare equivalence without proof!
        if is_equivalent and (not evidence_quote or len(evidence_quote) < 10):
            logger.warning(
                "LLM attempted to declare policies equivalent without verified evidence quote. Demoting to ambiguity."
            )
            diff_type = MNADifferenceType.UNRESOLVED_AMBIGUITY
            severity = MNAFindingSeverity.MEDIUM
            is_equivalent = False
            reasoning = (
                f"[SAFETY OVERRULE] LLM claimed equivalence without sufficient quoted evidence. "
                f"Original reasoning: {reasoning}"
            )

        return diff_type, severity, evidence_quote, reasoning, recommendation, is_equivalent

    except Exception as exc:
        logger.warning("Advisory LLM comparison failed: %s; falling back to conservative ambiguity.", exc)
        return (
            MNADifferenceType.UNRESOLVED_AMBIGUITY,
            MNAFindingSeverity.MEDIUM,
            "",
            f"Advisory semantic evaluation encountered error ({exc}); human compliance review required.",
            "Manually inspect policy wording differences across both entities.",
            False,
        )


async def run_due_diligence_comparison(
    snapshot_a: EntityComplianceSnapshot,
    snapshot_b: EntityComplianceSnapshot,
    job_id: str,
    initiator: str,
    settings: Settings,
    scope: list[str] | None = None,
    advisory_mode: bool = True,
) -> MNADueDiligenceReport:
    """Executes the end-to-end M&A compliance due-diligence comparison.

    INVARIANTS:
    - Zero mutation: no writes to DB tables or entity configurations.
    - Deterministic comparisons remain deterministic (is_advisory = False).
    - LLM comparisons are strictly advisory (is_advisory = True).
    - Every finding retains complete provenance.
    """
    effective_scope = set(scope or ["rules", "thresholds", "risk_overlay", "hitl", "violations", "graph"])
    findings: list[MNAFinding] = []

    rules_a_by_id = {r.rule_id: r for r in snapshot_a.rules}
    rules_b_by_id = {r.rule_id: r for r in snapshot_b.rules}
    all_rule_ids = sorted(set(rules_a_by_id.keys()) | set(rules_b_by_id.keys()))

    # =========================================================================
    # PASS 1: Compiled Rule & Threshold Comparison
    # =========================================================================
    if "rules" in effective_scope or "thresholds" in effective_scope:
        for rule_id in all_rule_ids:
            rule_a = rules_a_by_id.get(rule_id)
            rule_b = rules_b_by_id.get(rule_id)

            if rule_a and not rule_b:
                # Missing policy in Entity B
                findings.append(
                    MNAFinding(
                        difference_type=MNADifferenceType.MISSING_POLICY,
                        severity=MNAFindingSeverity.HIGH if rule_a.is_active else MNAFindingSeverity.MEDIUM,
                        title=f"Policy Missing in Entity B: {rule_id}",
                        description=(
                            f"Entity A has an active compiled rule '{rule_id}' (version {rule_a.rule_version}) "
                            f"which is completely absent from Entity B's compliance configuration."
                        ),
                        is_advisory=False,
                        provenance=MNAFindingProvenance(
                            entity_a_artifact_ref=f"rule:{rule_a.rule_id}:v{rule_a.rule_version}",
                            entity_b_artifact_ref=None,
                            entity_a_hash=rule_a.policy_sha256,
                            entity_b_hash=None,
                            metric_field=rule_a.section_reference,
                            evidence_notes=f"Entity A compiled policy hash: {rule_a.policy_sha256}; Entity B has no rule registered.",
                        ),
                        entity_a_value={"is_active": rule_a.is_active, "version": rule_a.rule_version},
                        entity_b_value=None,
                        recommendation="Evaluate whether Entity B requires adoption of this compliance rule prior to consolidation.",
                    )
                )
            elif rule_b and not rule_a:
                # Missing policy in Entity A
                findings.append(
                    MNAFinding(
                        difference_type=MNADifferenceType.MISSING_POLICY,
                        severity=MNAFindingSeverity.HIGH if rule_b.is_active else MNAFindingSeverity.MEDIUM,
                        title=f"Policy Missing in Entity A: {rule_id}",
                        description=(
                            f"Entity B enforces compliance rule '{rule_id}' (version {rule_b.rule_version}) "
                            f"which does not exist in Entity A's posture."
                        ),
                        is_advisory=False,
                        provenance=MNAFindingProvenance(
                            entity_a_artifact_ref=None,
                            entity_b_artifact_ref=f"rule:{rule_b.rule_id}:v{rule_b.rule_version}",
                            entity_a_hash=None,
                            entity_b_hash=rule_b.policy_sha256,
                            metric_field=rule_b.section_reference,
                            evidence_notes=f"Entity B compiled policy hash: {rule_b.policy_sha256}; Entity A has no rule registered.",
                        ),
                        entity_a_value=None,
                        entity_b_value={"is_active": rule_b.is_active, "version": rule_b.rule_version},
                        recommendation="Assess whether Entity A should implement this target policy or if it is redundant.",
                    )
                )
            elif rule_a and rule_b:
                # Rule exists on both sides: compare hashes and thresholds
                if rule_a.policy_sha256 and rule_b.policy_sha256 and rule_a.policy_sha256 == rule_b.policy_sha256:
                    # Identical compiled policy hash -- no material discrepancy!
                    pass
                else:
                    # Hashes differ! Extract numerical thresholds and operators
                    op_a, val_a = _extract_ast_thresholds(rule_a.jsonlogic_ast)
                    if op_a is None:
                        op_a, val_a = _extract_rego_thresholds(rule_a.rego_policy)

                    op_b, val_b = _extract_ast_thresholds(rule_b.jsonlogic_ast)
                    if op_b is None:
                        op_b, val_b = _extract_rego_thresholds(rule_b.rego_policy)

                    if val_a is not None and val_b is not None:
                        # Numeric comparison
                        if op_a != op_b:
                            # Contradictory operator! (e.g. >= vs <=)
                            findings.append(
                                MNAFinding(
                                    difference_type=MNADifferenceType.POTENTIAL_CONFLICT,
                                    severity=MNAFindingSeverity.CRITICAL,
                                    title=f"Contradictory Operator on Rule: {rule_id}",
                                    description=(
                                        f"Entity A enforces '{op_a} {val_a}' while Entity B enforces "
                                        f"opposing condition '{op_b} {val_b}' on rule {rule_id}."
                                    ),
                                    is_advisory=False,
                                    provenance=MNAFindingProvenance(
                                        entity_a_artifact_ref=f"rule:{rule_a.rule_id}:v{rule_a.rule_version}",
                                        entity_b_artifact_ref=f"rule:{rule_b.rule_id}:v{rule_b.rule_version}",
                                        entity_a_hash=rule_a.policy_sha256,
                                        entity_b_hash=rule_b.policy_sha256,
                                        metric_field=rule_a.section_reference,
                                        evidence_notes=f"Operator conflict: Entity A({op_a}) vs Entity B({op_b})",
                                    ),
                                    entity_a_value=f"{op_a} {val_a}",
                                    entity_b_value=f"{op_b} {val_b}",
                                    recommendation="Resolve contradictory rule logic before consolidating execution engines.",
                                )
                            )
                        elif abs(val_a - val_b) > 1e-4:
                            # Different threshold value on same operator
                            delta = abs(val_a - val_b)
                            delta_pct = (delta / val_a * 100.0) if val_a != 0 else None
                            findings.append(
                                MNAFinding(
                                    difference_type=MNADifferenceType.SEMANTIC_DIFFERENCE,
                                    severity=MNAFindingSeverity.HIGH if (delta_pct and delta_pct > 15.0) else MNAFindingSeverity.MEDIUM,
                                    title=f"Threshold Discrepancy on Rule: {rule_id}",
                                    description=(
                                        f"Rule {rule_id} has differing thresholds: Entity A requires {op_a} {val_a}, "
                                        f"whereas Entity B requires {op_b} {val_b} (delta: {delta:.2f})."
                                    ),
                                    is_advisory=False,
                                    provenance=MNAFindingProvenance(
                                        entity_a_artifact_ref=f"rule:{rule_a.rule_id}:v{rule_a.rule_version}",
                                        entity_b_artifact_ref=f"rule:{rule_b.rule_id}:v{rule_b.rule_version}",
                                        entity_a_hash=rule_a.policy_sha256,
                                        entity_b_hash=rule_b.policy_sha256,
                                        metric_field=rule_a.section_reference,
                                        evidence_notes=f"Threshold delta: A={val_a}, B={val_b}, delta_pct={delta_pct}",
                                    ),
                                    entity_a_value=val_a,
                                    entity_b_value=val_b,
                                    recommendation="Harmonize numerical compliance threshold to the more restrictive standard.",
                                )
                            )
                        else:
                            # Values & operators match, but hashes differ (syntactic difference)
                            findings.append(
                                MNAFinding(
                                    difference_type=MNADifferenceType.EXACT_DIFFERENCE,
                                    severity=MNAFindingSeverity.LOW,
                                    title=f"Policy Code / Hash Variation: {rule_id}",
                                    description=f"Rule {rule_id} shares identical thresholds but differing AST structure or policy hashes.",
                                    is_advisory=False,
                                    provenance=MNAFindingProvenance(
                                        entity_a_artifact_ref=f"rule:{rule_a.rule_id}:v{rule_a.rule_version}",
                                        entity_b_artifact_ref=f"rule:{rule_b.rule_id}:v{rule_b.rule_version}",
                                        entity_a_hash=rule_a.policy_sha256,
                                        entity_b_hash=rule_b.policy_sha256,
                                        metric_field=rule_a.section_reference,
                                        evidence_notes=f"Hash mismatch: A={rule_a.policy_sha256[:12]}... vs B={rule_b.policy_sha256[:12]}...",
                                    ),
                                    entity_a_value=rule_a.policy_sha256,
                                    entity_b_value=rule_b.policy_sha256,
                                    recommendation="Recompile rule on standard compiler version to align ASTs.",
                                )
                            )
                    else:
                        # Qualitative / non-numeric policy hash difference
                        if advisory_mode:
                            diff_type, sev, quote, reasoning, rec, is_equiv = await call_advisory_llm_comparison(
                                settings=settings,
                                title=f"Rule Logic: {rule_id}",
                                text_a=rule_a.rego_policy or str(rule_a.jsonlogic_ast),
                                text_b=rule_b.rego_policy or str(rule_b.jsonlogic_ast),
                                context=f"Clause reference: {rule_a.section_reference}",
                            )
                            if not is_equiv:
                                findings.append(
                                    MNAFinding(
                                        difference_type=diff_type,
                                        severity=sev,
                                        title=f"Qualitative Policy Difference: {rule_id}",
                                        description=reasoning,
                                        is_advisory=True,
                                        provenance=MNAFindingProvenance(
                                            entity_a_artifact_ref=f"rule:{rule_a.rule_id}:v{rule_a.rule_version}",
                                            entity_b_artifact_ref=f"rule:{rule_b.rule_id}:v{rule_b.rule_version}",
                                            entity_a_hash=rule_a.policy_sha256,
                                            entity_b_hash=rule_b.policy_sha256,
                                            metric_field=rule_a.section_reference,
                                            evidence_notes=quote or f"Advisory LLM evaluation: {reasoning[:200]}",
                                        ),
                                        entity_a_value=rule_a.policy_sha256,
                                        entity_b_value=rule_b.policy_sha256,
                                        recommendation=rec or "Review qualitative policy variation with legal counsel.",
                                    )
                                )
                        else:
                            findings.append(
                                MNAFinding(
                                    difference_type=MNADifferenceType.EXACT_DIFFERENCE,
                                    severity=MNAFindingSeverity.LOW,
                                    title=f"Policy Code / Hash Variation: {rule_id}",
                                    description=f"Rule {rule_id} has differing policy hashes without detectable numeric divergence.",
                                    is_advisory=False,
                                    provenance=MNAFindingProvenance(
                                        entity_a_artifact_ref=f"rule:{rule_a.rule_id}:v{rule_a.rule_version}",
                                        entity_b_artifact_ref=f"rule:{rule_b.rule_id}:v{rule_b.rule_version}",
                                        entity_a_hash=rule_a.policy_sha256,
                                        entity_b_hash=rule_b.policy_sha256,
                                        metric_field=rule_a.section_reference,
                                        evidence_notes="Deterministic hash mismatch.",
                                    ),
                                    entity_a_value=rule_a.policy_sha256,
                                    entity_b_value=rule_b.policy_sha256,
                                )
                            )

    # =========================================================================
    # PASS 2: Risk Overlay Discrepancies
    # =========================================================================
    if "risk_overlay" in effective_scope:
        overlay_a = snapshot_a.risk_overlay or {}
        overlay_b = snapshot_b.risk_overlay or {}
        all_keys = sorted(set(overlay_a.keys()) | set(overlay_b.keys()))

        for key in all_keys:
            val_a = overlay_a.get(key)
            val_b = overlay_b.get(key)

            if val_a is not None and val_b is None:
                findings.append(
                    MNAFinding(
                        difference_type=MNADifferenceType.MISSING_POLICY,
                        severity=MNAFindingSeverity.MEDIUM,
                        title=f"Risk Overlay Key Missing in Entity B: {key}",
                        description=f"Entity A specifies custom risk overlay '{key} = {val_a}', but Entity B has no override configured.",
                        is_advisory=False,
                        provenance=MNAFindingProvenance(
                            entity_a_artifact_ref=f"risk_overlay:{key}",
                            entity_b_artifact_ref=None,
                            metric_field=key,
                            evidence_notes=f"Entity A risk_overlay[{key}] = {val_a}",
                        ),
                        entity_a_value=val_a,
                        entity_b_value=None,
                        recommendation="Confirm whether Entity B should adopt Entity A's risk overlay parameter.",
                    )
                )
            elif val_b is not None and val_a is None:
                findings.append(
                    MNAFinding(
                        difference_type=MNADifferenceType.MISSING_POLICY,
                        severity=MNAFindingSeverity.MEDIUM,
                        title=f"Risk Overlay Key Missing in Entity A: {key}",
                        description=f"Entity B specifies risk overlay '{key} = {val_b}', which is absent from Entity A.",
                        is_advisory=False,
                        provenance=MNAFindingProvenance(
                            entity_a_artifact_ref=None,
                            entity_b_artifact_ref=f"risk_overlay:{key}",
                            metric_field=key,
                            evidence_notes=f"Entity B risk_overlay[{key}] = {val_b}",
                        ),
                        entity_a_value=None,
                        entity_b_value=val_b,
                        recommendation="Evaluate if Entity B's risk parameter represents a required prudential safeguard.",
                    )
                )
            elif val_a != val_b:
                findings.append(
                    MNAFinding(
                        difference_type=MNADifferenceType.SEMANTIC_DIFFERENCE,
                        severity=MNAFindingSeverity.HIGH if "margin" in key.lower() or "exposure" in key.lower() else MNAFindingSeverity.MEDIUM,
                        title=f"Risk Overlay Discrepancy: {key}",
                        description=f"Entity A sets '{key}' to {val_a}, whereas Entity B sets it to {val_b}.",
                        is_advisory=False,
                        provenance=MNAFindingProvenance(
                            entity_a_artifact_ref=f"risk_overlay:{key}",
                            entity_b_artifact_ref=f"risk_overlay:{key}",
                            metric_field=key,
                            evidence_notes=f"Risk overlay comparison: Entity A({val_a}) vs Entity B({val_b})",
                        ),
                        entity_a_value=val_a,
                        entity_b_value=val_b,
                        recommendation="Align risk parameters to prevent margin or exposure arbitrage upon merger.",
                    )
                )

    # =========================================================================
    # PASS 3: Unresolved HITL Reviews (Ambiguity Auditing)
    # =========================================================================
    if "hitl" in effective_scope:
        for hitl_a in snapshot_a.unresolved_hitl:
            findings.append(
                MNAFinding(
                    difference_type=MNADifferenceType.UNRESOLVED_AMBIGUITY,
                    severity=MNAFindingSeverity.HIGH if hitl_a.severity == "blocking" else MNAFindingSeverity.MEDIUM,
                    title=f"Unresolved HITL Review in Entity A: {hitl_a.reason_code}",
                    description=(
                        f"Entity A has an open/unresolved HITL review ({hitl_a.review_id}) with severity "
                        f"'{hitl_a.severity}': {hitl_a.description}"
                    ),
                    is_advisory=False,
                    provenance=MNAFindingProvenance(
                        entity_a_artifact_ref=f"hitl:{hitl_a.review_id}",
                        entity_b_artifact_ref=None,
                        entity_a_hash=hitl_a.reason_code,
                        evidence_notes=hitl_a.source_excerpt or hitl_a.description,
                    ),
                    entity_a_value={"review_id": hitl_a.review_id, "status": hitl_a.status},
                    entity_b_value=None,
                    recommendation="Require compliance officer sign-off to resolve pending HITL item prior to merger.",
                )
            )

        for hitl_b in snapshot_b.unresolved_hitl:
            findings.append(
                MNAFinding(
                    difference_type=MNADifferenceType.UNRESOLVED_AMBIGUITY,
                    severity=MNAFindingSeverity.HIGH if hitl_b.severity == "blocking" else MNAFindingSeverity.MEDIUM,
                    title=f"Unresolved HITL Review in Entity B: {hitl_b.reason_code}",
                    description=(
                        f"Target Entity B carries an open HITL review ({hitl_b.review_id}) with severity "
                        f"'{hitl_b.severity}': {hitl_b.description}"
                    ),
                    is_advisory=False,
                    provenance=MNAFindingProvenance(
                        entity_a_artifact_ref=None,
                        entity_b_artifact_ref=f"hitl:{hitl_b.review_id}",
                        entity_b_hash=hitl_b.reason_code,
                        evidence_notes=hitl_b.source_excerpt or hitl_b.description,
                    ),
                    entity_a_value=None,
                    entity_b_value={"review_id": hitl_b.review_id, "status": hitl_b.status},
                    recommendation="Remediate open compliance flag in target entity before closing transaction.",
                )
            )

    # =========================================================================
    # PASS 4: Graph Obligations & Cross-Domain Discrepancies
    # =========================================================================
    if "graph" in effective_scope:
        graph_a_by_metric = {g.metric: g for g in snapshot_a.graph_obligations}
        graph_b_by_metric = {g.metric: g for g in snapshot_b.graph_obligations}
        common_metrics = set(graph_a_by_metric.keys()) & set(graph_b_by_metric.keys())

        for metric in common_metrics:
            g_a = graph_a_by_metric[metric]
            g_b = graph_b_by_metric[metric]
            if g_a.operator != g_b.operator:
                findings.append(
                    MNAFinding(
                        difference_type=MNADifferenceType.POTENTIAL_CONFLICT,
                        severity=MNAFindingSeverity.HIGH,
                        title=f"Graph Obligation Conflict on Metric: {metric}",
                        description=(
                            f"Entity A graph obligation specifies '{g_a.operator} {g_a.value} {g_a.unit}', "
                            f"while Entity B graph obligation specifies '{g_b.operator} {g_b.value} {g_b.unit}'."
                        ),
                        is_advisory=False,
                        provenance=MNAFindingProvenance(
                            entity_a_artifact_ref=f"graph:{g_a.obligation_id}",
                            entity_b_artifact_ref=f"graph:{g_b.obligation_id}",
                            metric_field=metric,
                            evidence_notes=f"Graph conflict on domain: {g_a.domain or g_b.domain}",
                        ),
                        entity_a_value=f"{g_a.operator} {g_a.value}",
                        entity_b_value=f"{g_b.operator} {g_b.value}",
                        recommendation="Harmonize graph-derived regulatory obligations.",
                    )
                )

    # =========================================================================
    # PASS 5: Historical Violation Patterns
    # =========================================================================
    if "violations" in effective_scope:
        viols_a = snapshot_a.historical_violations_summary
        viols_b = snapshot_b.historical_violations_summary

        if viols_b.total_fails > 0:
            findings.append(
                MNAFinding(
                    difference_type=MNADifferenceType.POTENTIAL_CONFLICT,
                    severity=MNAFindingSeverity.HIGH if viols_b.total_fails > 10 else MNAFindingSeverity.MEDIUM,
                    title=f"Historical Non-Compliance History in Target Entity: {snapshot_b.entity_id}",
                    description=(
                        f"Target Entity B has logged {viols_b.total_fails} historical evaluation failures and "
                        f"{viols_b.total_hitl_reviews} HITL escalation events in the audit ledger. "
                        f"Frequently failed rules: {', '.join(viols_b.failed_rule_ids[:5])}."
                    ),
                    is_advisory=False,
                    provenance=MNAFindingProvenance(
                        entity_a_artifact_ref=None,
                        entity_b_artifact_ref=f"ledger:broker:{snapshot_b.entity_id}",
                        evidence_notes=f"Target ledger violations: {viols_b.total_fails} fails across {viols_b.total_evaluations} evaluations.",
                    ),
                    entity_a_value={"fails": viols_a.total_fails, "hitl": viols_a.total_hitl_reviews},
                    entity_b_value={"fails": viols_b.total_fails, "hitl": viols_b.total_hitl_reviews},
                    recommendation="Review historical failure root causes to identify latent regulatory exposure.",
                )
            )

    # =========================================================================
    # Summary Counts & Risk Score Calculation
    # =========================================================================
    summary_counts: dict[str, int] = {
        "total_findings": len(findings),
        "exact_difference": sum(1 for f in findings if f.difference_type == MNADifferenceType.EXACT_DIFFERENCE),
        "semantic_difference": sum(1 for f in findings if f.difference_type == MNADifferenceType.SEMANTIC_DIFFERENCE),
        "potential_conflict": sum(1 for f in findings if f.difference_type == MNADifferenceType.POTENTIAL_CONFLICT),
        "unresolved_ambiguity": sum(1 for f in findings if f.difference_type == MNADifferenceType.UNRESOLVED_AMBIGUITY),
        "missing_policy": sum(1 for f in findings if f.difference_type == MNADifferenceType.MISSING_POLICY),
        "CRITICAL": sum(1 for f in findings if f.severity == MNAFindingSeverity.CRITICAL),
        "HIGH": sum(1 for f in findings if f.severity == MNAFindingSeverity.HIGH),
        "MEDIUM": sum(1 for f in findings if f.severity == MNAFindingSeverity.MEDIUM),
        "LOW": sum(1 for f in findings if f.severity == MNAFindingSeverity.LOW),
        "INFO": sum(1 for f in findings if f.severity == MNAFindingSeverity.INFO),
    }

    # Normalized risk score between 0.0 and 100.0
    critical_weight = summary_counts["CRITICAL"] * 25.0
    high_weight = summary_counts["HIGH"] * 10.0
    medium_weight = summary_counts["MEDIUM"] * 4.0
    low_weight = summary_counts["LOW"] * 1.0
    raw_score = critical_weight + high_weight + medium_weight + low_weight
    overall_risk_score = min(100.0, round(raw_score, 2))

    executive_summary = (
        f"M&A Due-Diligence comparison between '{snapshot_a.display_name}' ({snapshot_a.entity_id}) and "
        f"'{snapshot_b.display_name}' ({snapshot_b.entity_id}) identified {len(findings)} total findings "
        f"resulting in an overall regulatory risk score of {overall_risk_score}/100.0. "
        f"Found {summary_counts['potential_conflict']} potential conflicts, {summary_counts['semantic_difference']} "
        f"semantic threshold differences, {summary_counts['missing_policy']} missing policies, and "
        f"{summary_counts['unresolved_ambiguity']} unresolved ambiguities."
    )

    return MNADueDiligenceReport(
        job_id=job_id,
        entity_a_id=snapshot_a.entity_id,
        entity_b_id=snapshot_b.entity_id,
        generated_at=dt.datetime.now(dt.timezone.utc),
        initiator_subject=initiator,
        entity_a_snapshot_hash=snapshot_a.snapshot_hash,
        entity_b_snapshot_hash=snapshot_b.snapshot_hash,
        findings=findings,
        summary_counts=summary_counts,
        overall_risk_score=overall_risk_score,
        executive_summary=executive_summary,
    )
