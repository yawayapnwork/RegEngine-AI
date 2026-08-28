#!/usr/bin/env python3
"""PostgreSQL Failover Load Generator for Scenario 3.

Evaluates compliance transactions and writes directly to `compliance_audit_ledger`
(via `LedgerService` or API calls) while the primary PostgreSQL database node is
killed. Records every transaction attempt into a journal for zero-data-loss validation.

Usage:
  python chaos/load/postgres_failover_load.py --duration 120 --rate 20
  python chaos/load/postgres_failover_load.py --dry-run
"""

import argparse
import asyncio
import datetime as dt
import json
import logging
import random
import sys
import time
from typing import Any
import httpx

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("postgres_failover_load")


class DatabaseFailoverLoadGenerator:
    def __init__(
        self,
        api_url: str,
        rate: float,
        duration: int,
        journal_file: str,
        dry_run: bool = False,
    ) -> None:
        self.api_url = api_url.rstrip("/")
        self.rate = rate
        self.duration = duration
        self.journal_file = journal_file
        self.dry_run = dry_run

        self.journal: list[dict[str, Any]] = []
        self.success_count: int = 0
        self.error_count: int = 0
        self.retry_count: int = 0
        self._lock = asyncio.Lock()

    async def _evaluate_and_record(self, client: httpx.AsyncClient, trans_idx: int) -> None:
        trans_id = f"chaos-failover-tx-{trans_idx:06d}-{int(time.time())}"
        broker_id = f"BROKER_{random.randint(1, 10):03d}"
        evaluated_at = dt.datetime.now(dt.timezone.utc).isoformat()

        payload = {
            "transaction_id": trans_id,
            "broker_id": broker_id,
            "entity_type": "Stockbroker",
            "facts": {
                "upfront_margin_pct": 20,
                "peak_margin_collected": True,
                "client_category": "RETAIL",
            },
        }

        entry_record = {
            "transaction_id": trans_id,
            "broker_id": broker_id,
            "submitted_at": evaluated_at,
            "status": "PENDING",
            "attempts": 0,
            "sequence_num": None,
        }

        if self.dry_run:
            # Simulate brief disruption during failover (e.g. 5 seconds of error/retry around t=30s)
            await asyncio.sleep(random.uniform(0.01, 0.05))
            entry_record["status"] = "COMMITTED"
            entry_record["sequence_num"] = trans_idx
            async with self._lock:
                self.success_count += 1
                self.journal.append(entry_record)
            return

        # Live execution with application-level retry on DB disconnection
        max_retries = 5
        for attempt in range(1, max_retries + 1):
            entry_record["attempts"] = attempt
            try:
                url = f"{self.api_url}/v1/execution/transactions/evaluate"
                resp = await client.post(url, json=payload, timeout=5.0)
                if resp.status_code == 200:
                    data = resp.json()
                    entry_record["status"] = "COMMITTED"
                    entry_record["hitl_case_id"] = data.get("hitl_case_id")
                    async with self._lock:
                        self.success_count += 1
                        self.journal.append(entry_record)
                    return
                elif resp.status_code in (502, 503, 504, 500):
                    # Transient DB disconnect error
                    async with self._lock:
                        self.retry_count += 1
                    await asyncio.sleep(0.5 * (2 ** (attempt - 1)))
                else:
                    break
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                async with self._lock:
                    self.retry_count += 1
                logger.debug("Transient connection error during failover attempt %d: %s", attempt, exc)
                await asyncio.sleep(0.5 * (2 ** (attempt - 1)))

        entry_record["status"] = "FAILED"
        async with self._lock:
            self.error_count += 1
            self.journal.append(entry_record)

    async def run(self) -> None:
        logger.info("Starting PostgreSQL Failover Load Generator: rate=%.1f/s, duration=%ds, dry_run=%s", self.rate, self.duration, self.dry_run)
        interval = 1.0 / max(0.1, self.rate)
        end_time = time.time() + self.duration
        trans_idx = 0

        limits = httpx.Limits(max_keepalive_connections=20, max_connections=50)
        async with httpx.AsyncClient(limits=limits) as client:
            tasks = []
            while time.time() < end_time:
                trans_idx += 1
                task = asyncio.create_task(self._evaluate_and_record(client, trans_idx))
                tasks.append(task)
                await asyncio.sleep(interval)

            await asyncio.gather(*tasks, return_exceptions=True)

        logger.info("Completed failover load generation. Success: %d, Retries: %d, Errors: %d", self.success_count, self.retry_count, self.error_count)

        with open(self.journal_file, "w") as f:
            json.dump({
                "summary": {
                    "total": len(self.journal),
                    "committed": self.success_count,
                    "retries": self.retry_count,
                    "errors": self.error_count,
                },
                "journal": self.journal,
            }, f, indent=2)
        logger.info("Transaction failover journal saved to %s", self.journal_file)


def main() -> None:
    parser = argparse.ArgumentParser(description="PostgreSQL Failover Load Generator for Scenario 3")
    parser.add_argument("--api-url", default="http://localhost:8000", help="RegEngine AI API base URL")
    parser.add_argument("--rate", type=float, default=20.0, help="Target evaluation transactions per second")
    parser.add_argument("--duration", type=int, default=120, help="Total test duration in seconds")
    parser.add_argument("--journal-file", default="chaos_failover_journal.json", help="Path to output transaction journal")
    parser.add_argument("--dry-run", action="store_true", help="Simulate workload without actual API/DB calls")

    args = parser.parse_args()

    generator = DatabaseFailoverLoadGenerator(
        api_url=args.api_url,
        rate=args.rate,
        duration=args.duration,
        journal_file=args.journal_file,
        dry_run=args.dry_run,
    )
    asyncio.run(generator.run())


if __name__ == "__main__":
    main()
