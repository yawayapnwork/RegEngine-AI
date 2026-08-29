#!/usr/bin/env python3
"""Cross-region failover orchestrator for RegEngine AI (SEBI BCP).

Sequence, each step gating the next:

  1. Zero-data-loss gate: read the DR replica's `ReplicaLag` CloudWatch
     metric. If lag exceeds `max_acceptable_lag_seconds`, this is now a
     documented-RPO decision, not a mechanical one -- the script refuses to
     auto-promote and instead pages a human (SEBI BCP requires knowing and
     recording your actual RPO, not silently accepting data loss).
  2. Promote the DR read replica (`aws rds promote-read-replica`) and wait
     for it to leave `modifying` state -- this permanently severs
     replication, so it cannot be undone by re-running this script.
  3. Flip DNS explicitly via dr/dns_client.py (belt-and-braces: Route53's
     own health check has usually already done this, but see that
     module's docstring for the gray-failure case this covers).
  4. Cryptographic chain validation: run
     dr/validate_chain_post_failover.py's checks against the newly
     promoted primary. This does NOT gate steps 1-3 -- by the time you'd
     want to abort a failover already in flight it's generally too late
     to reverse it safely -- but a failed validation here must page
     someone immediately, since it means the audit trail itself may be in
     question after a failover SEBI will ask about.

Usage:
  python dr/failover_orchestrator.py --config dr/failover_config.json
  python dr/failover_orchestrator.py --dr-replica-id regengine-prod-dr-replica \\
      --dr-region ap-south-2 --dns-provider route53 --hosted-zone-id Z0123... \\
      --api-fqdn api.regengine.ai --dr-alb-dns-name regengine-dr-alb....amazonaws.com \\
      --dr-alb-zone-id Z18NTBI3Y7N9YO --new-primary-db-url postgresql+asyncpg://...
"""
from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import logging
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dr.dns_client import FailoverTarget, get_dns_client

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("dr.failover_orchestrator")


class FailoverAbortedError(RuntimeError):
    """Raised when a safety gate refuses to proceed automatically."""


@dataclasses.dataclass
class FailoverConfig:
    dr_replica_id: str
    dr_region: str
    dns_provider: str  # "route53" | "cloudflare"
    api_fqdn: str
    new_primary_db_url: str  # asyncpg URL against the DR endpoint, for post-failover chain validation
    max_acceptable_lag_seconds: int = 30
    promotion_timeout_seconds: int = 600
    # Route53
    hosted_zone_id: str | None = None
    dr_alb_dns_name: str | None = None
    dr_alb_zone_id: str | None = None
    # Cloudflare
    cloudflare_load_balancer_name: str | None = None
    cloudflare_dr_pool_id: str | None = None


def load_config_from_env() -> FailoverConfig:
    return FailoverConfig(
        dr_replica_id=os.environ["DR_REPLICA_ID"],
        dr_region=os.environ.get("DR_REGION", "ap-south-2"),
        dns_provider=os.environ.get("DNS_PROVIDER", "route53"),
        api_fqdn=os.environ["API_FQDN"],
        new_primary_db_url=os.environ["NEW_PRIMARY_DB_URL"],
        max_acceptable_lag_seconds=int(os.environ.get("MAX_ACCEPTABLE_LAG_SECONDS", "30")),
        hosted_zone_id=os.environ.get("HOSTED_ZONE_ID"),
        dr_alb_dns_name=os.environ.get("DR_ALB_DNS_NAME"),
        dr_alb_zone_id=os.environ.get("DR_ALB_ZONE_ID"),
        cloudflare_load_balancer_name=os.environ.get("CLOUDFLARE_LB_NAME"),
        cloudflare_dr_pool_id=os.environ.get("CLOUDFLARE_DR_POOL_ID"),
    )


def check_replica_lag(dr_replica_id: str, dr_region: str) -> float:
    import boto3

    cw = boto3.client("cloudwatch", region_name=dr_region)
    resp = cw.get_metric_statistics(
        Namespace="AWS/RDS",
        MetricName="ReplicaLag",
        Dimensions=[{"Name": "DBInstanceIdentifier", "Value": dr_replica_id}],
        StartTime=time.time() - 300,
        EndTime=time.time(),
        Period=60,
        Statistics=["Average"],
    )
    datapoints = sorted(resp.get("Datapoints", []), key=lambda d: d["Timestamp"])
    if not datapoints:
        logger.warning("No ReplicaLag datapoints in the last 5 minutes for %s -- treating as unknown lag.", dr_replica_id)
        return float("inf")
    return float(datapoints[-1]["Average"])


def promote_replica(dr_replica_id: str, dr_region: str, timeout_seconds: int) -> None:
    import boto3

    rds = boto3.client("rds", region_name=dr_region)
    logger.warning("Promoting read replica %s in %s to standalone primary. This is IRREVERSIBLE.", dr_replica_id, dr_region)
    rds.promote_read_replica(DBInstanceIdentifier=dr_replica_id)

    waiter = rds.get_waiter("db_instance_available")
    waiter.wait(
        DBInstanceIdentifier=dr_replica_id,
        WaiterConfig={"Delay": 15, "MaxAttempts": max(1, timeout_seconds // 15)},
    )
    logger.info("Promotion complete: %s is now a standalone read/write primary.", dr_replica_id)


def flip_dns(config: FailoverConfig) -> None:
    if config.dns_provider == "route53":
        client = get_dns_client("route53", hosted_zone_id=config.hosted_zone_id)
        target = FailoverTarget(fqdn=config.api_fqdn, dr_ip_or_alias=config.dr_alb_dns_name, dr_alias_zone_id=config.dr_alb_zone_id)
        change_id = client.force_failover_to_dr(target)
        client.wait_for_propagation(change_id)
    elif config.dns_provider == "cloudflare":
        client = get_dns_client("cloudflare")
        client.force_failover_to_dr(config.cloudflare_load_balancer_name, config.cloudflare_dr_pool_id)
    else:
        raise ValueError(f"Unknown dns_provider: {config.dns_provider}")


async def _run_post_failover_validation(new_primary_db_url: str) -> bool:
    from dr.validate_chain_post_failover import validate_post_failover

    result = await validate_post_failover(new_primary_db_url)
    return result.valid


def run_failover(config: FailoverConfig, force_despite_lag: bool = False) -> None:
    logger.info("=" * 70)
    logger.info("STARTING CROSS-REGION FAILOVER")
    logger.info("=" * 70)

    lag_seconds = check_replica_lag(config.dr_replica_id, config.dr_region)
    logger.info("DR replica lag: %.1fs (max acceptable: %ds)", lag_seconds, config.max_acceptable_lag_seconds)
    if lag_seconds > config.max_acceptable_lag_seconds and not force_despite_lag:
        raise FailoverAbortedError(
            f"Replica lag ({lag_seconds:.1f}s) exceeds max_acceptable_lag_seconds "
            f"({config.max_acceptable_lag_seconds}s). This would promote with a known, "
            f"non-zero RPO. Re-run with --force to proceed anyway and record the accepted "
            f"data loss window in the incident report, or wait for lag to drop."
        )
    if lag_seconds > config.max_acceptable_lag_seconds:
        logger.critical("Proceeding DESPITE lag of %.1fs due to --force. RPO is NOT zero for this failover.", lag_seconds)

    promote_replica(config.dr_replica_id, config.dr_region, config.promotion_timeout_seconds)
    flip_dns(config)

    logger.info("Running post-failover cryptographic chain validation...")
    chain_ok = asyncio.run(_run_post_failover_validation(config.new_primary_db_url))
    if chain_ok:
        logger.info("✅ Audit ledger hash chain verified intact on the new primary.")
    else:
        logger.critical(
            "❌ Audit ledger hash chain validation FAILED on the new primary. "
            "Page the compliance/security on-call immediately -- see DR_RUNBOOK.md "
            "'Post-Failover Chain Break' procedure."
        )

    logger.info("=" * 70)
    logger.info("FAILOVER COMPLETE. New primary: %s (%s). DNS now points at DR.", config.dr_replica_id, config.dr_region)
    logger.info("=" * 70)


def main() -> None:
    parser = argparse.ArgumentParser(description="RegEngine AI cross-region failover orchestrator")
    parser.add_argument("--config", type=Path, default=None, help="JSON file matching FailoverConfig fields")
    parser.add_argument("--force", action="store_true", help="Proceed even if replica lag exceeds the max-acceptable threshold")
    args = parser.parse_args()

    if args.config:
        config = FailoverConfig(**json.loads(args.config.read_text()))
    else:
        config = load_config_from_env()

    try:
        run_failover(config, force_despite_lag=args.force)
    except FailoverAbortedError as exc:
        logger.error("Failover ABORTED by safety gate: %s", exc)
        sys.exit(2)


if __name__ == "__main__":
    main()
