"""Precision, Recall, F1, and Accuracy metrics for the Extraction Agent.

Metric design
-------------
Three independent scorers mirror the three main extraction targets:

  EntityScorer      target_entities  (normalized_entity label)
  ThresholdScorer   deterministic_logic (metric × operator × value × unit × applies_to)
  ObligationScorer  obligation_type  (classification accuracy)

Each scorer accepts a list of ``(ExtractedComplianceRule, GroundTruth)`` pairs
and returns a ``MetricResult`` with per-field and aggregate scores.

Matching strategy
-----------------
We use *soft matching* for entities and thresholds rather than exact equality,
because the extraction agent may produce minor wording variations that are
semantically correct (e.g. "Stockbroker" vs "stockbroker").  The match
functions are explicit so they can be tightened for stricter eval runs.

  Entity match:    normalised lowercase string equality on ``normalized_entity``
  Threshold match: metric (lowercase), operator (==), value (abs diff ≤ 0.01),
                   unit (lowercase equality), applies_to (substring or None)

A threshold is a True Positive only when ALL five fields match simultaneously —
partial matches (right metric but wrong value) count as false-positive/negative
pairs, which is intentionally strict for a financial compliance engine.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Sequence

from app.agents.schemas import ExtractedComplianceRule
from evals.fixtures.sebi_scenarios import GroundTruth, GTEntity, GTThreshold


# ---------------------------------------------------------------------------
# Result container
# ---------------------------------------------------------------------------

@dataclass
class MetricResult:
    """Scores for one scorer across all evaluated scenarios."""

    scorer: str
    precision: float = 0.0
    recall: float = 0.0
    f1: float = 0.0
    accuracy: float = 0.0          # for classification tasks (obligation type)
    true_positives: int = 0
    false_positives: int = 0
    false_negatives: int = 0
    total_scenarios: int = 0
    per_scenario: list[dict] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"[{self.scorer}] "
            f"P={self.precision:.3f}  R={self.recall:.3f}  "
            f"F1={self.f1:.3f}  Acc={self.accuracy:.3f}  "
            f"(TP={self.true_positives} FP={self.false_positives} FN={self.false_negatives} "
            f"n={self.total_scenarios})"
        )


def _safe_div(a: float, b: float) -> float:
    return a / b if b > 0 else 0.0


def _f1(p: float, r: float) -> float:
    return _safe_div(2 * p * r, p + r)


# ---------------------------------------------------------------------------
# Entity scorer
# ---------------------------------------------------------------------------

def _norm(s: str | None) -> str:
    return (s or "").strip().lower()


def _entity_matches(predicted: str | None, gt: str) -> bool:
    """Case-insensitive exact match on normalized entity names."""
    return _norm(predicted) == _norm(gt)


class EntityScorer:
    """Scores extraction of TargetEntity.normalized_entity against ground truth."""

    def score(
        self,
        pairs: Sequence[tuple[ExtractedComplianceRule, GroundTruth]],
    ) -> MetricResult:
        total_tp = total_fp = total_fn = 0
        per_scenario = []

        for extracted, gt in pairs:
            predicted_entities = [_norm(e.normalized_entity) for e in extracted.target_entities]
            gt_entities = [_norm(g.normalized_entity) for g in gt.entities]

            tp = sum(1 for p in predicted_entities if p in gt_entities)
            fp = sum(1 for p in predicted_entities if p not in gt_entities)
            fn = sum(1 for g in gt_entities if g not in predicted_entities)

            total_tp += tp
            total_fp += fp
            total_fn += fn

            scenario_p = _safe_div(tp, tp + fp)
            scenario_r = _safe_div(tp, tp + fn)
            per_scenario.append({
                "rule_id": extracted.rule_id,
                "precision": scenario_p,
                "recall": scenario_r,
                "f1": _f1(scenario_p, scenario_r),
                "predicted": predicted_entities,
                "expected": gt_entities,
                "tp": tp, "fp": fp, "fn": fn,
            })

        precision = _safe_div(total_tp, total_tp + total_fp)
        recall = _safe_div(total_tp, total_tp + total_fn)
        return MetricResult(
            scorer="EntityScorer",
            precision=precision,
            recall=recall,
            f1=_f1(precision, recall),
            accuracy=precision,  # meaningful: what fraction of extracted entities are correct
            true_positives=total_tp,
            false_positives=total_fp,
            false_negatives=total_fn,
            total_scenarios=len(pairs),
            per_scenario=per_scenario,
        )


# ---------------------------------------------------------------------------
# Threshold scorer
# ---------------------------------------------------------------------------

_VALUE_TOLERANCE = 0.01   # allow ±0.01 on numeric values (float comparison noise)


def _threshold_key(t: GTThreshold) -> tuple:
    """Canonical match key for a ground-truth threshold."""
    return (
        _norm(t.metric),
        t.operator,
        t.value,
        _norm(t.unit),
    )


def _extracted_threshold_key(t) -> tuple:
    """Canonical match key for an extracted NumericalThreshold."""
    return (
        _norm(t.metric),
        t.operator.value if hasattr(t.operator, "value") else str(t.operator),
        t.value,
        _norm(t.unit),
    )


def _threshold_matches(extracted_key: tuple, gt_key: tuple) -> bool:
    """All four dimensions must match; value comparison uses tolerance."""
    em, eop, ev, eu = extracted_key
    gm, gop, gv, gu = gt_key
    return (
        em == gm
        and eop == gop
        and abs(ev - gv) <= _VALUE_TOLERANCE
        and eu == gu
    )


class ThresholdScorer:
    """Scores extraction of NumericalThreshold against ground truth.

    A threshold is a TP only when metric, operator, value, AND unit all match.
    This is intentionally strict — an almost-correct threshold in a compliance
    rule is a compliance defect, not a partial credit.
    """

    def score(
        self,
        pairs: Sequence[tuple[ExtractedComplianceRule, GroundTruth]],
    ) -> MetricResult:
        total_tp = total_fp = total_fn = 0
        per_scenario = []

        for extracted, gt in pairs:
            pred_keys = [_extracted_threshold_key(t) for t in extracted.deterministic_logic]
            gt_keys = [_threshold_key(t) for t in gt.thresholds]

            matched_gt: set[int] = set()
            matched_pred: set[int] = set()

            for pi, pk in enumerate(pred_keys):
                for gi, gk in enumerate(gt_keys):
                    if gi in matched_gt:
                        continue
                    if _threshold_matches(pk, gk):
                        matched_gt.add(gi)
                        matched_pred.add(pi)
                        break

            tp = len(matched_gt)
            fp = len(pred_keys) - len(matched_pred)
            fn = len(gt_keys) - len(matched_gt)

            total_tp += tp
            total_fp += fp
            total_fn += fn

            sp = _safe_div(tp, tp + fp)
            sr = _safe_div(tp, tp + fn)
            per_scenario.append({
                "rule_id": extracted.rule_id,
                "precision": sp,
                "recall": sr,
                "f1": _f1(sp, sr),
                "predicted_count": len(pred_keys),
                "expected_count": len(gt_keys),
                "tp": tp, "fp": fp, "fn": fn,
            })

        precision = _safe_div(total_tp, total_tp + total_fp)
        recall = _safe_div(total_tp, total_tp + total_fn)
        return MetricResult(
            scorer="ThresholdScorer",
            precision=precision,
            recall=recall,
            f1=_f1(precision, recall),
            accuracy=precision,
            true_positives=total_tp,
            false_positives=total_fp,
            false_negatives=total_fn,
            total_scenarios=len(pairs),
            per_scenario=per_scenario,
        )


# ---------------------------------------------------------------------------
# Obligation type scorer (classification accuracy)
# ---------------------------------------------------------------------------

class ObligationScorer:
    """Scores the obligation_type classification (MANDATORY/PROHIBITED/etc.).

    Uses multi-class accuracy and a per-class breakdown so we can see whether
    the model confuses mandatory↔prohibited (dangerous) vs other pairs.
    """

    def score(
        self,
        pairs: Sequence[tuple[ExtractedComplianceRule, GroundTruth]],
    ) -> MetricResult:
        correct = 0
        confusion: dict[tuple[str, str], int] = {}  # (predicted, actual) -> count
        per_scenario = []

        for extracted, gt in pairs:
            pred = extracted.obligation_type.value if hasattr(extracted.obligation_type, "value") else str(extracted.obligation_type)
            actual = gt.obligation_type
            is_correct = pred == actual
            if is_correct:
                correct += 1

            key = (pred, actual)
            confusion[key] = confusion.get(key, 0) + 1

            per_scenario.append({
                "rule_id": extracted.rule_id,
                "predicted": pred,
                "expected": actual,
                "correct": is_correct,
            })

        n = len(pairs)
        accuracy = _safe_div(correct, n)

        # Compute macro-averaged P/R/F1 across all obligation types
        labels = list({gt.obligation_type for _, gt in pairs})
        macro_p = macro_r = macro_f1 = 0.0
        for label in labels:
            tp = sum(1 for (p, a), c in confusion.items() if p == label and a == label for _ in range(c))
            fp = sum(c for (p, a), c in confusion.items() if p == label and a != label)
            fn = sum(c for (p, a), c in confusion.items() if p != label and a == label)
            lp = _safe_div(tp, tp + fp)
            lr = _safe_div(tp, tp + fn)
            macro_p += lp
            macro_r += lr
            macro_f1 += _f1(lp, lr)

        nl = max(len(labels), 1)
        return MetricResult(
            scorer="ObligationScorer",
            precision=macro_p / nl,
            recall=macro_r / nl,
            f1=macro_f1 / nl,
            accuracy=accuracy,
            true_positives=correct,
            false_positives=n - correct,
            false_negatives=n - correct,
            total_scenarios=n,
            per_scenario=per_scenario,
        )


# ---------------------------------------------------------------------------
# Confidence calibration scorer
# ---------------------------------------------------------------------------

@dataclass
class CalibrationResult:
    """Expected Calibration Error (ECE) and overconfidence rate."""
    ece: float
    mean_confidence: float
    mean_accuracy: float
    overconfidence_rate: float    # fraction of wrong predictions with confidence > 0.8
    n: int


class ConfidenceCalibrationScorer:
    """Measures whether extraction_confidence correlates with actual correctness.

    A well-calibrated model should have confidence ≈ accuracy in each bucket.
    Overconfident wrong predictions are the most dangerous failure mode.
    """

    def score(
        self,
        pairs: Sequence[tuple[ExtractedComplianceRule, GroundTruth]],
        n_bins: int = 10,
    ) -> CalibrationResult:
        confidences = []
        correctness = []   # 1 if entity + threshold + obligation all match, else 0

        entity_scorer = EntityScorer()
        thresh_scorer = ThresholdScorer()
        oblig_scorer = ObligationScorer()

        for extracted, gt in pairs:
            e_res = entity_scorer.score([(extracted, gt)])
            t_res = thresh_scorer.score([(extracted, gt)])
            o_res = oblig_scorer.score([(extracted, gt)])

            # A scenario is "correct" if all three dimensions exceed 0.8 F1/accuracy
            is_correct = (
                e_res.f1 >= 0.8
                and t_res.f1 >= 0.8
                and o_res.accuracy >= 1.0
            )
            confidences.append(float(extracted.extraction_confidence))
            correctness.append(1 if is_correct else 0)

        # Expected Calibration Error
        bin_size = 1.0 / n_bins
        ece = 0.0
        for b in range(n_bins):
            lo = b * bin_size
            hi = lo + bin_size
            indices = [i for i, c in enumerate(confidences) if lo <= c < hi]
            if not indices:
                continue
            bin_acc = sum(correctness[i] for i in indices) / len(indices)
            bin_conf = sum(confidences[i] for i in indices) / len(indices)
            ece += abs(bin_acc - bin_conf) * len(indices) / len(confidences)

        n = len(pairs)
        wrong_indices = [i for i, c in enumerate(correctness) if c == 0]
        overconfident = sum(1 for i in wrong_indices if confidences[i] > 0.8)

        return CalibrationResult(
            ece=ece,
            mean_confidence=sum(confidences) / max(n, 1),
            mean_accuracy=sum(correctness) / max(n, 1),
            overconfidence_rate=_safe_div(overconfident, len(wrong_indices)),
            n=n,
        )


# ---------------------------------------------------------------------------
# Aggregate runner
# ---------------------------------------------------------------------------

@dataclass
class ExtractionEvalResult:
    entity: MetricResult
    threshold: MetricResult
    obligation: MetricResult
    calibration: CalibrationResult
    overall_f1: float = 0.0

    def __post_init__(self):
        self.overall_f1 = (
            self.entity.f1 + self.threshold.f1 + self.obligation.f1
        ) / 3.0

    def report_lines(self) -> list[str]:
        return [
            "=" * 60,
            "EXTRACTION AGENT EVALUATION RESULTS",
            "=" * 60,
            self.entity.summary(),
            self.threshold.summary(),
            self.obligation.summary(),
            f"[Calibration] ECE={self.calibration.ece:.4f}  "
            f"OverconfidenceRate={self.calibration.overconfidence_rate:.3f}  "
            f"MeanConf={self.calibration.mean_confidence:.3f}  "
            f"MeanAcc={self.calibration.mean_accuracy:.3f}",
            "-" * 60,
            f"OVERALL F1 (macro): {self.overall_f1:.4f}",
            "=" * 60,
        ]


def evaluate_extraction(
    pairs: Sequence[tuple[ExtractedComplianceRule, GroundTruth]],
) -> ExtractionEvalResult:
    """Run all three extraction scorers + calibration in one call."""
    entity = EntityScorer().score(pairs)
    threshold = ThresholdScorer().score(pairs)
    obligation = ObligationScorer().score(pairs)
    calibration = ConfidenceCalibrationScorer().score(pairs)
    return ExtractionEvalResult(
        entity=entity,
        threshold=threshold,
        obligation=obligation,
        calibration=calibration,
    )
