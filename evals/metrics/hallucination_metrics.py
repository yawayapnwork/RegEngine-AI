"""Hallucination detection metrics for the Logic Auditor Agent.

The auditor is given a pre-poisoned ``ExtractedComplianceRule`` (with known
injected hallucinations) and the raw source text.  We measure how reliably
it catches every injection and how rarely it raises false alarms on clean fields.

Metrics produced
----------------
HallucinationDetectionScore (HDS)
    The primary headline metric.  Fraction of injected hallucinations the
    auditor correctly flagged at the right severity level.

    HDS = TP_findings / (TP_findings + FN_findings)

    A HDS < 1.0 means the auditor let at least one hallucination through;
    in a BLOCKER-miss that means a fabricated compliance rule could reach
    production.

FalsePositiveRate (FPR)
    Fraction of auditor findings on the *clean* case (H-030) that have no
    corresponding expected finding.  High FPR means the auditor is noisy and
    will erode compliance-officer trust.

BLOCKERCatchRate (BCR)
    Fraction of injected BLOCKER-severity hallucinations the auditor correctly
    flagged as BLOCKER.  Distinct from HDS because a BLOCKER-miss is
    categorically worse than a MINOR-miss.

    BCR = TP_blockers / (TP_blockers + FN_blockers)

VerdictAccuracy
    Fraction of cases where the auditor's verdict (APPROVED/NEEDS_REVISION/REJECTED)
    matches the expected verdict given the injections.

FidelityScoreCorrelation
    Pearson correlation between the auditor's ``fidelity_score`` and the
    injection severity level (higher injection severity → lower fidelity expected).
    A well-calibrated auditor should show negative correlation.

FindingTypeAccuracy
    Per-injection-type accuracy: for each FindingType (HALLUCINATED_THRESHOLD,
    HALLUCINATED_ENTITY, etc.) what fraction of injections of that type did the
    auditor catch?

Tier breakdown
    HDS / BCR are also broken down by tier (1/2/3) so we can see whether the
    auditor degrades on subtle injections (Tier 3).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Sequence

from app.agents.schemas import AuditVerdict, ComplianceRuleAudit, FindingType, Severity
from evals.fixtures.hallucination_injections import (
    ALL_INJECTIONS,
    ExpectedFinding,
    HallucinationCase,
)


# ---------------------------------------------------------------------------
# Result containers
# ---------------------------------------------------------------------------

@dataclass
class FindingMatchResult:
    """Whether a single expected finding was caught by the auditor."""
    case_id: str
    expected: ExpectedFinding
    caught: bool
    matching_auditor_finding: dict | None = None


@dataclass
class HallucinationEvalResult:
    """Complete hallucination detection metrics for one evaluation run."""

    hds: float = 0.0              # Hallucination Detection Score
    bcr: float = 0.0              # BLOCKER Catch Rate
    fpr: float = 0.0              # False Positive Rate (on clean case)
    verdict_accuracy: float = 0.0
    fidelity_correlation: float = 0.0

    total_injections: int = 0
    caught_injections: int = 0
    missed_injections: int = 0

    total_blockers: int = 0
    caught_blockers: int = 0

    false_positives: int = 0      # auditor findings with no corresponding expected finding
    clean_case_findings: int = 0  # all auditor findings on the H-030 clean case

    per_type_accuracy: dict[str, float] = field(default_factory=dict)
    per_tier_hds: dict[int, float] = field(default_factory=dict)
    per_tier_bcr: dict[int, float] = field(default_factory=dict)

    per_case: list[dict] = field(default_factory=list)

    def report_lines(self) -> list[str]:
        lines = [
            "=" * 60,
            "AUDITOR AGENT — HALLUCINATION DETECTION METRICS",
            "=" * 60,
            f"Hallucination Detection Score (HDS):  {self.hds:.4f}",
            f"BLOCKER Catch Rate (BCR):             {self.bcr:.4f}",
            f"False Positive Rate (clean case):     {self.fpr:.4f}",
            f"Verdict Accuracy:                     {self.verdict_accuracy:.4f}",
            f"Fidelity Score Correlation:           {self.fidelity_correlation:+.4f}",
            "",
            f"Injections: {self.caught_injections}/{self.total_injections} caught  "
            f"({self.missed_injections} missed)",
            f"BLOCKER:    {self.caught_blockers}/{self.total_blockers} caught",
            f"False Positives on clean case: {self.false_positives}/{self.clean_case_findings}",
            "",
            "Per FindingType accuracy:",
        ]
        for ft, acc in sorted(self.per_type_accuracy.items()):
            lines.append(f"  {ft:<35} {acc:.3f}")
        lines.append("")
        lines.append("Per Tier HDS / BCR:")
        for tier in sorted(self.per_tier_hds):
            lines.append(
                f"  Tier {tier}: HDS={self.per_tier_hds[tier]:.3f}  "
                f"BCR={self.per_tier_bcr.get(tier, 0.0):.3f}"
            )
        lines.append("=" * 60)
        return lines


# ---------------------------------------------------------------------------
# Core matching logic
# ---------------------------------------------------------------------------

def _finding_matches_expected(auditor_finding, expected: ExpectedFinding) -> bool:
    """A finding is a match when:
      - finding_type exactly matches
      - severity matches OR auditor used BLOCKER when MAJOR was expected
        (stricter is acceptable, but lenient is not)
      - field_path contains the expected field_path as a substring
        (allows 'deterministic_logic[0].value' to match 'deterministic_logic[0]')
    """
    af_type = (
        auditor_finding.finding_type.value
        if hasattr(auditor_finding.finding_type, "value")
        else str(auditor_finding.finding_type)
    )
    af_sev = (
        auditor_finding.severity.value
        if hasattr(auditor_finding.severity, "value")
        else str(auditor_finding.severity)
    )
    af_path = getattr(auditor_finding, "field_path", "") or ""

    expected_type = expected.finding_type.value
    expected_sev = expected.severity.value
    expected_path = expected.field_path

    type_match = af_type == expected_type

    # Severity: exact match OR auditor escalated (BLOCKER instead of MAJOR)
    _SEV_ORDER = {"info": 0, "minor": 1, "major": 2, "blocker": 3}
    sev_match = (
        af_sev == expected_sev
        or _SEV_ORDER.get(af_sev, 0) >= _SEV_ORDER.get(expected_sev, 0)
    )

    # Field path: substring match (auditor may use slightly different pointer)
    path_match = expected_path.split("[")[0] in af_path or af_path in expected_path

    return type_match and sev_match and path_match


def _evaluate_case(
    case: HallucinationCase,
    audit: ComplianceRuleAudit,
) -> dict:
    """Compare one auditor output against a HallucinationCase's expected findings."""
    expected = case.expected_findings
    auditor_findings = audit.findings

    matched_expected: set[int] = set()
    matched_auditor: set[int] = set()

    for ei, exp in enumerate(expected):
        for ai, af in enumerate(auditor_findings):
            if ai in matched_auditor:
                continue
            if _finding_matches_expected(af, exp):
                matched_expected.add(ei)
                matched_auditor.add(ai)
                break

    tp = len(matched_expected)
    fn = len(expected) - tp
    fp = len(auditor_findings) - len(matched_auditor)

    blocker_expected = [e for e in expected if e.severity == Severity.BLOCKER]
    blocker_caught = sum(
        1 for ei, e in enumerate(blocker_expected)
        if any(
            _finding_matches_expected(af, e)
            for ai, af in enumerate(auditor_findings)
        )
    )

    verdict_correct = audit.verdict == case.expected_verdict

    return {
        "case_id": case.case_id,
        "tier": case.tier,
        "injection_summary": case.injection_summary,
        "expected_verdict": case.expected_verdict.value,
        "actual_verdict": audit.verdict.value,
        "verdict_correct": verdict_correct,
        "fidelity_score": audit.fidelity_score,
        "expected_findings": len(expected),
        "caught": tp,
        "missed": fn,
        "false_positives": fp,
        "blocker_expected": len(blocker_expected),
        "blocker_caught": blocker_caught,
        "missed_details": [
            {"type": e.finding_type.value, "severity": e.severity.value, "path": e.field_path}
            for ei, e in enumerate(expected)
            if ei not in matched_expected
        ],
    }


# ---------------------------------------------------------------------------
# Fidelity score correlation
# ---------------------------------------------------------------------------

def _pearson(xs: list[float], ys: list[float]) -> float:
    n = len(xs)
    if n < 2:
        return 0.0
    mx = sum(xs) / n
    my = sum(ys) / n
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    sx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    sy = math.sqrt(sum((y - my) ** 2 for y in ys))
    return cov / (sx * sy) if sx * sy > 0 else 0.0


def _injection_severity_score(case: HallucinationCase) -> float:
    """Numeric severity encoding: higher = more severe injection.
    BLOCKER=1.0, MAJOR=0.67, MINOR=0.33, INFO=0.0, clean=0.0"""
    if not case.expected_findings:
        return 0.0
    _MAP = {"blocker": 1.0, "major": 0.67, "minor": 0.33, "info": 0.0}
    return max(_MAP.get(e.severity.value, 0.0) for e in case.expected_findings)


# ---------------------------------------------------------------------------
# Public evaluator
# ---------------------------------------------------------------------------

class HallucinationMetricsEvaluator:
    """Compute all hallucination detection metrics from a batch of
    (HallucinationCase, ComplianceRuleAudit) pairs."""

    def evaluate(
        self,
        pairs: Sequence[tuple[HallucinationCase, ComplianceRuleAudit]],
    ) -> HallucinationEvalResult:
        per_case = []
        total_inj = caught_inj = total_bl = caught_bl = 0
        verdict_correct_count = 0

        # Per-type tracking
        type_tp: dict[str, int] = {}
        type_total: dict[str, int] = {}

        # For fidelity correlation
        fidelity_scores: list[float] = []
        injection_severities: list[float] = []

        # False-positive tracking on the clean case
        clean_case_findings = 0
        clean_false_positives = 0

        for case, audit in pairs:
            result = _evaluate_case(case, audit)
            per_case.append(result)

            n_exp = result["expected_findings"]
            total_inj += n_exp
            caught_inj += result["caught"]
            total_bl += result["blocker_expected"]
            caught_bl += result["blocker_caught"]

            if result["verdict_correct"]:
                verdict_correct_count += 1

            # Per-type
            for exp in case.expected_findings:
                ft = exp.finding_type.value
                type_total[ft] = type_total.get(ft, 0) + 1

            # Check which types were caught by looking at missed details
            missed_types = {m["type"] for m in result["missed_details"]}
            for exp in case.expected_findings:
                ft = exp.finding_type.value
                if ft not in missed_types:
                    type_tp[ft] = type_tp.get(ft, 0) + 1

            # Fidelity correlation
            fidelity_scores.append(result["fidelity_score"])
            injection_severities.append(_injection_severity_score(case))

            # Clean case (H-030)
            if case.case_id == "H-030":
                clean_case_findings = len(audit.findings)
                clean_false_positives = result["false_positives"]

        n_cases = len(pairs)

        # Tier-level HDS / BCR
        tier_stats: dict[int, dict] = {}
        for case, audit in pairs:
            t = case.tier
            if t not in tier_stats:
                tier_stats[t] = {"caught": 0, "total": 0, "bl_caught": 0, "bl_total": 0}
            r = next(r for r in per_case if r["case_id"] == case.case_id)
            tier_stats[t]["caught"] += r["caught"]
            tier_stats[t]["total"] += r["expected_findings"]
            tier_stats[t]["bl_caught"] += r["blocker_caught"]
            tier_stats[t]["bl_total"] += r["blocker_expected"]

        per_tier_hds = {
            t: v["caught"] / v["total"] if v["total"] > 0 else 1.0
            for t, v in tier_stats.items()
        }
        per_tier_bcr = {
            t: v["bl_caught"] / v["bl_total"] if v["bl_total"] > 0 else 1.0
            for t, v in tier_stats.items()
        }

        per_type_accuracy = {
            ft: type_tp.get(ft, 0) / total
            for ft, total in type_total.items()
        }

        fpr = clean_false_positives / max(clean_case_findings, 1)

        return HallucinationEvalResult(
            hds=caught_inj / max(total_inj, 1),
            bcr=caught_bl / max(total_bl, 1),
            fpr=fpr,
            verdict_accuracy=verdict_correct_count / max(n_cases, 1),
            fidelity_correlation=_pearson(injection_severities, [1.0 - s for s in fidelity_scores]),
            total_injections=total_inj,
            caught_injections=caught_inj,
            missed_injections=total_inj - caught_inj,
            total_blockers=total_bl,
            caught_blockers=caught_bl,
            false_positives=clean_false_positives,
            clean_case_findings=clean_case_findings,
            per_type_accuracy=per_type_accuracy,
            per_tier_hds=per_tier_hds,
            per_tier_bcr=per_tier_bcr,
            per_case=per_case,
        )


# ---------------------------------------------------------------------------
# Regression thresholds (defaults; regression_guard.py can override)
# ---------------------------------------------------------------------------

REGRESSION_THRESHOLDS = {
    "hds": 0.90,           # auditor must catch ≥ 90% of all hallucinations
    "bcr": 0.95,           # must catch ≥ 95% of BLOCKER injections
    "fpr": 0.20,           # false positive rate ≤ 20% on clean case
    "verdict_accuracy": 0.85,
}
