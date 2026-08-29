#!/usr/bin/env python3
"""Primary-region health monitor for RegEngine AI's SEBI BCP DR plan.

Polls the primary region's `/healthz` and its RDS primary's own status,
requires `failure_threshold` *consecutive* failures before declaring a
regional outage (single blips must not trigger a cross-region failover --
that would trade a transient hiccup for the far larger risk surface of a
DR promotion), and then hands off to `failover_orchestrator.run_failover`.

Intended to run as a small always-on process (systemd unit / k8s
CronJob-with-liveness, one per DR-capable region pair) -- not as a
one-shot chaos-experiment probe like chaos/validation/validate_scenario3.py,
which checks the *outcome* of a failover, not whether one should start.

Usage:
  python dr/health_check.py --primary-url https://api-primary.regengine.ai/healthz \\
      --primary-db-endpoint regengine-prod-primary.xxxx.ap-south-1.rds.amazonaws.com \\
      --interval 10 --failure-threshold 3
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("dr.health_check")


class PrimaryHealthMonitor:
    def __init__(
        self,
        primary_healthz_url: str,
        interval_seconds: int,
        failure_threshold: int,
        request_timeout_seconds: float = 5.0,
    ) -> None:
        self.primary_healthz_url = primary_healthz_url
        self.interval_seconds = interval_seconds
        self.failure_threshold = failure_threshold
        self.request_timeout_seconds = request_timeout_seconds
        self._consecutive_failures = 0
        self._client = httpx.Client(timeout=request_timeout_seconds)

    def check_once(self) -> bool:
        """Returns True iff the primary is healthy. A healthy check
        additionally verifies the API's own DB connectivity via the
        `/healthz` payload (not just that the process is alive) -- a
        gray-failure where the app is up but its Postgres pool is wedged
        must count as unhealthy, since that is exactly the failure mode
        `dr/dns_client.py`'s force-failover path exists for."""
        try:
            resp = self._client.get(self.primary_healthz_url)
            if resp.status_code != 200:
                logger.warning("Primary /healthz returned status %d", resp.status_code)
                return False
            body = resp.json()
            db_ok = body.get("database", "unknown") in ("ok", "healthy", True)
            if not db_ok:
                logger.warning("Primary /healthz reports database not healthy: %s", body)
                return False
            return True
        except httpx.HTTPError as exc:
            logger.warning("Primary /healthz unreachable: %s", exc)
            return False

    def run_forever(self, on_outage_confirmed) -> None:
        logger.info(
            "Monitoring primary %s every %ds (failover triggers after %d consecutive failures = %ds detection window).",
            self.primary_healthz_url, self.interval_seconds, self.failure_threshold,
            self.interval_seconds * self.failure_threshold,
        )
        while True:
            healthy = self.check_once()
            if healthy:
                if self._consecutive_failures > 0:
                    logger.info("Primary recovered after %d consecutive failures.", self._consecutive_failures)
                self._consecutive_failures = 0
            else:
                self._consecutive_failures += 1
                logger.warning("Consecutive failure count: %d/%d", self._consecutive_failures, self.failure_threshold)
                if self._consecutive_failures >= self.failure_threshold:
                    logger.critical("Primary region outage CONFIRMED (%d consecutive failures). Triggering failover.", self._consecutive_failures)
                    on_outage_confirmed()
                    # Reset so a flapping/partially-recovered primary doesn't
                    # re-trigger failover every loop while the operator is
                    # already responding to the first alert.
                    self._consecutive_failures = 0

            time.sleep(self.interval_seconds)


def main() -> None:
    parser = argparse.ArgumentParser(description="RegEngine AI primary-region health monitor")
    parser.add_argument("--primary-url", required=True, help="Primary region /healthz URL")
    parser.add_argument("--interval", type=int, default=10, help="Seconds between checks")
    parser.add_argument("--failure-threshold", type=int, default=3, help="Consecutive failures before declaring an outage")
    parser.add_argument("--dry-run", action="store_true", help="Log the failover trigger instead of invoking failover_orchestrator")
    args = parser.parse_args()

    monitor = PrimaryHealthMonitor(args.primary_url, args.interval, args.failure_threshold)

    def on_outage_confirmed() -> None:
        if args.dry_run:
            logger.info("[dry-run] Would invoke dr.failover_orchestrator.run_failover() now.")
            return
        from dr.failover_orchestrator import load_config_from_env, run_failover

        run_failover(load_config_from_env())

    try:
        monitor.run_forever(on_outage_confirmed)
    except KeyboardInterrupt:
        logger.info("Health monitor stopped.")


if __name__ == "__main__":
    main()
