#!/usr/bin/env python3
"""Peak Trade Volume Load Generator for Scenario 2 (OPA Network Degradation).

Simulates high-throughput trade compliance evaluation requests against the
FastAPI `/v1/execution/transactions/evaluate` endpoint while network latency
(500ms) and packet loss (10%) are being injected into the OPA policy engine.

Usage:
  python chaos/load/opa_peak_trade_load.py --rate 50 --duration 60 --concurrency 10
  python chaos/load/opa_peak_trade_load.py --dry-run
"""

import argparse
import asyncio
import json
import logging
import math
import random
import sys
import time
from typing import Any
import httpx

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("opa_peak_trade_load")


SAMPLE_BROKERS = [f"BROKER_{i:03d}" for i in range(1, 21)]
ENTITY_TYPES = ["Stockbroker", "InvestmentAdviser", "DepositoryParticipant", "PortfolioManager"]


def generate_trade_payload(trans_idx: int) -> dict[str, Any]:
    broker_id = random.choice(SAMPLE_BROKERS)
    entity_type = random.choice(ENTITY_TYPES)
    margin_pct = random.choice([15, 20, 25, 50, 100])
    
    return {
        "transaction_id": f"chaos-trade-{trans_idx:08d}-{int(time.time())}",
        "broker_id": broker_id,
        "entity_type": entity_type,
        "facts": {
            "upfront_margin_pct": margin_pct,
            "peak_margin_collected": True,
            "client_category": random.choice(["RETAIL", "HNI", "INSTITUTIONAL"]),
            "order_value_inr": random.randint(10_000, 5_000_000),
            "collateral_haircut_applied": True,
        },
    }


class LoadGenerator:
    def __init__(
        self,
        target_url: str,
        rate: float,
        duration: int,
        concurrency: int,
        timeout: float,
        dry_run: bool = False,
    ) -> None:
        self.target_url = target_url
        self.rate = rate
        self.duration = duration
        self.concurrency = concurrency
        self.timeout = timeout
        self.dry_run = dry_run

        self.latencies_ms: list[float] = []
        self.status_counts: dict[str, int] = {}
        self.decisions: dict[str, int] = {}
        self.start_time: float = 0.0
        self.end_time: float = 0.0
        self.total_sent: int = 0
        self._lock = asyncio.Lock()

    async def _send_request(self, client: httpx.AsyncClient, trans_idx: int) -> None:
        payload = generate_trade_payload(trans_idx)
        t0 = time.perf_counter()
        status_key = "UNKNOWN"
        decision_key = "NONE"

        if self.dry_run:
            # Simulate latency under degraded conditions
            simulated_latency = random.uniform(0.005, 0.550) if random.random() > 0.1 else random.uniform(0.550, 1.200)
            await asyncio.sleep(simulated_latency)
            elapsed_ms = (time.perf_counter() - t0) * 1000
            if simulated_latency > 1.0:
                status_key = "TIMEOUT"
                decision_key = "ERROR"
            else:
                status_key = "200"
                decision_key = random.choice(["ALLOW", "ALLOW", "DENY", "FLAGGED"])
        else:
            try:
                resp = await client.post(self.target_url, json=payload, timeout=self.timeout)
                elapsed_ms = (time.perf_counter() - t0) * 1000
                status_key = str(resp.status_code)
                if resp.status_code == 200:
                    body = resp.json()
                    decision_key = body.get("decision", "UNKNOWN")
                else:
                    decision_key = f"HTTP_{resp.status_code}"
            except httpx.TimeoutException:
                elapsed_ms = (time.perf_counter() - t0) * 1000
                status_key = "TIMEOUT"
                decision_key = "TIMEOUT"
            except Exception as exc:
                elapsed_ms = (time.perf_counter() - t0) * 1000
                status_key = "ERROR"
                decision_key = type(exc).__name__

        async with self._lock:
            self.total_sent += 1
            self.latencies_ms.append(elapsed_ms)
            self.status_counts[status_key] = self.status_counts.get(status_key, 0) + 1
            self.decisions[decision_key] = self.decisions.get(decision_key, 0) + 1

    async def _worker(self, client: httpx.AsyncClient, queue: asyncio.Queue[int]) -> None:
        while True:
            try:
                trans_idx = queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            await self._send_request(client, trans_idx)
            queue.task_done()

    async def run(self) -> dict[str, Any]:
        logger.info(
            "Starting peak trade load test: target=%s, rate=%.1f req/s, duration=%ds, concurrency=%d, dry_run=%s",
            self.target_url,
            self.rate,
            self.duration,
            self.concurrency,
            self.dry_run,
        )

        total_requests = int(self.rate * self.duration)
        queue: asyncio.Queue[int] = asyncio.Queue()
        for i in range(total_requests):
            queue.put_nowait(i)

        self.start_time = time.perf_counter()
        limits = httpx.Limits(max_keepalive_connections=self.concurrency, max_connections=self.concurrency * 2)
        async with httpx.AsyncClient(limits=limits) as client:
            workers = [asyncio.create_task(self._worker(client, queue)) for _ in range(self.concurrency)]
            await asyncio.gather(*workers)

        self.end_time = time.perf_counter()
        return self.compute_report()

    def compute_report(self) -> dict[str, Any]:
        total_time = max(0.001, self.end_time - self.start_time)
        sorted_lat = sorted(self.latencies_ms) if self.latencies_ms else [0.0]

        def percentile(p: float) -> float:
            idx = int(math.ceil((p / 100.0) * len(sorted_lat))) - 1
            return sorted_lat[max(0, min(idx, len(sorted_lat) - 1))]

        report = {
            "duration_seconds": round(total_time, 2),
            "total_requests": self.total_sent,
            "actual_throughput_rps": round(self.total_sent / total_time, 2),
            "status_counts": self.status_counts,
            "decision_counts": self.decisions,
            "latency_ms": {
                "min": round(sorted_lat[0], 2),
                "p50": round(percentile(50), 2),
                "p90": round(percentile(90), 2),
                "p95": round(percentile(95), 2),
                "p99": round(percentile(99), 2),
                "max": round(sorted_lat[-1], 2),
            },
        }

        logger.info("================ LOAD TEST RESULTS ================")
        logger.info("Duration: %.2f s | Total Reqs: %d | Throughput: %.2f req/s", report["duration_seconds"], report["total_requests"], report["actual_throughput_rps"])
        logger.info("Status Counts: %s", json.dumps(self.status_counts))
        logger.info("Decisions: %s", json.dumps(self.decisions))
        logger.info("Latency Percentiles (ms): p50=%.1f, p95=%.1f, p99=%.1f, max=%.1f", report["latency_ms"]["p50"], report["latency_ms"]["p95"], report["latency_ms"]["p99"], report["latency_ms"]["max"])
        logger.info("==================================================")
        return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Peak Trade Volume Load Generator for Scenario 2")
    parser.add_argument("--target-url", default="http://localhost:8000/v1/execution/transactions/evaluate", help="FastAPI evaluation endpoint")
    parser.add_argument("--rate", type=float, default=50.0, help="Target requests per second")
    parser.add_argument("--duration", type=int, default=60, help="Total duration in seconds")
    parser.add_argument("--concurrency", type=int, default=10, help="Number of concurrent worker tasks")
    parser.add_argument("--timeout", type=float, default=3.0, help="HTTP request timeout in seconds")
    parser.add_argument("--dry-run", action="store_true", help="Simulate load without actual API calls")
    parser.add_argument("--output-json", default=None, help="File path to save JSON summary report")

    args = parser.parse_args()

    generator = LoadGenerator(
        target_url=args.target_url,
        rate=args.rate,
        duration=args.duration,
        concurrency=args.concurrency,
        timeout=args.timeout,
        dry_run=args.dry_run,
    )

    report = asyncio.run(generator.run())

    if args.output_json:
        with open(args.output_json, "w") as f:
            json.dump(report, f, indent=2)
        logger.info("Report saved to %s", args.output_json)


if __name__ == "__main__":
    main()
