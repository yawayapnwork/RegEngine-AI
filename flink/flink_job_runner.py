#!/usr/bin/env python3
"""PyFlink Execution Runner & High-Frequency Stream Simulator.

Executes PyFlink streaming topologies or runs a high-throughput event stream simulator
for testing sub-5ms trade compliance evaluations and 24-hour sliding margin window aggregation.

Usage:
  python flink/flink_job_runner.py --dry-run --count 100
  python flink/flink_job_runner.py --kafka-bootstrap localhost:9092 --opa-url http://localhost:8181
"""

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
from typing import Any

# Add project root to sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from flink.stream_processor import OPAStreamEvaluator, build_pyflink_topology

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("flink_job_runner")


class SlidingWindowTracker:
    """Simulates 24-Hour Sliding Event-Time Window Aggregations per (broker_id, client_code)."""

    def __init__(self, window_hours: int = 24) -> None:
        self.window_seconds = window_hours * 3600
        self.windows: dict[str, list[dict[str, Any]]] = {}

    def add_trade_and_aggregate(self, broker_id: str, client_code: str, trade_facts: dict[str, Any]) -> dict[str, Any]:
        key = f"{broker_id}:{client_code}"
        now_ts = time.time()
        cutoff_ts = now_ts - self.window_seconds

        if key not in self.windows:
            self.windows[key] = []

        # Evict trades outside 24h window
        self.windows[key] = [t for t in self.windows[key] if t["ts"] >= cutoff_ts]

        order_val = float(trade_facts.get("order_value_inr", 100_000))
        upfront_pct = float(trade_facts.get("upfront_margin_pct", 20.0))
        margin_collected = (order_val * upfront_pct) / 100.0

        trade_entry = {"ts": now_ts, "order_val": order_val, "margin_val": margin_collected}
        self.windows[key].append(trade_entry)

        tot_order_val = sum(t["order_val"] for t in self.windows[key])
        tot_margin_val = sum(t["margin_val"] for t in self.windows[key])

        return {
            "cumulative_order_value_inr": round(tot_order_val, 2),
            "cumulative_margin_collected_inr": round(tot_margin_val, 2),
            "trade_count": len(self.windows[key]),
        }


class StreamSimulator:
    """Simulates high-frequency trade event streams for <5ms compliance evaluations."""

    def __init__(
        self,
        opa_url: str = "http://localhost:8181",
        count: int = 100,
        dry_run: bool = True,
    ) -> None:
        self.evaluator = OPAStreamEvaluator(opa_url=opa_url)
        self.window_tracker = SlidingWindowTracker()
        self.count = count
        self.dry_run = dry_run
        self.latencies_ms: list[float] = []

    async def run_simulation(self) -> dict[str, Any]:
        logger.info("Starting PyFlink High-Frequency Stream Simulation (count=%d)...", self.count)
        start_time = time.perf_counter()

        decisions_count: dict[str, int] = {}
        for i in range(1, self.count + 1):
            broker_id = f"INZ{random.randint(1001, 1005):07d}"
            client_code = f"CLI_{random.randint(101, 110)}"
            margin_pct = random.uniform(12.0, 35.0)
            order_val = random.randint(100_000, 5_000_000)

            trade_payload = {
                "transaction_id": f"STREAM-TX-{i:06d}",
                "broker_id": broker_id,
                "entity_type": "Stockbroker",
                "facts": {
                    "upfront_margin_pct": round(margin_pct, 2),
                    "peak_margin_collected": bool(random.random() > 0.1),
                    "client_funds_segregated": bool(random.random() > 0.05),
                    "order_value_inr": order_val,
                },
            }

            window_state = self.window_tracker.add_trade_and_aggregate(broker_id, client_code, trade_payload["facts"])
            result = await self.evaluator.evaluate_trade(trade_payload, window_state)

            dec = result["decision"]
            decisions_count[dec] = decisions_count.get(dec, 0) + 1
            self.latencies_ms.append(result["latency_ms"])

            if i % 25 == 0 or i == self.count:
                logger.info(
                    "Processed Stream Event %d/%d: id=%s dec=%s lat=%.2fms (cum_margin_pct=%.1f%%)",
                    i, self.count, result["transaction_id"], dec, result["latency_ms"],
                    (window_state["cumulative_margin_collected_inr"] / window_state["cumulative_order_value_inr"]) * 100.0,
                )

        end_time = time.perf_counter()
        total_time = max(0.001, end_time - start_time)
        sorted_lat = sorted(self.latencies_ms) if self.latencies_ms else [0.0]

        def pct(p: float) -> float:
            idx = int(math.ceil((p / 100.0) * len(sorted_lat))) - 1
            return sorted_lat[max(0, min(idx, len(sorted_lat) - 1))]

        report = {
            "total_events": self.count,
            "duration_seconds": round(total_time, 2),
            "throughput_rps": round(self.count / total_time, 2),
            "decisions": decisions_count,
            "latency_ms": {
                "p50": round(pct(50), 2),
                "p90": round(pct(90), 2),
                "p95": round(pct(95), 2),
                "p99": round(pct(99), 2),
                "max": round(sorted_lat[-1], 2),
            },
        }

        logger.info("================ PYFLINK STREAMING SUMMARY ================")
        logger.info("Total Events: %d | Duration: %.2fs | Throughput: %.1f req/s", self.count, total_time, report["throughput_rps"])
        logger.info("Decisions: %s", json.dumps(decisions_count))
        logger.info("Latency Percentiles (ms): p50=%.2fms, p95=%.2fms, p99=%.2fms", report["latency_ms"]["p50"], report["latency_ms"]["p95"], report["latency_ms"]["p99"])
        logger.info("==========================================================")

        return report


def main() -> None:
    parser = argparse.ArgumentParser(description="PyFlink Stream Processor Execution Runner")
    parser.add_argument("--kafka-bootstrap", default="localhost:9092", help="Kafka bootstrap servers")
    parser.add_argument("--opa-url", default="http://localhost:8181", help="OPA policy engine URL")
    parser.add_argument("--count", type=int, default=100, help="Number of stream trade events to evaluate")
    parser.add_argument("--dry-run", action="store_true", help="Simulate PyFlink stream execution locally")

    args = parser.parse_args()

    if args.dry_run:
        simulator = StreamSimulator(opa_url=args.opa_url, count=args.count, dry_run=True)
        asyncio.run(simulator.run_simulation())
    else:
        logger.info("Submitting PyFlink topology to Flink cluster...")
        topology = build_pyflink_topology(kafka_bootstrap=args.kafka_bootstrap, opa_url=args.opa_url)
        topology.execute("RegEngine_PyFlink_Trade_Compliance_Job")


if __name__ == "__main__":
    main()
