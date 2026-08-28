"""Compliance Performance & Accuracy Validation Runner for RegEngine AI.

Runs synthetic SEBI order streams through the evaluation engine (in-memory or via
live HTTP API), measures decision classification accuracy (TP, FP, TN, FN, Precision,
Recall, F1 Score), and profiles response latency (p50, p90, p95, p99 ms) under load.

Exports:
  - evals/reports/compliance_validation_<timestamp>.json (Machine-readable)
  - evals/reports/compliance_validation_<timestamp>.html (Human-readable HTML Dashboard)

Usage:
  python evals/compliance_validation_runner.py --count 500 --scenario mixed_market_stream
  python evals/compliance_validation_runner.py --dry-run
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import logging
import math
import os
import random
import sys
import time
from dataclasses import asdict
from typing import Any

import httpx

# Add project root to sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from evals.synthetic_trade_generator import ScenarioType, SyntheticTrade, SyntheticTradeGenerator

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("compliance_validation_runner")


HTML_REPORT_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>RegEngine AI — Compliance Validation & Evaluation Report</title>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
  <style>
    :root {{
      --primary: #4f46e5;
      --bg: #0f172a;
      --card-bg: #1e293b;
      --text: #f8fafc;
      --text-muted: #94a3b8;
      --border: #334155;
      --pass: #10b981;
      --warn: #f59e0b;
      --fail: #ef4444;
    }}
    body {{
      margin: 0;
      padding: 40px;
      font-family: 'Inter', sans-serif;
      background: var(--bg);
      color: var(--text);
    }}
    .container {{
      max-width: 1200px;
      margin: 0 auto;
    }}
    .header {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      border-bottom: 1px solid var(--border);
      padding-bottom: 20px;
      margin-bottom: 30px;
    }}
    h1 {{ font-size: 28px; margin: 0; font-weight: 700; color: #fff; }}
    .timestamp {{ color: var(--text-muted); font-size: 14px; }}
    .grid {{
      display: grid;
      grid-template-columns: repeat(4, 1fr);
      gap: 20px;
      margin-bottom: 30px;
    }}
    .card {{
      background: var(--card-bg);
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 20px;
    }}
    .card-title {{ font-size: 13px; text-transform: uppercase; color: var(--text-muted); font-weight: 600; letter-spacing: 0.5px; }}
    .card-value {{ font-size: 32px; font-weight: 700; margin-top: 10px; }}
    .val-pass {{ color: var(--pass); }}
    .val-warn {{ color: var(--warn); }}
    .val-fail {{ color: var(--fail); }}
    .section {{
      background: var(--card-bg);
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 24px;
      margin-bottom: 30px;
    }}
    h2 {{ font-size: 20px; margin-top: 0; margin-bottom: 20px; border-bottom: 1px solid var(--border); padding-bottom: 10px; }}
    table {{ width: 100%; border-collapse: collapse; text-align: left; font-size: 14px; }}
    th, td {{ padding: 12px 16px; border-bottom: 1px solid var(--border); }}
    th {{ color: var(--text-muted); font-weight: 600; background: rgba(0,0,0,0.2); }}
    code {{ font-family: 'JetBrains Mono', monospace; font-size: 13px; background: rgba(0,0,0,0.3); padding: 2px 6px; border-radius: 4px; }}
  </style>
</head>
<body>
  <div class="container">
    <div class="header">
      <div>
        <h1>RegEngine AI — Compliance Validation Dashboard</h1>
        <div class="timestamp">Synthetic SEBI Order Stream Evaluation | Executed: {timestamp}</div>
      </div>
      <div>
        <span style="background: var(--primary); padding: 8px 16px; border-radius: 20px; font-size: 14px; font-weight: 600;">Scenario: {scenario}</span>
      </div>
    </div>

    <div class="grid">
      <div class="card">
        <div class="card-title">Accuracy</div>
        <div class="card-value val-pass">{accuracy_pct:.1f}%</div>
      </div>
      <div class="card">
        <div class="card-title">Precision</div>
        <div class="card-value val-pass">{precision_pct:.1f}%</div>
      </div>
      <div class="card">
        <div class="card-title">Recall</div>
        <div class="card-value val-pass">{recall_pct:.1f}%</div>
      </div>
      <div class="card">
        <div class="card-title">F1 Score</div>
        <div class="card-value val-pass">{f1_score_pct:.1f}%</div>
      </div>
    </div>

    <div class="section">
      <h2>Confusion Matrix & Classification Breakdown</h2>
      <table>
        <thead>
          <tr>
            <th>Classification Metric</th>
            <th>Count</th>
            <th>Description</th>
          </tr>
        </thead>
        <tbody>
          <tr>
            <td><strong>True Positives (TP)</strong></td>
            <td><code class="val-pass">{tp}</code></td>
            <td>Non-compliant trade violations correctly denied or flagged</td>
          </tr>
          <tr>
            <td><strong>True Negatives (TN)</strong></td>
            <td><code class="val-pass">{tn}</code></td>
            <td>Fully compliant market trades correctly allowed</td>
          </tr>
          <tr>
            <td><strong>False Positives (FP)</strong></td>
            <td><code>{fp}</code></td>
            <td>Compliant market trades incorrectly denied/flagged</td>
          </tr>
          <tr>
            <td><strong>False Negatives (FN)</strong></td>
            <td><code>{fn}</code></td>
            <td>Non-compliant violations missed by rule evaluator</td>
          </tr>
          <tr>
            <td><strong>Total Evaluated</strong></td>
            <td><code>{total_count}</code></td>
            <td>Total synthetic SEBI trade transactions processed</td>
          </tr>
        </tbody>
      </table>
    </div>

    <div class="section">
      <h2>Evaluation Latency Distribution (ms)</h2>
      <table>
        <thead>
          <tr>
            <th>p50 (Median)</th>
            <th>p90</th>
            <th>p95</th>
            <th>p99</th>
            <th>Max Latency</th>
            <th>Throughput (req/s)</th>
          </tr>
        </thead>
        <tbody>
          <tr>
            <td><code>{lat_p50:.1f} ms</code></td>
            <td><code>{lat_p90:.1f} ms</code></td>
            <td><code>{lat_p95:.1f} ms</code></td>
            <td><code>{lat_p99:.1f} ms</code></td>
            <td><code>{lat_max:.1f} ms</code></td>
            <td><code>{throughput:.1f} req/s</code></td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</body>
</html>
"""


class ComplianceValidationRunner:
    def __init__(
        self,
        api_url: str = "http://localhost:8000",
        scenario: ScenarioType = ScenarioType.MIXED_MARKET_STREAM,
        count: int = 100,
        concurrency: int = 10,
        dry_run: bool = False,
        seed: int = 42,
    ) -> None:
        self.api_url = api_url.rstrip("/")
        self.scenario = scenario
        self.count = count
        self.concurrency = concurrency
        self.dry_run = dry_run
        self.generator = SyntheticTradeGenerator(seed=seed)

        self.latencies_ms: list[float] = []
        self.results: list[dict[str, Any]] = []

    async def _evaluate_single(
        self,
        client: httpx.AsyncClient,
        trade: SyntheticTrade,
    ) -> dict[str, Any]:
        t0 = time.perf_counter()
        expected = trade.expected_decision.lower()

        if self.dry_run:
            # Simulate evaluation response delay
            simulated_latency = random.uniform(0.002, 0.025)
            await asyncio.sleep(simulated_latency)
            elapsed_ms = (time.perf_counter() - t0) * 1000

            # In dry-run mode, 98% match expected ground truth to simulate evaluation engine
            actual = expected if random.random() > 0.02 else ("allow" if expected != "allow" else "deny")
            return {
                "transaction_id": trade.payload["transaction_id"],
                "expected": expected,
                "actual": actual,
                "latency_ms": elapsed_ms,
                "matched": expected == actual,
            }

        url = f"{self.api_url}/v1/execution/transactions/evaluate"
        try:
            resp = await client.post(url, json=trade.payload, timeout=5.0)
            elapsed_ms = (time.perf_counter() - t0) * 1000
            if resp.status_code == 200:
                data = resp.json()
                actual = str(data.get("decision", "unknown")).lower()
            else:
                actual = "error"
        except Exception:
            elapsed_ms = (time.perf_counter() - t0) * 1000
            actual = "error"

        return {
            "transaction_id": trade.payload["transaction_id"],
            "expected": expected,
            "actual": actual,
            "latency_ms": elapsed_ms,
            "matched": expected == actual,
        }

    async def run(self) -> dict[str, Any]:
        logger.info(
            "Starting Compliance Validation Runner: scenario=%s, count=%d, concurrency=%d, dry_run=%s",
            self.scenario.value, self.count, self.concurrency, self.dry_run,
        )

        trades = self.generator.generate_suite(count=self.count, scenario=self.scenario)
        queue: asyncio.Queue[SyntheticTrade] = asyncio.Queue()
        for t in trades:
            queue.put_nowait(t)

        start_time = time.perf_counter()

        limits = httpx.Limits(max_keepalive_connections=self.concurrency, max_connections=self.concurrency * 2)
        async with httpx.AsyncClient(limits=limits) as client:

            async def worker():
                while True:
                    try:
                        trade = queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                    res = await self._evaluate_single(client, trade)
                    self.results.append(res)
                    self.latencies_ms.append(res["latency_ms"])
                    queue.task_done()

            workers = [asyncio.create_task(worker()) for _ in range(self.concurrency)]
            await asyncio.gather(*workers)

        end_time = time.perf_counter()
        total_time = max(0.001, end_time - start_time)

        return self._compute_report(total_time)

    def _compute_report(self, total_time: float) -> dict[str, Any]:
        tp = tn = fp = fn = 0

        for r in self.results:
            exp, act = r["expected"], r["actual"]

            is_exp_violation = exp in ("deny", "flagged")
            is_act_violation = act in ("deny", "flagged")

            if is_exp_violation and is_act_violation:
                tp += 1
            elif not is_exp_violation and not is_act_violation:
                tn += 1
            elif not is_exp_violation and is_act_violation:
                fp += 1
            else:
                fn += 1

        total = len(self.results)
        accuracy = (tp + tn) / max(1, total)
        precision = tp / max(1, tp + fp)
        recall = tp / max(1, tp + fn)
        f1_score = (2 * precision * recall) / max(0.001, precision + recall)

        sorted_lat = sorted(self.latencies_ms) if self.latencies_ms else [0.0]

        def pct(p: float) -> float:
            idx = int(math.ceil((p / 100.0) * len(sorted_lat))) - 1
            return sorted_lat[max(0, min(idx, len(sorted_lat) - 1))]

        report = {
            "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(),
            "scenario": self.scenario.value,
            "total_evaluated": total,
            "total_duration_seconds": round(total_time, 2),
            "throughput_rps": round(total / total_time, 2),
            "metrics": {
                "accuracy_pct": round(accuracy * 100, 2),
                "precision_pct": round(precision * 100, 2),
                "recall_pct": round(recall * 100, 2),
                "f1_score_pct": round(f1_score * 100, 2),
            },
            "confusion_matrix": {
                "true_positives": tp,
                "true_negatives": tn,
                "false_positives": fp,
                "false_negatives": fn,
            },
            "latency_ms": {
                "p50": round(pct(50), 2),
                "p90": round(pct(90), 2),
                "p95": round(pct(95), 2),
                "p99": round(pct(99), 2),
                "max": round(sorted_lat[-1], 2),
            },
        }

        logger.info("================ COMPLIANCE VALIDATION RESULTS ================")
        logger.info("Accuracy: %.1f%% | Precision: %.1f%% | Recall: %.1f%% | F1: %.1f%%", accuracy * 100, precision * 100, recall * 100, f1_score * 100)
        logger.info("Confusion Matrix: TP=%d, TN=%d, FP=%d, FN=%d (Total=%d)", tp, tn, fp, fn, total)
        logger.info("Latency Percentiles (ms): p50=%.1f, p95=%.1f, p99=%.1f | Throughput: %.1f req/s", pct(50), pct(95), pct(99), total / total_time)
        logger.info("==============================================================")

        return report

    def export_reports(self, report: dict[str, Any], output_dir: str = "evals/reports") -> tuple[str, str]:
        os.makedirs(output_dir, exist_ok=True)
        ts_slug = dt.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        json_path = os.path.join(output_dir, f"compliance_validation_{ts_slug}.json")
        html_path = os.path.join(output_dir, f"compliance_validation_{ts_slug}.html")

        # 1. Export JSON Report
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2)
        logger.info("Saved JSON validation report to %s", json_path)

        # 2. Export HTML Dashboard Report
        html_content = HTML_REPORT_TEMPLATE.format(
            timestamp=report["timestamp"],
            scenario=report["scenario"],
            accuracy_pct=report["metrics"]["accuracy_pct"],
            precision_pct=report["metrics"]["precision_pct"],
            recall_pct=report["metrics"]["recall_pct"],
            f1_score_pct=report["metrics"]["f1_score_pct"],
            tp=report["confusion_matrix"]["true_positives"],
            tn=report["confusion_matrix"]["true_negatives"],
            fp=report["confusion_matrix"]["false_positives"],
            fn=report["confusion_matrix"]["false_negatives"],
            total_count=report["total_evaluated"],
            lat_p50=report["latency_ms"]["p50"],
            lat_p90=report["latency_ms"]["p90"],
            lat_p95=report["latency_ms"]["p95"],
            lat_p99=report["latency_ms"]["p99"],
            lat_max=report["latency_ms"]["max"],
            throughput=report["throughput_rps"],
        )

        with open(html_path, "w", encoding="utf-8") as f:
            f.write(html_content)
        logger.info("Saved HTML validation dashboard to %s", html_path)

        return json_path, html_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Compliance Performance & Accuracy Validation Runner")
    parser.add_argument("--api-url", default="http://localhost:8000", help="RegEngine AI API base URL")
    parser.add_argument("--scenario", choices=[s.value for s in ScenarioType], default=ScenarioType.MIXED_MARKET_STREAM.value)
    parser.add_argument("--count", type=int, default=100, help="Number of synthetic trades to evaluate")
    parser.add_argument("--concurrency", type=int, default=10, help="Worker concurrency limit")
    parser.add_argument("--dry-run", action="store_true", help="Simulate runner execution without live API calls")
    parser.add_argument("--output-dir", default="evals/reports", help="Directory path to save reports")

    args = parser.parse_args()

    runner = ComplianceValidationRunner(
        api_url=args.api_url,
        scenario=ScenarioType(args.scenario),
        count=args.count,
        concurrency=args.concurrency,
        dry_run=args.dry_run,
    )

    report = asyncio.run(runner.run())
    json_p, html_p = runner.export_reports(report, output_dir=args.output_dir)
    logger.info("Validation complete! Check HTML dashboard: %s", html_p)


if __name__ == "__main__":
    main()
