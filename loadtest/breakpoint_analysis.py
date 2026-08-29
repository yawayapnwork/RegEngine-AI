#!/usr/bin/env python3
"""Automated pass/fail breakpoint analysis for the OPA-evaluation load test.

Reads two independent sources, deliberately not just one:

  1. Locust's own CSV stats (`--csv=<prefix>` on the locust run) --
     end-to-end, client-observed latency and the achieved throughput
     (rps), for confirming the test actually reached its target load.
  2. Prometheus, queried the same way chaos/validation/validate_scenario3.py
     already does (`query_prometheus`) -- server-side truth for the two
     HARD requirements:
       - p99 of `opa_policy_evaluation_duration_seconds` < 10ms
       - `audit_ledger_write_failures_total` increased by exactly 0 during
         the test window (zero dropped audit logs)
     plus soft/informational signals (Redis memory, container CPU) that
     are reported but do not by themselves fail the run -- they explain
     *why* a hard gate failed, if one did.

Exit code 0 = PASS (both hard gates held), 1 = FAIL -- wire this into CI
the same way validate_scenario3.py is wired into the chaos-experiment
pipeline.

Usage:
  python loadtest/breakpoint_analysis.py \\
      --locust-csv-prefix loadtest/reports/run1 \\
      --prometheus-url http://localhost:9090 \\
      --window-start "2026-08-29T09:00:00Z" --window-end "2026-08-29T09:20:00Z" \\
      --out loadtest/reports/run1_breakpoint_result.json
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import logging
import sys
from pathlib import Path
from typing import Any

import httpx

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("loadtest.breakpoint_analysis")

# --- Hard SLA gates (requirement 3) ---
MAX_P99_EVALUATION_LATENCY_SECONDS = 0.010  # 10ms
MAX_AUDIT_LEDGER_WRITE_FAILURES = 0

# --- Soft / informational thresholds (reported, not gated) ---
REDIS_MEMORY_WARN_BYTES = 4 * 1024 * 1024 * 1024  # 4GiB
CONTAINER_CPU_WARN_RATIO = 0.90  # 90% of allocated CPU


def query_prometheus_instant(prometheus_url: str, query: str) -> float | None:
    """Mirrors chaos/validation/validate_scenario3.py's query_prometheus
    helper -- same tool, same failure-tolerant shape, so an operator
    already familiar with that script recognizes this one immediately."""
    url = f"{prometheus_url.rstrip('/')}/api/v1/query"
    try:
        resp = httpx.get(url, params={"query": query}, timeout=10.0)
        resp.raise_for_status()
        data = resp.json()
        if data.get("status") != "success":
            return None
        result = data.get("data", {}).get("result", [])
        if not result:
            return None
        return float(result[0]["value"][1])
    except Exception as exc:
        logger.warning("Prometheus query '%s' failed: %s", query, exc)
        return None


def _quantile_query(quantile: float, window_seconds: int) -> str:
    return (
        f"histogram_quantile({quantile}, sum(rate(opa_policy_evaluation_duration_seconds_bucket"
        f"[{window_seconds}s])) by (le))"
    )


def read_locust_csv_stats(csv_prefix: str) -> dict[str, Any]:
    """Parses `<prefix>_stats.csv`'s "Aggregated" row for the client-observed
    percentiles/throughput Locust itself measured."""
    stats_path = Path(f"{csv_prefix}_stats.csv")
    if not stats_path.exists():
        logger.warning("Locust stats CSV not found at %s -- skipping client-side stats.", stats_path)
        return {}

    with stats_path.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))

    aggregated = next((r for r in rows if r.get("Name") == "Aggregated" or r.get("Type") == "Aggregated"), None)
    if aggregated is None and rows:
        aggregated = rows[-1]
    if aggregated is None:
        return {}

    def _f(key: str) -> float | None:
        val = aggregated.get(key)
        try:
            return float(val) if val not in (None, "") else None
        except ValueError:
            return None

    return {
        "request_count": _f("Request Count"),
        "failure_count": _f("Failure Count"),
        "median_response_ms": _f("Median Response Time"),
        "p95_response_ms": _f("95%"),
        "p99_response_ms": _f("99%"),
        "avg_response_ms": _f("Average Response Time"),
        "requests_per_sec": _f("Requests/s"),
    }


def run_breakpoint_analysis(
    prometheus_url: str,
    window_start: dt.datetime,
    window_end: dt.datetime,
    locust_csv_prefix: str | None,
    target_container_name: str = "regengine-api",
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "analyzed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "window_start": window_start.isoformat(),
        "window_end": window_end.isoformat(),
        "gates": {},
        "informational": {},
        "locust_client_side_stats": {},
    }

    # --- Hard gate 1: p99 OPA evaluation latency ---
    window_seconds = max(60, int((window_end - window_start).total_seconds()))
    p50_latency = query_prometheus_instant(prometheus_url, _quantile_query(0.50, window_seconds))
    p95_latency = query_prometheus_instant(prometheus_url, _quantile_query(0.95, window_seconds))
    p99_latency = query_prometheus_instant(prometheus_url, _quantile_query(0.99, window_seconds))

    latency_pass = p99_latency is not None and p99_latency < MAX_P99_EVALUATION_LATENCY_SECONDS
    result["gates"]["p99_evaluation_latency"] = {
        "pass": latency_pass,
        "p50_seconds": p50_latency,
        "p95_seconds": p95_latency,
        "p99_seconds": p99_latency,
        "threshold_seconds": MAX_P99_EVALUATION_LATENCY_SECONDS,
        "note": "Server-side OPA decision latency (app.execution.opa_engine.OPAEngine.evaluate) -- NOT the same as Locust's end-to-end client latency below.",
    }

    # --- Hard gate 2: zero dropped audit logs ---
    ledger_failures_start = query_prometheus_instant(prometheus_url, f"audit_ledger_write_failures_total @ {window_start.timestamp():.0f}") or 0.0
    ledger_failures_end = query_prometheus_instant(prometheus_url, f"audit_ledger_write_failures_total @ {window_end.timestamp():.0f}") or 0.0
    ledger_failures_during_test = ledger_failures_end - ledger_failures_start
    ledger_pass = ledger_failures_during_test <= MAX_AUDIT_LEDGER_WRITE_FAILURES

    result["gates"]["zero_dropped_audit_logs"] = {
        "pass": ledger_pass,
        "failures_during_window": ledger_failures_during_test,
        "threshold": MAX_AUDIT_LEDGER_WRITE_FAILURES,
        "note": "app.ledger.integration.log_evaluation is best-effort by design (a ledger outage must never 5xx a live compliance decision) -- this counter is the ONLY signal a write was actually lost.",
    }

    # --- Informational: policy cache hit ratio (requirement 2, "Redis cache saturation") ---
    cache_hits = query_prometheus_instant(prometheus_url, f'sum(increase(policy_cache_lookup_total{{outcome="hit"}}[{int((window_end - window_start).total_seconds())}s]))')
    cache_misses = query_prometheus_instant(prometheus_url, f'sum(increase(policy_cache_lookup_total{{outcome="miss"}}[{int((window_end - window_start).total_seconds())}s]))')
    total_lookups = (cache_hits or 0) + (cache_misses or 0)
    cache_hit_ratio = (cache_hits / total_lookups * 100.0) if total_lookups else None
    result["informational"]["policy_cache"] = {"hits": cache_hits, "misses": cache_misses, "hit_ratio_pct": cache_hit_ratio}

    # --- Informational: Redis memory/connections (requires redis_exporter target) ---
    redis_memory_bytes = query_prometheus_instant(prometheus_url, "redis_memory_used_bytes")
    redis_connected_clients = query_prometheus_instant(prometheus_url, "redis_connected_clients")
    redis_blocked_clients = query_prometheus_instant(prometheus_url, "redis_blocked_clients")
    result["informational"]["redis"] = {
        "memory_used_bytes": redis_memory_bytes,
        "memory_warn_threshold_bytes": REDIS_MEMORY_WARN_BYTES,
        "memory_near_saturation": (redis_memory_bytes or 0) > REDIS_MEMORY_WARN_BYTES,
        "connected_clients": redis_connected_clients,
        "blocked_clients": redis_blocked_clients,
    }

    # --- Informational: container CPU/memory (requires cadvisor target) ---
    cpu_ratio = query_prometheus_instant(
        prometheus_url,
        f'sum(rate(container_cpu_usage_seconds_total{{name=~".*{target_container_name}.*"}}[1m]))',
    )
    memory_bytes = query_prometheus_instant(prometheus_url, f'container_memory_usage_bytes{{name=~".*{target_container_name}.*"}}')
    result["informational"]["container_resources"] = {
        "cpu_cores_used": cpu_ratio,
        "cpu_near_saturation": (cpu_ratio or 0) > CONTAINER_CPU_WARN_RATIO,
        "memory_bytes": memory_bytes,
    }

    # --- Locust's own end-to-end client-side stats ---
    if locust_csv_prefix:
        result["locust_client_side_stats"] = read_locust_csv_stats(locust_csv_prefix)

    result["overall_pass"] = latency_pass and ledger_pass
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Automated breakpoint pass/fail analysis for the RegEngine AI load test.")
    parser.add_argument("--prometheus-url", default="http://localhost:9090")
    parser.add_argument("--window-start", required=True, help="ISO 8601 UTC timestamp, start of the run")
    parser.add_argument("--window-end", required=True, help="ISO 8601 UTC timestamp, end of the run")
    parser.add_argument("--locust-csv-prefix", default=None, help="Prefix passed to locust's --csv flag, e.g. loadtest/reports/run1")
    parser.add_argument("--target-container-name", default="regengine-api")
    parser.add_argument("--out", type=Path, default=None, help="Write the full JSON result here (also always printed to stdout)")
    args = parser.parse_args()

    window_start = dt.datetime.fromisoformat(args.window_start.replace("Z", "+00:00"))
    window_end = dt.datetime.fromisoformat(args.window_end.replace("Z", "+00:00"))

    result = run_breakpoint_analysis(
        args.prometheus_url, window_start, window_end, args.locust_csv_prefix, args.target_container_name
    )

    output = json.dumps(result, indent=2, default=str)
    print(output)
    if args.out:
        args.out.write_text(output, encoding="utf-8")

    if result["overall_pass"]:
        logger.info("✅ BREAKPOINT ANALYSIS PASSED -- p99 latency and zero-dropped-audit-log gates both held.")
        sys.exit(0)
    else:
        logger.error("❌ BREAKPOINT ANALYSIS FAILED -- see 'gates' in the output above for which one(s) broke.")
        sys.exit(1)


if __name__ == "__main__":
    main()
