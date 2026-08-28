"""Pre-flight regression guard — blocks model fine-tuning or prompt updates
when evaluation metrics regress below established baselines.

How it works
------------
1. Loads the most recent baseline results from ``evals/reports/baseline.json``
   (or a path supplied via ``--baseline``).
2. Runs the full evaluation suite (or a specified subset) against the current
   agent code/prompts.
3. Compares every tracked metric against the baseline value, applying per-metric
   tolerance deltas (configurable via ``REGRESSION_DELTAS`` below).
4. Exits with code 0 if all metrics pass (safe to fine-tune / deploy new prompts).
5. Exits with code 1 with a detailed regression report if any metric regresses
   beyond tolerance.

Integration points
------------------
- GitHub Actions CI (``cd.yml``): add a step before any fine-tuning job:
      python -m evals.regression_guard --fail-fast

- Pre-commit hook (fine-tuning scripts):
      python -m evals.regression_guard --suite extraction --baseline evals/reports/baseline.json

- Promote current run to baseline (after human review):
      python -m evals.regression_guard --save-baseline

Metric coverage
---------------
  Extraction suite
    entity_f1           Entity detection F1
    threshold_f1        Numeric threshold detection F1
    obligation_acc      Obligation type classification accuracy
    overall_f1          Macro-average across three dimensions
    calibration_ece     Expected Calibration Error (lower = better)
    overconfidence_rate Fraction of wrong high-confidence predictions (lower = better)

  Hallucination suite
    hds                 Hallucination Detection Score (higher = better)
    bcr                 BLOCKER Catch Rate (higher = better)
    fpr                 False Positive Rate on clean case (lower = better)
    verdict_accuracy    Verdict accuracy (higher = better)
    tier1_hds           HDS on Tier 1 cases (single BLOCKER)
    tier2_hds           HDS on Tier 2 cases (mixed severity)
    tier3_hds           HDS on Tier 3 cases (subtle injections)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import pathlib
import sys
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

REPORTS_DIR = pathlib.Path(__file__).parent / "reports"
DEFAULT_BASELINE_PATH = REPORTS_DIR / "baseline.json"

# ---------------------------------------------------------------------------
# Regression tolerance deltas
#
# Format: { metric_key: (min_allowed_delta, is_lower_better) }
#   min_allowed_delta: how much the metric may DROP (for higher-is-better) or
#                      RISE (for lower-is-better) before being flagged.
#   is_lower_better:   True for ECE, FPR, overconfidence_rate.
#
# These are deliberately conservative — any regression in BLOCKER catch rate
# or hallucination detection is flagged at a 0.02 tolerance (2pp drop).
# ---------------------------------------------------------------------------

@dataclass
class MetricSpec:
    key: str
    label: str
    suite: str               # "extraction" | "hallucination"
    path: list[str]          # JSON path into results.json
    tolerance: float         # allowed delta before regression flag
    lower_is_better: bool = False
    hard_minimum: float | None = None  # absolute floor regardless of baseline


METRIC_SPECS: list[MetricSpec] = [
    # --- Extraction ---
    MetricSpec("entity_f1",        "Entity F1",
               "extraction", ["extraction", "entity", "f1"],
               tolerance=0.03),
    MetricSpec("threshold_f1",     "Threshold F1",
               "extraction", ["extraction", "threshold", "f1"],
               tolerance=0.03),
    MetricSpec("obligation_acc",   "Obligation Accuracy",
               "extraction", ["extraction", "obligation", "accuracy"],
               tolerance=0.03),
    MetricSpec("overall_f1",       "Overall F1 (macro)",
               "extraction", ["extraction", "overall_f1"],
               tolerance=0.03),
    MetricSpec("calibration_ece",  "Calibration ECE",
               "extraction", ["extraction", "calibration", "ece"],
               tolerance=0.02, lower_is_better=True),
    MetricSpec("overconfidence_rate", "Overconfidence Rate",
               "extraction", ["extraction", "calibration", "overconfidence_rate"],
               tolerance=0.05, lower_is_better=True),

    # --- Hallucination ---
    MetricSpec("hds",              "Hallucination Detection Score",
               "hallucination", ["hallucination", "hds"],
               tolerance=0.02, hard_minimum=0.85),
    MetricSpec("bcr",              "BLOCKER Catch Rate",
               "hallucination", ["hallucination", "bcr"],
               tolerance=0.02, hard_minimum=0.90),
    MetricSpec("fpr",              "False Positive Rate",
               "hallucination", ["hallucination", "fpr"],
               tolerance=0.05, lower_is_better=True),
    MetricSpec("verdict_accuracy", "Verdict Accuracy",
               "hallucination", ["hallucination", "verdict_accuracy"],
               tolerance=0.03),
    MetricSpec("tier1_hds",        "Tier 1 HDS (single BLOCKER)",
               "hallucination", ["hallucination", "per_tier_hds", "1"],
               tolerance=0.02, hard_minimum=0.90),
    MetricSpec("tier2_hds",        "Tier 2 HDS (mixed severity)",
               "hallucination", ["hallucination", "per_tier_hds", "2"],
               tolerance=0.03),
    MetricSpec("tier3_hds",        "Tier 3 HDS (subtle injections)",
               "hallucination", ["hallucination", "per_tier_hds", "3"],
               tolerance=0.05),
]


# ---------------------------------------------------------------------------
# JSON path helper
# ---------------------------------------------------------------------------

def _get_path(data: dict, path: list[str]) -> float | None:
    """Walk a nested dict using a list of string keys.  Returns None if any
    key is missing (metric not produced in this run — skip comparison)."""
    cur: Any = data
    for key in path:
        if cur is None:
            return None
        if isinstance(cur, dict):
            cur = cur.get(key)
        else:
            return None
    return float(cur) if cur is not None else None


# ---------------------------------------------------------------------------
# Regression comparison
# ---------------------------------------------------------------------------

@dataclass
class MetricComparison:
    spec: MetricSpec
    baseline_value: float | None
    current_value: float | None
    delta: float | None          # current - baseline (positive = improvement)
    regressed: bool
    reason: str


def _compare_metric(
    spec: MetricSpec,
    baseline: float | None,
    current: float | None,
) -> MetricComparison:
    if current is None:
        return MetricComparison(spec, baseline, current, None, False,
                                "metric not produced in current run — skipped")

    # Hard minimum check (independent of baseline)
    if spec.hard_minimum is not None:
        if not spec.lower_is_better and current < spec.hard_minimum:
            return MetricComparison(spec, baseline, current, None, True,
                                    f"below hard minimum {spec.hard_minimum:.4f}")

    if baseline is None:
        return MetricComparison(spec, baseline, current, None, False,
                                "no baseline available — recording for future comparison")

    delta = current - baseline  # positive = current improved vs baseline

    if spec.lower_is_better:
        # For ECE/FPR: delta > tolerance means current is WORSE
        regressed = delta > spec.tolerance
        reason = (
            f"rose by {delta:+.4f} (tolerance ±{spec.tolerance:.4f})"
            if regressed
            else f"{'improved' if delta < 0 else 'stable'} ({delta:+.4f})"
        )
    else:
        # For F1/accuracy/BCR: delta < -tolerance means current is WORSE
        regressed = delta < -spec.tolerance
        reason = (
            f"dropped by {abs(delta):.4f} (tolerance ±{spec.tolerance:.4f})"
            if regressed
            else f"{'improved' if delta > 0 else 'stable'} ({delta:+.4f})"
        )

    return MetricComparison(spec, baseline, current, delta, regressed, reason)


# ---------------------------------------------------------------------------
# Report rendering
# ---------------------------------------------------------------------------

_GREEN = "\033[92m"
_RED = "\033[91m"
_YELLOW = "\033[93m"
_BOLD = "\033[1m"
_RESET = "\033[0m"


def _colorize(text: str, color: str, use_color: bool) -> str:
    return f"{color}{text}{_RESET}" if use_color else text


def _render_report(
    comparisons: list[MetricComparison],
    manifest: dict,
    baseline_manifest: dict | None,
    use_color: bool = True,
) -> str:
    lines = [
        "",
        _colorize("=" * 70, _BOLD, use_color),
        _colorize("  REGENGINE AI — REGRESSION GUARD REPORT", _BOLD, use_color),
        _colorize("=" * 70, _BOLD, use_color),
        f"  Run ID:       {manifest.get('run_id', 'N/A')}",
        f"  Git SHA:      {manifest.get('git_sha', 'N/A')}",
        f"  Model:        {manifest.get('model', 'N/A')}",
        f"  Started:      {manifest.get('started_at', 'N/A')}",
    ]
    if baseline_manifest:
        lines += [
            f"  Baseline run: {baseline_manifest.get('run_id', 'N/A')}",
            f"  Baseline SHA: {baseline_manifest.get('git_sha', 'N/A')}",
        ]
    else:
        lines.append("  Baseline:     NONE (first run — all metrics will be recorded)")

    lines += ["", _colorize(f"  {'METRIC':<35} {'BASELINE':>10} {'CURRENT':>10} {'DELTA':>10}  STATUS", _BOLD, use_color), "  " + "-" * 70]

    regressions: list[MetricComparison] = []
    for c in comparisons:
        bv = f"{c.baseline_value:.4f}" if c.baseline_value is not None else "   N/A"
        cv = f"{c.current_value:.4f}" if c.current_value is not None else "   N/A"
        dv = f"{c.delta:+.4f}" if c.delta is not None else "   N/A"

        if c.regressed:
            status = _colorize("✘ REGRESSED", _RED, use_color)
            regressions.append(c)
        elif c.baseline_value is None or c.current_value is None:
            status = _colorize("~ SKIPPED", _YELLOW, use_color)
        else:
            status = _colorize("✔ PASS", _GREEN, use_color)

        lines.append(f"  {c.spec.label:<35} {bv:>10} {cv:>10} {dv:>10}  {status}  {c.reason}")

    lines.append("")
    if regressions:
        lines.append(_colorize(f"  ✘  {len(regressions)} metric(s) REGRESSED:", _RED, use_color))
        for r in regressions:
            lines.append(f"     • {r.spec.label}: {r.reason}")
        lines += [
            "",
            _colorize("  ACTION REQUIRED: Fix the regression before fine-tuning or", _RED, use_color),
            _colorize("  deploying prompt changes.  Run evals/runner.py for details.", _RED, use_color),
        ]
    else:
        lines.append(_colorize("  ✔  All metrics passed. Safe to proceed.", _GREEN, use_color))

    lines += ["", _colorize("=" * 70, _BOLD, use_color), ""]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Baseline I/O
# ---------------------------------------------------------------------------

def _load_baseline(path: pathlib.Path) -> dict | None:
    if not path.exists():
        logger.info("No baseline found at %s — treating this as first run.", path)
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _save_baseline(results: dict, path: pathlib.Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, default=str)
    logger.info("Baseline saved to %s", path)


# ---------------------------------------------------------------------------
# Main guard logic
# ---------------------------------------------------------------------------

def _run_comparison(
    current_data: dict,
    baseline_data: dict | None,
    suite: str,
) -> tuple[list[MetricComparison], bool]:
    """Returns (comparisons, any_regression)."""
    applicable = [
        s for s in METRIC_SPECS
        if suite == "both" or s.suite == suite
    ]

    comparisons: list[MetricComparison] = []
    any_regression = False

    for spec in applicable:
        current_val = _get_path(current_data, spec.path)
        baseline_val = _get_path(baseline_data, spec.path) if baseline_data else None
        comp = _compare_metric(spec, baseline_val, current_val)
        comparisons.append(comp)
        if comp.regressed:
            any_regression = True

    return comparisons, any_regression


async def _guard(
    suite: str,
    baseline_path: pathlib.Path,
    limit: int | None,
    dry_run: bool,
    fail_fast: bool,
    save_baseline: bool,
    no_color: bool,
) -> int:
    """Core async guard logic.  Returns exit code (0 = pass, 1 = regression)."""
    from evals.runner import run_eval, _result_to_dict

    logger.info("Running evaluation suite: %s", suite)

    eval_result = await run_eval(
        suite=suite,
        limit=limit,
        dry_run=dry_run,
        write_reports=True,
    )

    current_data = _result_to_dict(eval_result)
    baseline_data = _load_baseline(baseline_path)

    comparisons, any_regression = _run_comparison(
        current_data, baseline_data, suite
    )

    report = _render_report(
        comparisons,
        current_data.get("manifest", {}),
        baseline_data.get("manifest") if baseline_data else None,
        use_color=not no_color and sys.stdout.isatty(),
    )
    print(report)

    # Optionally write the regression report to disk alongside run results
    run_id = current_data.get("manifest", {}).get("run_id", "unknown")
    rpt_path = REPORTS_DIR / run_id / "regression_report.txt"
    rpt_path.parent.mkdir(parents=True, exist_ok=True)
    rpt_path.write_text(report, encoding="utf-8")

    if save_baseline:
        if any_regression:
            logger.warning(
                "--save-baseline requested but regressions detected. "
                "Baseline NOT updated to prevent locking in a degraded state. "
                "Fix regressions first, then re-run with --save-baseline."
            )
        else:
            _save_baseline(current_data, baseline_path)
            logger.info("Baseline promoted to current run results.")

    if any_regression:
        if fail_fast:
            logger.error("Regression detected — exiting immediately (--fail-fast).")
        return 1

    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=(
            "RegEngine AI regression guard — blocks fine-tuning/prompt updates "
            "when agent metrics regress below baseline."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Standard pre-fine-tuning check (both suites, fail fast)
  python -m evals.regression_guard --fail-fast

  # Check extraction only (faster)
  python -m evals.regression_guard --suite extraction

  # Dry-run to validate the guard pipeline without LLM calls
  python -m evals.regression_guard --dry-run

  # Promote current run to baseline after human review
  python -m evals.regression_guard --save-baseline

  # Use a custom baseline
  python -m evals.regression_guard --baseline evals/reports/my_baseline.json
        """,
    )
    p.add_argument(
        "--suite",
        choices=["both", "extraction", "hallucination"],
        default="both",
    )
    p.add_argument(
        "--baseline",
        type=pathlib.Path,
        default=DEFAULT_BASELINE_PATH,
        help=f"Path to baseline JSON (default: {DEFAULT_BASELINE_PATH}).",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Cap scenarios/cases per suite (quick CI check).",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Skip LLM calls; test guard pipeline without API budget.",
    )
    p.add_argument(
        "--fail-fast",
        action="store_true",
        help="Exit immediately on first regression (default: collect all then exit).",
    )
    p.add_argument(
        "--save-baseline",
        action="store_true",
        help="Promote current run to baseline (only if no regressions).",
    )
    p.add_argument(
        "--no-color",
        action="store_true",
        help="Disable ANSI colour codes in report output.",
    )
    p.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    return p


def main() -> None:
    args = _build_parser().parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
        datefmt="%H:%M:%S",
    )

    exit_code = asyncio.run(
        _guard(
            suite=args.suite,
            baseline_path=args.baseline,
            limit=args.limit,
            dry_run=args.dry_run,
            fail_fast=args.fail_fast,
            save_baseline=args.save_baseline,
            no_color=args.no_color,
        )
    )
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
