#!/usr/bin/env python3
"""Renders `report_template.html` (a plain `string.Template` file --
deliberately no Jinja2 dependency for a single-page report) from a
`breakpoint_analysis.py` JSON result into a shareable HTML report.

Usage:
  python loadtest/report_generator.py \\
      --result loadtest/reports/run1_breakpoint_result.json \\
      --out loadtest/reports/run1_report.html \\
      --run-id run1
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path
from string import Template


def _ms(seconds: float | None) -> str:
    return f"{seconds * 1000:.2f}" if seconds is not None else "n/a"


def _mb(bytes_val: float | None) -> str:
    return f"{bytes_val / (1024 * 1024):.1f}" if bytes_val is not None else "n/a"


def _fmt(value, digits: int = 2) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def render_report(result: dict, run_id: str) -> str:
    template_path = Path(__file__).resolve().parent / "report_template.html"
    template = Template(template_path.read_text(encoding="utf-8"))

    gates = result.get("gates", {})
    latency_gate = gates.get("p99_evaluation_latency", {})
    ledger_gate = gates.get("zero_dropped_audit_logs", {})
    informational = result.get("informational", {})
    locust_stats = result.get("locust_client_side_stats", {}) or {}

    overall_pass = result.get("overall_pass", False)

    substitutions = {
        "run_id": run_id,
        "window_start": result.get("window_start", "n/a"),
        "window_end": result.get("window_end", "n/a"),
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "verdict_class": "pass" if overall_pass else "fail",
        "verdict_label": "PASS" if overall_pass else "FAIL",
        "p99_gate_class": "gate-pass" if latency_gate.get("pass") else "gate-fail",
        "p99_gate_label": "PASS" if latency_gate.get("pass") else "FAIL",
        "p50_latency_ms": _ms(latency_gate.get("p50_seconds")),
        "p95_latency_ms": _ms(latency_gate.get("p95_seconds")),
        "p99_latency_ms": _ms(latency_gate.get("p99_seconds")),
        "ledger_gate_class": "gate-pass" if ledger_gate.get("pass") else "gate-fail",
        "ledger_gate_label": "PASS" if ledger_gate.get("pass") else "FAIL",
        "ledger_failures": _fmt(ledger_gate.get("failures_during_window"), 0),
        "locust_request_count": _fmt(locust_stats.get("request_count"), 0),
        "locust_failure_count": _fmt(locust_stats.get("failure_count"), 0),
        "locust_rps": _fmt(locust_stats.get("requests_per_sec")),
        "locust_median_ms": _fmt(locust_stats.get("median_response_ms")),
        "locust_p95_ms": _fmt(locust_stats.get("p95_response_ms")),
        "locust_p99_ms": _fmt(locust_stats.get("p99_response_ms")),
        "cache_hit_ratio_pct": _fmt(informational.get("policy_cache", {}).get("hit_ratio_pct")),
        "redis_memory_mb": _mb(informational.get("redis", {}).get("memory_used_bytes")),
        "redis_connected_clients": _fmt(informational.get("redis", {}).get("connected_clients"), 0),
        "redis_saturation_note": "⚠ near saturation threshold" if informational.get("redis", {}).get("memory_near_saturation") else "nominal",
        "container_cpu_cores": _fmt(informational.get("container_resources", {}).get("cpu_cores_used")),
        "cpu_saturation_note": "⚠ near saturation threshold" if informational.get("container_resources", {}).get("cpu_near_saturation") else "nominal",
        "container_memory_mb": _mb(informational.get("container_resources", {}).get("memory_bytes")),
        "raw_json": json.dumps(result, indent=2),
    }

    return template.substitute(substitutions)


def main() -> None:
    parser = argparse.ArgumentParser(description="Render a load-test breakpoint-analysis JSON result into an HTML report.")
    parser.add_argument("--result", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--run-id", default="unnamed-run")
    args = parser.parse_args()

    result = json.loads(args.result.read_text(encoding="utf-8"))
    html = render_report(result, args.run_id)
    args.out.write_text(html, encoding="utf-8")
    print(f"Report written to {args.out}")


if __name__ == "__main__":
    main()
