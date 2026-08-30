"""Async evaluation runner for RegEngine AI agents.

Runs two independent eval suites in parallel:

  Suite A — Extraction Agent benchmark
    Calls ``extract_and_audit_clause`` on all 50 SEBI scenarios, collects the
    ``ExtractedComplianceRule`` output, and scores it against ground truth
    using EntityScorer, ThresholdScorer, ObligationScorer, and
    ConfidenceCalibrationScorer.

  Suite B — Auditor Agent hallucination detection
    Feeds the 30 pre-poisoned ``ExtractedComplianceRule`` objects (from
    ``hallucination_injections.py``) directly to the Auditor Agent (bypassing
    the extractor so we test the auditor in isolation) and measures HDS, BCR,
    FPR, VerdictAccuracy, and FidelityCorrelation.

Both suites write:
  evals/reports/<run_id>/results.json    — full machine-readable results
  evals/reports/<run_id>/report.html     — human-readable HTML dashboard
  evals/reports/latest.json             — symlink/copy to the most recent run

The run_id is ``YYYY-MM-DDTHH-MM-SS`` so runs are naturally sorted.

Usage
-----
  # Run the full suite (both A and B)
  python -m evals.runner

  # Extraction only
  python -m evals.runner --suite extraction

  # Hallucination only, limit to 10 cases
  python -m evals.runner --suite hallucination --limit 10

  # Dry-run: load fixtures and validate schemas without calling the LLM
  python -m evals.runner --dry-run

The runner is also importable so regression_guard.py and CI scripts can call
``run_eval()`` programmatically.
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import logging
import os
import pathlib
import sys
import traceback
import uuid
from dataclasses import asdict, dataclass
from typing import Any

# ---------------------------------------------------------------------------
# Lazy imports so the module loads without crewai/huggingface_hub in dry-run mode
# ---------------------------------------------------------------------------
logger = logging.getLogger(__name__)

REPORTS_DIR = pathlib.Path(__file__).parent / "reports"
REPORTS_DIR.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Data containers for one run
# ---------------------------------------------------------------------------

@dataclass
class RunManifest:
    run_id: str
    started_at: str
    finished_at: str
    suite: str                  # "both" | "extraction" | "hallucination"
    scenario_count: int
    injection_count: int
    git_sha: str | None
    model: str
    python_version: str


@dataclass
class EvalRunResult:
    manifest: RunManifest
    extraction: dict | None     # ExtractionEvalResult serialised
    hallucination: dict | None  # HallucinationEvalResult serialised
    errors: list[dict]          # per-scenario/per-case error log


# ---------------------------------------------------------------------------
# Schema adapter: ClauseChunk builder from ScenarioFixture
# ---------------------------------------------------------------------------

def _make_chunk(scenario):
    """Build an ``app.models.ClauseChunk`` from a ``ScenarioFixture``."""
    from app.models import ClauseChunk  # imported here to avoid module-level crewai load
    import hashlib

    sha = hashlib.sha256(scenario.clause_text.encode()).hexdigest()
    return ClauseChunk(
        chunk_id=f"eval::{scenario.scenario_id}",
        circular_number=scenario.circular_number,
        clause_number=scenario.clause_number,
        section_path=[scenario.category],
        text=scenario.clause_text,
        sha256=sha,
        page_start=None,
        page_end=None,
        element_kind="clause",
        contains_table=(scenario.category == "D"),
    )


# ---------------------------------------------------------------------------
# Suite A: Extraction Agent evaluation
# ---------------------------------------------------------------------------

async def _run_extraction_suite(
    scenarios,
    settings,
    limit: int | None,
    dry_run: bool,
) -> tuple[list[tuple], list[dict]]:
    """Returns (pairs, errors) where pairs = [(ExtractedComplianceRule, GroundTruth)]."""
    from evals.fixtures.sebi_scenarios import ALL_SCENARIOS

    selected = scenarios[:limit] if limit else scenarios
    pairs: list[tuple] = []
    errors: list[dict] = []

    semaphore = asyncio.Semaphore(3)  # respect Hugging Face Inference rate limit

    async def _run_one(scenario):
        async with semaphore:
            if dry_run:
                # Return a dummy rule that passes schema validation
                from app.agents.schemas import (
                    ExtractedComplianceRule, ObligationType,
                )
                dummy = ExtractedComplianceRule(
                    rule_id=f"dry::{scenario.scenario_id}",
                    source_chunk_id=f"chunk::{scenario.scenario_id}",
                    source_sha256="a" * 64,
                    circular_number=scenario.circular_number,
                    clause_number=scenario.clause_number,
                    obligation_type=ObligationType.MANDATORY,
                    extraction_confidence=0.5,
                )
                return (dummy, scenario.ground_truth, None)

            try:
                from app.agents.pipeline import extract_and_audit_clause
                chunk = _make_chunk(scenario)
                audited = await extract_and_audit_clause(chunk, [], settings)
                return (audited.rule, scenario.ground_truth, None)
            except Exception as exc:
                logger.error(
                    "Extraction failed for scenario %s: %s",
                    scenario.scenario_id, exc,
                )
                return (None, scenario.ground_truth, {
                    "scenario_id": scenario.scenario_id,
                    "error": str(exc),
                    "traceback": traceback.format_exc(),
                })

    results = await asyncio.gather(*[_run_one(s) for s in selected])

    for rule, gt, err in results:
        if err:
            errors.append(err)
        elif rule is not None:
            pairs.append((rule, gt))

    return pairs, errors


# ---------------------------------------------------------------------------
# Suite B: Auditor Agent hallucination detection
# ---------------------------------------------------------------------------

async def _run_hallucination_suite(
    injections,
    settings,
    limit: int | None,
    dry_run: bool,
) -> tuple[list[tuple], list[dict]]:
    """Returns (pairs, errors) where pairs = [(HallucinationCase, ComplianceRuleAudit)]."""
    selected = injections[:limit] if limit else injections
    pairs: list[tuple] = []
    errors: list[dict] = []

    semaphore = asyncio.Semaphore(3)

    async def _run_one(case):
        async with semaphore:
            if dry_run:
                from app.agents.schemas import (
                    AuditVerdict, ComplianceRuleAudit,
                )
                dummy_audit = ComplianceRuleAudit(
                    rule_id=case.case_id,
                    verdict=AuditVerdict.APPROVED,
                    fidelity_score=0.5,
                    verified_quote_count=0,
                    unverified_quote_count=0,
                    findings=[],
                )
                return (case, dummy_audit, None)

            try:
                from app.agents.crew import build_audit_agent, build_audit_task
                from crewai import Crew, Process

                # Build a minimal chunk wrapper for the auditor task
                chunk = _make_chunk_from_case(case)

                audit_agent = build_audit_agent(settings)

                # Create a mock extraction task output using the poisoned rule
                class _FakeTask:
                    class output:
                        @staticmethod
                        def get_pydantic():
                            return case.poisoned_extraction

                    # CrewAI context injection
                    def __init__(self):
                        self.output = type("O", (), {
                            "pydantic": case.poisoned_extraction,
                            "raw": case.poisoned_extraction.model_dump_json(),
                        })()

                fake_extraction_task = _FakeTask()
                audit_task = build_audit_task(
                    audit_agent, chunk, fake_extraction_task, []
                )

                crew = Crew(
                    agents=[audit_agent],
                    tasks=[audit_task],
                    process=Process.sequential,
                    memory=False,
                    cache=False,
                    verbose=settings.agent_verbose,
                    max_rpm=settings.agent_max_rpm,
                )
                crew.kickoff()

                from app.agents.schemas import ComplianceRuleAudit
                audit: ComplianceRuleAudit = audit_task.output.pydantic
                return (case, audit, None)

            except Exception as exc:
                logger.error(
                    "Auditor eval failed for case %s: %s",
                    case.case_id, exc,
                )
                return (case, None, {
                    "case_id": case.case_id,
                    "error": str(exc),
                    "traceback": traceback.format_exc(),
                })

    results = await asyncio.gather(*[_run_one(c) for c in selected])

    for case, audit, err in results:
        if err:
            errors.append(err)
        elif audit is not None:
            pairs.append((case, audit))

    return pairs, errors


def _make_chunk_from_case(case):
    """Minimal ClauseChunk from a HallucinationCase."""
    from app.models import ClauseChunk
    import hashlib

    sha = hashlib.sha256(case.source_text.encode()).hexdigest()
    return ClauseChunk(
        chunk_id=f"hal::{case.case_id}",
        circular_number="SEBI/HO/EVAL/CIR/2026/99",
        clause_number=case.case_id,
        section_path=["hallucination_eval"],
        text=case.source_text,
        sha256=sha,
        page_start=None,
        page_end=None,
        element_kind="clause",
        contains_table=False,
    )


# ---------------------------------------------------------------------------
# Report serialisation helpers
# ---------------------------------------------------------------------------

def _result_to_dict(obj) -> Any:
    """Recursively convert dataclasses / pydantic models to plain dicts."""
    if hasattr(obj, "model_dump"):
        return obj.model_dump(mode="json")
    if hasattr(obj, "__dataclass_fields__"):
        return {k: _result_to_dict(v) for k, v in asdict(obj).items()}
    if isinstance(obj, dict):
        return {k: _result_to_dict(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_result_to_dict(v) for v in obj]
    return obj


def _write_json(path: pathlib.Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, default=str)
    logger.info("Wrote JSON report: %s", path)


# ---------------------------------------------------------------------------
# HTML report renderer
# ---------------------------------------------------------------------------

_HTML_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>RegEngine AI Agent Eval — {run_id}</title>
  <style>
    body  {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
             background:#f8f9fa; color:#212529; margin:0; padding:24px; }}
    h1   {{ color:#003366; border-bottom:3px solid #FF6600; padding-bottom:8px; }}
    h2   {{ color:#003366; margin-top:32px; }}
    .kpi {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(160px,1fr));
             gap:12px; margin:16px 0; }}
    .kpi-card {{ background:#fff; border:1px solid #dee2e6; border-radius:8px;
                 padding:16px; text-align:center; }}
    .kpi-val  {{ font-size:2rem; font-weight:700; color:#003366; }}
    .kpi-lbl  {{ font-size:.8rem; color:#6c757d; margin-top:4px; }}
    .pass  {{ color:#006633; font-weight:700; }}
    .fail  {{ color:#CC0000; font-weight:700; }}
    .warn  {{ color:#FF8C00; font-weight:700; }}
    table {{ border-collapse:collapse; width:100%; margin-top:12px;
             background:#fff; border:1px solid #dee2e6; border-radius:6px;
             overflow:hidden; font-size:.875rem; }}
    th    {{ background:#003366; color:#fff; padding:8px 12px; text-align:left; }}
    td    {{ padding:8px 12px; border-bottom:1px solid #dee2e6; }}
    tr:last-child td {{ border-bottom:none; }}
    tr:hover td {{ background:#f0f4f8; }}
    .badge {{ display:inline-block; padding:2px 8px; border-radius:4px;
              font-size:.75rem; font-weight:600; }}
    .badge-pass {{ background:#d4edda; color:#155724; }}
    .badge-fail {{ background:#f8d7da; color:#721c24; }}
    .badge-warn {{ background:#fff3cd; color:#856404; }}
    .meta-grid {{ display:grid; grid-template-columns:1fr 1fr; gap:8px;
                  background:#fff; border:1px solid #dee2e6; border-radius:6px;
                  padding:16px; margin:16px 0; font-size:.875rem; }}
    .meta-grid dt {{ font-weight:600; color:#495057; }}
    .meta-grid dd {{ color:#212529; margin:0; }}
    .error-box {{ background:#fff5f5; border:1px solid #f5c6cb; border-radius:6px;
                  padding:12px; margin:8px 0; font-size:.8rem; color:#721c24; }}
    footer {{ margin-top:40px; font-size:.75rem; color:#6c757d;
              border-top:1px solid #dee2e6; padding-top:12px; }}
  </style>
</head>
<body>
<h1>RegEngine AI — Agent Evaluation Report</h1>

<dl class="meta-grid">
  <dt>Run ID</dt>        <dd>{run_id}</dd>
  <dt>Suite</dt>         <dd>{suite}</dd>
  <dt>Started</dt>       <dd>{started_at}</dd>
  <dt>Finished</dt>      <dd>{finished_at}</dd>
  <dt>Model</dt>         <dd>{model}</dd>
  <dt>Git SHA</dt>       <dd>{git_sha}</dd>
  <dt>Scenarios</dt>     <dd>{scenario_count}</dd>
  <dt>Injections</dt>    <dd>{injection_count}</dd>
</dl>

{extraction_section}

{hallucination_section}

{errors_section}

<footer>Generated by evals/runner.py · RegEngine AI evaluation framework</footer>
</body>
</html>
"""

_EXTRACTION_SECTION = """\
<h2>Suite A — Extraction Agent</h2>
<div class="kpi">
  <div class="kpi-card"><div class="kpi-val {ef1_cls}">{entity_f1}</div>
    <div class="kpi-lbl">Entity F1</div></div>
  <div class="kpi-card"><div class="kpi-val {tf1_cls}">{thresh_f1}</div>
    <div class="kpi-lbl">Threshold F1</div></div>
  <div class="kpi-card"><div class="kpi-val {oa_cls}">{oblig_acc}</div>
    <div class="kpi-lbl">Obligation Accuracy</div></div>
  <div class="kpi-card"><div class="kpi-val">{overall_f1}</div>
    <div class="kpi-lbl">Overall F1 (macro)</div></div>
  <div class="kpi-card"><div class="kpi-val">{ece}</div>
    <div class="kpi-lbl">Calibration ECE</div></div>
  <div class="kpi-card"><div class="kpi-val {ocr_cls}">{overcnf_rate}</div>
    <div class="kpi-lbl">Overconfidence Rate</div></div>
</div>
<table>
  <thead><tr><th>Scorer</th><th>Precision</th><th>Recall</th>
    <th>F1</th><th>TP</th><th>FP</th><th>FN</th></tr></thead>
  <tbody>
    {extraction_rows}
  </tbody>
</table>
"""

_HALLUCINATION_SECTION = """\
<h2>Suite B — Auditor Agent Hallucination Detection</h2>
<div class="kpi">
  <div class="kpi-card"><div class="kpi-val {hds_cls}">{hds}</div>
    <div class="kpi-lbl">HDS (Detection Score)</div></div>
  <div class="kpi-card"><div class="kpi-val {bcr_cls}">{bcr}</div>
    <div class="kpi-lbl">BLOCKER Catch Rate</div></div>
  <div class="kpi-card"><div class="kpi-val {fpr_cls}">{fpr}</div>
    <div class="kpi-lbl">False Positive Rate</div></div>
  <div class="kpi-card"><div class="kpi-val {va_cls}">{verdict_acc}</div>
    <div class="kpi-lbl">Verdict Accuracy</div></div>
  <div class="kpi-card"><div class="kpi-val">{fidelity_corr}</div>
    <div class="kpi-lbl">Fidelity Correlation</div></div>
</div>
<h3>Per-Tier Results</h3>
<table>
  <thead><tr><th>Tier</th><th>HDS</th><th>BCR</th><th>Description</th></tr></thead>
  <tbody>
    {tier_rows}
  </tbody>
</table>
<h3>Per FindingType Accuracy</h3>
<table>
  <thead><tr><th>Finding Type</th><th>Accuracy</th><th>Status</th></tr></thead>
  <tbody>
    {type_rows}
  </tbody>
</table>
<h3>Case Details</h3>
<table>
  <thead><tr><th>Case ID</th><th>Tier</th><th>Injection</th><th>Expected Verdict</th>
    <th>Actual Verdict</th><th>Caught</th><th>Missed</th><th>FP</th></tr></thead>
  <tbody>
    {case_rows}
  </tbody>
</table>
"""


def _cls(val: float, good: float, bad: float, invert: bool = False) -> str:
    """Return CSS class based on threshold comparison."""
    if invert:
        return "pass" if val <= good else ("warn" if val <= bad else "fail")
    return "pass" if val >= good else ("warn" if val >= bad else "fail")


def _pct(v: float) -> str:
    return f"{v*100:.1f}%"


def _render_html(result: EvalRunResult) -> str:
    m = result.manifest

    # --- Extraction section ---
    ext_section = ""
    if result.extraction:
        e = result.extraction
        ent = e.get("entity", {})
        thr = e.get("threshold", {})
        obl = e.get("obligation", {})
        cal = e.get("calibration", {})
        ef1 = ent.get("f1", 0.0)
        tf1 = thr.get("f1", 0.0)
        oa = obl.get("accuracy", 0.0)
        ovf = e.get("overall_f1", 0.0)
        ece = cal.get("ece", 0.0)
        ocr = cal.get("overconfidence_rate", 0.0)

        rows = ""
        for scorer, d in [("Entity", ent), ("Threshold", thr), ("Obligation", obl)]:
            rows += (
                f"<tr><td>{scorer}</td>"
                f"<td>{d.get('precision',0):.3f}</td>"
                f"<td>{d.get('recall',0):.3f}</td>"
                f"<td class='{_cls(d.get('f1',0),0.85,0.70)}'>{d.get('f1',0):.3f}</td>"
                f"<td>{d.get('true_positives',0)}</td>"
                f"<td>{d.get('false_positives',0)}</td>"
                f"<td>{d.get('false_negatives',0)}</td></tr>"
            )
        ext_section = _EXTRACTION_SECTION.format(
            entity_f1=f"{ef1:.3f}", ef1_cls=_cls(ef1, 0.85, 0.70),
            thresh_f1=f"{tf1:.3f}", tf1_cls=_cls(tf1, 0.85, 0.70),
            oblig_acc=f"{oa:.3f}", oa_cls=_cls(oa, 0.90, 0.75),
            overall_f1=f"{ovf:.3f}",
            ece=f"{ece:.4f}",
            overcnf_rate=_pct(ocr), ocr_cls=_cls(ocr, 0.20, 0.35, invert=True),
            extraction_rows=rows,
        )

    # --- Hallucination section ---
    hal_section = ""
    if result.hallucination:
        h = result.hallucination
        hds = h.get("hds", 0.0)
        bcr = h.get("bcr", 0.0)
        fpr = h.get("fpr", 0.0)
        va = h.get("verdict_accuracy", 0.0)
        fc = h.get("fidelity_correlation", 0.0)

        tier_rows = ""
        for tier, thds in sorted(h.get("per_tier_hds", {}).items()):
            tbcr = h.get("per_tier_bcr", {}).get(tier, 0.0)
            desc = {1: "Single BLOCKER", 2: "Mixed severity", 3: "Subtle injections"}.get(tier, "")
            tier_rows += (
                f"<tr><td>{tier}</td>"
                f"<td class='{_cls(thds,0.90,0.75)}'>{thds:.3f}</td>"
                f"<td class='{_cls(tbcr,0.95,0.80)}'>{tbcr:.3f}</td>"
                f"<td>{desc}</td></tr>"
            )

        type_rows = ""
        for ft, acc in sorted(h.get("per_type_accuracy", {}).items()):
            status = (
                "<span class='badge badge-pass'>PASS</span>" if acc >= 0.85
                else "<span class='badge badge-warn'>WARN</span>" if acc >= 0.65
                else "<span class='badge badge-fail'>FAIL</span>"
            )
            type_rows += (
                f"<tr><td>{ft}</td>"
                f"<td class='{_cls(acc,0.85,0.65)}'>{acc:.3f}</td>"
                f"<td>{status}</td></tr>"
            )

        case_rows = ""
        for c in h.get("per_case", []):
            badge_cls = "badge-pass" if c["verdict_correct"] else "badge-fail"
            case_rows += (
                f"<tr>"
                f"<td>{c['case_id']}</td><td>{c['tier']}</td>"
                f"<td style='font-size:.8rem'>{c['injection_summary'][:60]}</td>"
                f"<td>{c['expected_verdict']}</td>"
                f"<td class='{'pass' if c['verdict_correct'] else 'fail'}'>{c['actual_verdict']}</td>"
                f"<td>{c['caught']}/{c['expected_findings']}</td>"
                f"<td class='{'pass' if c['missed']==0 else 'fail'}'>{c['missed']}</td>"
                f"<td class='{'pass' if c['false_positives']==0 else 'warn'}'>{c['false_positives']}</td>"
                f"</tr>"
            )

        hal_section = _HALLUCINATION_SECTION.format(
            hds=f"{hds:.3f}", hds_cls=_cls(hds, 0.90, 0.75),
            bcr=f"{bcr:.3f}", bcr_cls=_cls(bcr, 0.95, 0.80),
            fpr=_pct(fpr), fpr_cls=_cls(fpr, 0.20, 0.35, invert=True),
            verdict_acc=f"{va:.3f}", va_cls=_cls(va, 0.85, 0.70),
            fidelity_corr=f"{fc:+.3f}",
            tier_rows=tier_rows,
            type_rows=type_rows,
            case_rows=case_rows,
        )

    # --- Errors section ---
    errors_section = ""
    if result.errors:
        errors_section = "<h2>Errors</h2>"
        for err in result.errors:
            sid = err.get("scenario_id") or err.get("case_id", "unknown")
            errors_section += (
                f"<div class='error-box'><strong>{sid}</strong>: "
                f"{err.get('error','')}</div>"
            )

    return _HTML_TEMPLATE.format(
        run_id=m.run_id,
        suite=m.suite,
        started_at=m.started_at,
        finished_at=m.finished_at,
        model=m.model,
        git_sha=m.git_sha or "unknown",
        scenario_count=m.scenario_count,
        injection_count=m.injection_count,
        extraction_section=ext_section,
        hallucination_section=hal_section,
        errors_section=errors_section,
    )


def _write_html(path: pathlib.Path, html: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html, encoding="utf-8")
    logger.info("Wrote HTML report: %s", path)


# ---------------------------------------------------------------------------
# Git SHA helper
# ---------------------------------------------------------------------------

def _git_sha() -> str | None:
    try:
        import subprocess
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            stderr=subprocess.DEVNULL,
        ).decode().strip()
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Main async entry point
# ---------------------------------------------------------------------------

async def run_eval(
    suite: str = "both",
    limit: int | None = None,
    dry_run: bool = False,
    run_id: str | None = None,
    write_reports: bool = True,
) -> EvalRunResult:
    """Run the evaluation suite and return an ``EvalRunResult``.

    Parameters
    ----------
    suite:
        ``"both"`` | ``"extraction"`` | ``"hallucination"``
    limit:
        Cap the number of scenarios/cases per suite (useful for quick CI runs).
    dry_run:
        Skip all LLM calls; return dummy outputs to validate the reporting
        pipeline without spending API budget.
    run_id:
        If not provided, one is generated from the current timestamp.
    write_reports:
        Write JSON + HTML reports to ``evals/reports/<run_id>/``.
    """
    from evals.fixtures.hallucination_injections import ALL_INJECTIONS
    from evals.fixtures.sebi_scenarios import ALL_SCENARIOS
    from evals.metrics.extraction_metrics import evaluate_extraction
    from evals.metrics.hallucination_metrics import HallucinationMetricsEvaluator

    if not dry_run:
        from app.config import get_settings
        settings = get_settings()
    else:
        settings = type("S", (), {
            "agent_verbose": False, "agent_max_rpm": 20,
            "hf_api_token": "dry-run",
        })()

    run_id = run_id or dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H-%M-%S")
    started_at = dt.datetime.now(dt.timezone.utc).isoformat()
    all_errors: list[dict] = []

    extraction_result_dict: dict | None = None
    hallucination_result_dict: dict | None = None

    # --- Suite A ---
    if suite in ("both", "extraction"):
        logger.info("Starting extraction eval (%d scenarios%s)...",
                    len(ALL_SCENARIOS[:limit] if limit else ALL_SCENARIOS),
                    " [DRY RUN]" if dry_run else "")
        pairs, errs = await _run_extraction_suite(ALL_SCENARIOS, settings, limit, dry_run)
        all_errors.extend(errs)
        if pairs:
            result = evaluate_extraction(pairs)
            extraction_result_dict = _result_to_dict(result)
            for line in result.report_lines():
                logger.info(line)

    # --- Suite B ---
    if suite in ("both", "hallucination"):
        logger.info("Starting hallucination eval (%d cases%s)...",
                    len(ALL_INJECTIONS[:limit] if limit else ALL_INJECTIONS),
                    " [DRY RUN]" if dry_run else "")
        pairs_h, errs_h = await _run_hallucination_suite(ALL_INJECTIONS, settings, limit, dry_run)
        all_errors.extend(errs_h)
        if pairs_h:
            result_h = HallucinationMetricsEvaluator().evaluate(pairs_h)
            hallucination_result_dict = _result_to_dict(result_h)
            for line in result_h.report_lines():
                logger.info(line)

    finished_at = dt.datetime.now(dt.timezone.utc).isoformat()

    manifest = RunManifest(
        run_id=run_id,
        started_at=started_at,
        finished_at=finished_at,
        suite=suite,
        scenario_count=len(ALL_SCENARIOS[:limit] if limit else ALL_SCENARIOS),
        injection_count=len(ALL_INJECTIONS[:limit] if limit else ALL_INJECTIONS),
        git_sha=_git_sha(),
        model="huggingface/Qwen/Qwen2.5-72B-Instruct" + (" (dry-run)" if dry_run else ""),
        python_version=sys.version,
    )

    eval_result = EvalRunResult(
        manifest=manifest,
        extraction=extraction_result_dict,
        hallucination=hallucination_result_dict,
        errors=all_errors,
    )

    if write_reports:
        run_dir = REPORTS_DIR / run_id
        run_dir.mkdir(parents=True, exist_ok=True)

        _write_json(run_dir / "results.json", _result_to_dict(eval_result))

        html = _render_html(eval_result)
        _write_html(run_dir / "report.html", html)

        # Update latest.json symlink / copy
        latest = REPORTS_DIR / "latest.json"
        _write_json(latest, _result_to_dict(eval_result))
        logger.info("Run complete. Reports in: %s", run_dir)

    return eval_result


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="RegEngine AI agent evaluation runner.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--suite",
        choices=["both", "extraction", "hallucination"],
        default="both",
        help="Which eval suite to run (default: both).",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Cap scenarios/cases per suite (for quick CI runs).",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Skip LLM calls; validate pipeline without spending API budget.",
    )
    p.add_argument(
        "--run-id",
        default=None,
        help="Override the auto-generated run ID.",
    )
    p.add_argument(
        "--no-reports",
        action="store_true",
        help="Skip writing JSON/HTML reports to disk.",
    )
    p.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    return p


def main():
    args = _build_parser().parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
        datefmt="%H:%M:%S",
    )

    result = asyncio.run(
        run_eval(
            suite=args.suite,
            limit=args.limit,
            dry_run=args.dry_run,
            run_id=args.run_id,
            write_reports=not args.no_reports,
        )
    )

    if result.errors:
        logger.warning("%d scenario(s)/case(s) errored during evaluation.", len(result.errors))
        sys.exit(2)


if __name__ == "__main__":
    main()
