#!/usr/bin/env python3
"""Registers the load test's synthetic broker tenants (tenants_config.yaml)
into the target environment's `TenantClientStore` (Redis) so
POST /v1/auth/token succeeds for each of them during the run.

There is deliberately no public "sign up a tenant" API (see
app.security.tenant_store's module docstring) -- this script is the
operator-facing registration path that would otherwise be a manual admin
action, run once against whichever environment (`--redis-url`) is about to
be load tested. It is idempotent: re-running it just rotates the secrets
to match the config file's current values.

Usage:
  python loadtest/provision_tenants.py --redis-url redis://localhost:6379/0
  python loadtest/provision_tenants.py --redis-url redis://staging-redis:6379/0 --deregister-others
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import redis.asyncio as redis

from app.security.models import Role
from app.security.tenant_store import TenantClientStore


def _load_tenants(config_path: Path) -> list[dict]:
    data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    return data["tenants"]


async def provision(redis_url: str, key_prefix: str, tenants: list[dict]) -> None:
    client = redis.from_url(redis_url, decode_responses=True)
    store = TenantClientStore(redis_client=client, key_prefix=key_prefix)
    try:
        for tenant in tenants:
            await store.register(
                client_id=tenant["client_id"],
                client_secret=tenant["client_secret"],
                tenant_id=tenant["tenant_id"],
                roles=[Role.BROKER_API_CLIENT],
            )
            print(f"Registered {tenant['client_id']} -> tenant_id={tenant['tenant_id']} ({tenant.get('display_name', '')})")
    finally:
        await client.aclose()


def main() -> None:
    parser = argparse.ArgumentParser(description="Provision load-test broker tenants into TenantClientStore.")
    parser.add_argument("--redis-url", required=True, help="Redis URL of the TARGET environment being load tested -- never point this at production.")
    parser.add_argument("--key-prefix", default="regengine:tenant_clients", help="Must match settings.tenant_client_key_prefix on the target environment.")
    parser.add_argument("--config", type=Path, default=Path(__file__).resolve().parent / "tenants_config.yaml")
    args = parser.parse_args()

    tenants = _load_tenants(args.config)
    print(f"Provisioning {len(tenants)} load-test tenant(s) into {args.redis_url} ...")
    asyncio.run(provision(args.redis_url, args.key_prefix, tenants))
    print("Done.")


if __name__ == "__main__":
    main()
