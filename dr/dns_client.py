"""DNS failover client -- Route 53 and Cloudflare, one interface.

`dr/failover_orchestrator.py` calls this for the *explicit* flip. In the
common case, Route53's own health check (terraform/modules/dns_failover)
already routes new resolutions to the DR endpoint the moment the primary
fails its check -- no API call needed. This client exists for the
gray-failure case: the primary still answers `/healthz` (so the health
check stays green) but is otherwise unusable (e.g. its DB connection pool
is wedged, or a human has decided to fail over for a planned DR drill).
In both of those cases nothing will flip DNS unless something explicitly
tells it to.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass

logger = logging.getLogger("dr.dns_client")


@dataclass
class FailoverTarget:
    fqdn: str
    dr_ip_or_alias: str
    dr_alias_zone_id: str | None = None  # Route53 alias records only


class Route53FailoverClient:
    def __init__(self, hosted_zone_id: str) -> None:
        import boto3

        self._zone_id = hosted_zone_id
        self._client = boto3.client("route53")

    def force_failover_to_dr(self, target: FailoverTarget) -> str:
        """Explicitly repoints the PRIMARY record's alias target at the DR
        endpoint. This is deliberately destructive to the primary record
        (not just disabling its health check) so a subsequent `terraform
        apply` reconciles cleanly instead of fighting a manually-disabled
        health check left behind after the incident."""
        change = {
            "Changes": [
                {
                    "Action": "UPSERT",
                    "ResourceRecordSet": {
                        "Name": target.fqdn,
                        "Type": "A",
                        "SetIdentifier": "primary",
                        "Failover": "PRIMARY",
                        "AliasTarget": {
                            "HostedZoneId": target.dr_alias_zone_id,
                            "DNSName": target.dr_ip_or_alias,
                            "EvaluateTargetHealth": True,
                        },
                    },
                }
            ]
        }
        resp = self._client.change_resource_record_sets(HostedZoneId=self._zone_id, ChangeBatch=change)
        change_id = resp["ChangeInfo"]["Id"]
        logger.warning("Route53 PRIMARY record for %s force-repointed to DR endpoint %s (change_id=%s)", target.fqdn, target.dr_ip_or_alias, change_id)
        return change_id

    def wait_for_propagation(self, change_id: str, timeout_seconds: int = 120) -> bool:
        waiter = self._client.get_waiter("resource_record_sets_changed")
        try:
            waiter.wait(Id=change_id, WaiterConfig={"Delay": 5, "MaxAttempts": timeout_seconds // 5})
            return True
        except Exception:
            logger.exception("Route53 change %s did not reach INSYNC within %ds", change_id, timeout_seconds)
            return False


class CloudflareFailoverClient:
    def __init__(self, api_token: str | None = None, zone_id: str | None = None) -> None:
        self._token = api_token or os.environ["CLOUDFLARE_API_TOKEN"]
        self._zone_id = zone_id or os.environ["CLOUDFLARE_ZONE_ID"]

    def force_failover_to_dr(self, load_balancer_name: str, dr_pool_id: str) -> None:
        """Sets the load balancer's default pool to the DR pool directly,
        rather than waiting on the origin monitor's own failure threshold --
        same gray-failure rationale as the Route53 path above."""
        import httpx

        url = f"https://api.cloudflare.com/client/v4/zones/{self._zone_id}/load_balancers/{load_balancer_name}"
        resp = httpx.patch(
            url,
            headers={"Authorization": f"Bearer {self._token}", "Content-Type": "application/json"},
            json={"default_pools": [dr_pool_id]},
            timeout=15.0,
        )
        resp.raise_for_status()
        logger.warning("Cloudflare load balancer %s force-failed-over to DR pool %s", load_balancer_name, dr_pool_id)


def get_dns_client(provider: str, **kwargs) -> "Route53FailoverClient | CloudflareFailoverClient":
    if provider == "route53":
        return Route53FailoverClient(hosted_zone_id=kwargs["hosted_zone_id"])
    if provider == "cloudflare":
        return CloudflareFailoverClient(api_token=kwargs.get("api_token"), zone_id=kwargs.get("zone_id"))
    raise ValueError(f"Unknown DNS provider: {provider!r}")
