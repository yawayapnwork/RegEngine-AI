"""Requirement 3: automated post-mortem generation. Every
`ChaosMonkeyRunner.run_all()` call writes one of these, unconditionally
-- a run where every defense held is exactly as reportable as one where
something didn't, and a reliability program that only writes reports
for failures will eventually lose the ability to prove its passing
history is real.
"""
from __future__ import annotations

import json
from pathlib import Path

from chaos.monkey.results import ChaosCheckResult, ChaosRunReport


def _status_badge(passed: bool) -> str:
    return "PASS" if passed else "FAIL"


def _render_check(result: ChaosCheckResult) -> str:
    lines = [
        f"### [{_status_badge(result.passed)}] {result.title}",
        "",
        f"- Scenario ID: `{result.scenario_id}`",
        f"- Ran at: {result.ran_at.isoformat()}",
        f"- Summary: {result.summary}",
        "",
        "**Evidence:**",
        "",
        "```json",
        json.dumps(result.evidence, indent=2, default=str),
        "```",
    ]
    return "\n".join(lines)


def render_postmortem(report: ChaosRunReport) -> str:
    duration_s = (report.finished_at - report.started_at).total_seconds()
    overall = _status_badge(report.all_passed)

    header = [
        "# Compliance Chaos Monkey -- Post-Mortem",
        "",
        f"- Run ID: `{report.run_id}`",
        f"- Started: {report.started_at.isoformat()}",
        f"- Finished: {report.finished_at.isoformat()} ({duration_s:.2f}s)",
        f"- Overall result: **{overall}** ({len(report.results) - len(report.failed)}/{len(report.results)} scenarios passed)",
        "",
    ]

    if report.failed:
        header += [
            "## Escaped defects",
            "",
            "The following scenario(s) found a gap where an injected fault was NOT caught or NOT "
            "safely contained. Each is a real finding, not a test artifact -- treat it the way any "
            "other production-readiness gap would be treated before this environment is trusted.",
            "",
        ]
        for r in report.failed:
            header.append(f"- **{r.title}** (`{r.scenario_id}`): {r.summary}")
        header.append("")
    else:
        header += [
            "## Result",
            "",
            "Every injected fault was caught or safely contained by the system's existing defenses. "
            "No corrective action required from this run.",
            "",
        ]

    body = [_render_check(r) for r in report.results]

    return "\n\n".join(header + ["## Scenario detail", ""] + body) + "\n"


def write_postmortem(report: ChaosRunReport, output_dir: str) -> Path:
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"chaos-run-{report.run_id}.md"
    path.write_text(render_postmortem(report), encoding="utf-8")

    json_path = directory / f"chaos-run-{report.run_id}.json"
    json_path.write_text(
        json.dumps(
            {
                "run_id": report.run_id,
                "started_at": report.started_at.isoformat(),
                "finished_at": report.finished_at.isoformat(),
                "all_passed": report.all_passed,
                "results": [
                    {
                        "scenario_id": r.scenario_id,
                        "title": r.title,
                        "passed": r.passed,
                        "summary": r.summary,
                        "evidence": r.evidence,
                        "ran_at": r.ran_at.isoformat(),
                    }
                    for r in report.results
                ],
            },
            indent=2,
            default=str,
        ),
        encoding="utf-8",
    )
    return path
