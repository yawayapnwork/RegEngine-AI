"""Tenant-aware OPA policy registry and bundle segregation.

This module extends the flat ``PolicyRegistry`` (app/execution/policy_registry.py)
with per-tenant namespace isolation so that:

  * Stockbroker A's compiled Rego policies are registered under
    ``regengine:policy_registry:stockbroker_a`` in Redis.
  * AMC B's policies live under ``regengine:policy_registry:amc_b``.
  * Neither tenant can see or inadvertently evaluate the other's rules.

OPA bundle layout
-----------------
Each tenant's policies are pushed to OPA under a tenant-namespaced path:

    tenants/<tenant_id>/<rule_id>

so the Rego package declaration becomes:

    package tenants.stockbroker_a.sebi_upfront_margin_v1

The ``TenantOPAEngine`` wrapper (below) re-scopes OPA ``PUT /v1/policies``
and ``POST /v1/data`` calls to the per-tenant path, guaranteeing that a bug
in one tenant's rule cannot affect another tenant's evaluation.

Risk overlay injection
-----------------------
Each tenant's ``risk_overlay`` JSONB (from the ``Tenant`` DB model) is pushed
as an OPA data document at the path ``data.tenants.<tenant_id>.overlay``:

    PUT /v1/data/tenants/<tenant_id>/overlay

Rego policies reference this via ``data.tenants[tenant_id].overlay.margin_pct``
etc., so threshold customisation is a data-plane change (no policy recompile)
when only thresholds change, and a full recompile only when logic changes.

Shared baseline
---------------
SEBI master-circular policies (owned by the ``sebi_baseline`` tenant) are
also registered in every tenant's registry lookup under the special key
``"*"`` so the tenant-scoped ``Evaluator`` still applies global SEBI rules.
``TenantPolicyRegistry.policies_for`` merges the tenant-specific list with
the baseline list and de-duplicates by ``rule_id``.
"""
from __future__ import annotations

import json
import logging
from typing import Any

import httpx
import redis.asyncio as redis

from app.compiler.models import CompiledRego
from app.execution.policy_cache import PolicyLookup

logger = logging.getLogger(__name__)

# The baseline tenant id whose policies are globally visible to all tenants.
_BASELINE_TENANT = "sebi_baseline"


def _registry_key(key_prefix: str, tenant_id: str) -> str:
    """Namespaced Redis key: ``regengine:policy_registry:<tenant_id>``."""
    return f"{key_prefix}:{tenant_id}"


class TenantPolicyRegistry:
    """Redis-backed, tenant-namespaced policy registry.

    A drop-in replacement for ``PolicyRegistry`` that satisfies the
    ``PolicyLookup`` protocol, so ``Evaluator`` and ``PolicyCache`` work
    without modification.  The difference is that ``policies_for`` only
    returns policies belonging to the configured ``tenant_id`` (plus
    baseline policies that apply to all tenants).

    Parameters
    ----------
    redis_client:
        Shared async Redis connection.
    registry_key_prefix:
        Base Redis key prefix (from ``settings.policy_registry_key``).
        The actual key is ``<prefix>:<tenant_id>``.
    tenant_id:
        The tenant whose policy partition this instance manages.
    """

    def __init__(
        self,
        redis_client: redis.Redis,
        registry_key_prefix: str,
        tenant_id: str,
    ) -> None:
        self._redis = redis_client
        self._prefix = registry_key_prefix
        self._tenant_id = tenant_id
        self._key = _registry_key(registry_key_prefix, tenant_id)
        self._baseline_key = _registry_key(registry_key_prefix, _BASELINE_TENANT)

    # ------------------------------------------------------------------
    # Write path (used by compiler pipeline when publishing a new rule)
    # ------------------------------------------------------------------

    async def register(self, compiled: CompiledRego, entity_types: list[str]) -> None:
        """Register a compiled policy under this tenant's partition.

        ``entity_types`` comes from the compiler (``ExtractedComplianceRule.
        target_entities``); an empty list means the policy has no entity
        guard and applies to every transaction type, stored under ``"*"``.
        """
        entry = json.dumps({"rule_id": compiled.rule_id, "package": compiled.package})
        for entity_type in entity_types or ["*"]:
            existing = await self._redis.hget(self._key, entity_type)
            entries: list[str] = json.loads(existing) if existing else []
            if entry not in entries:
                entries.append(entry)
            await self._redis.hset(self._key, entity_type, json.dumps(entries))

        logger.debug(
            "Registered policy rule_id=%s for tenant=%s entity_types=%s",
            compiled.rule_id,
            self._tenant_id,
            entity_types or ["*"],
        )

    async def unregister(self, rule_id: str) -> None:
        """Remove a rule from this tenant's partition across all entity types."""
        all_entries = await self._redis.hgetall(self._key)
        for entity_type, raw in all_entries.items():
            entries = json.loads(raw)
            filtered = [e for e in entries if json.loads(e)["rule_id"] != rule_id]
            if filtered:
                await self._redis.hset(self._key, entity_type, json.dumps(filtered))
            else:
                await self._redis.hdel(self._key, entity_type)

    # ------------------------------------------------------------------
    # Read path (hot evaluation path — called by PolicyCache.policies_for)
    # ------------------------------------------------------------------

    async def policies_for(self, entity_type: str) -> list[dict[str, str]]:
        """Return all applicable policies for ``entity_type``:

        - tenant-specific policies scoped to this entity type
        - tenant-specific wildcard (``"*"``) policies
        - baseline (``sebi_baseline``) policies for this entity type
        - baseline wildcard policies

        Result is de-duplicated by ``rule_id`` (tenant-specific wins over
        baseline if both declare the same rule_id, allowing a tenant to
        override a baseline rule with their own version).
        """
        results: list[dict[str, str]] = []
        seen_rule_ids: set[str] = set()

        # Tenant-specific entries first (higher priority)
        for key in (self._key,):
            for et in (entity_type, "*"):
                raw = await self._redis.hget(key, et)
                if raw:
                    for entry in json.loads(raw):
                        parsed = json.loads(entry)
                        if parsed["rule_id"] not in seen_rule_ids:
                            seen_rule_ids.add(parsed["rule_id"])
                            results.append(parsed)

        # Baseline entries (lower priority — already-seen rule_ids are skipped,
        # so a tenant can shadow a baseline rule with its own variant)
        if self._tenant_id != _BASELINE_TENANT:
            for et in (entity_type, "*"):
                raw = await self._redis.hget(self._baseline_key, et)
                if raw:
                    for entry in json.loads(raw):
                        parsed = json.loads(entry)
                        if parsed["rule_id"] not in seen_rule_ids:
                            seen_rule_ids.add(parsed["rule_id"])
                            results.append(parsed)

        return results

    # ------------------------------------------------------------------
    # Admin helpers
    # ------------------------------------------------------------------

    async def list_all(self) -> dict[str, list[dict[str, str]]]:
        """Return the full policy map for this tenant (entity_type -> entries).
        Used by the sandbox API to enumerate available rules."""
        raw_map = await self._redis.hgetall(self._key)
        return {
            et: [json.loads(e) for e in json.loads(raw)]
            for et, raw in raw_map.items()
        }

    async def clear(self) -> None:
        """Wipe all policy registrations for this tenant.
        Operator/admin use only; never exposed via a public API endpoint."""
        await self._redis.delete(self._key)
        logger.warning("Cleared entire policy registry for tenant=%s", self._tenant_id)


class TenantOPAEngine:
    """Tenant-namespaced wrapper around the OPA REST Policy API.

    Wraps the raw ``httpx`` calls (mirroring ``OPAEngine``) to:
      1. Prefix every policy ``rule_id`` with ``tenants/<tenant_id>/`` when
         pushing to or deleting from OPA, enforcing bundle isolation at the
         OPA module level.
      2. Push the tenant's ``risk_overlay`` data document to OPA at
         ``data.tenants.<tenant_id>.overlay`` so tenant-specific Rego
         threshold lookups resolve correctly.
      3. Scope policy evaluation queries to the tenant namespace.

    ``OPAEngine`` is kept as-is (it handles the baseline / global evaluation
    path); ``TenantOPAEngine`` is used exclusively by the compiler pipeline's
    tenant-aware publish step.
    """

    def __init__(
        self,
        base_url: str,
        timeout_seconds: float,
        tenant_id: str,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_seconds
        self._tenant_id = tenant_id

    def _scoped_rule_id(self, rule_id: str) -> str:
        """Ensure the stored OPA policy id is namespaced to the tenant."""
        prefix = f"tenants_{self._tenant_id}_"
        return rule_id if rule_id.startswith(prefix) else f"{prefix}{rule_id}"

    def _scoped_package(self, package: str) -> str:
        """Rewrite e.g. ``sebi.upfront_margin`` ->
        ``tenants.stockbroker_a.sebi.upfront_margin``."""
        ns = f"tenants.{self._tenant_id}"
        return package if package.startswith(ns) else f"{ns}.{package}"

    async def publish_policy(self, compiled: CompiledRego) -> None:
        """PUT the Rego module to OPA under the tenant-namespaced rule_id.

        The ``package`` declaration inside the Rego source is rewritten to
        include the tenant namespace so OPA's internal module registry
        keeps tenant policies separate even if two tenants compile the
        same rule_id.
        """
        scoped_id = self._scoped_rule_id(compiled.rule_id)
        scoped_package = self._scoped_package(compiled.package)

        # Rewrite the `package` line in the Rego source.
        rego_lines = compiled.rego_code.splitlines()
        rewritten = []
        for line in rego_lines:
            stripped = line.strip()
            if stripped.startswith("package "):
                rewritten.append(f"package {scoped_package}")
            else:
                rewritten.append(line)
        rego_source = "\n".join(rewritten)

        url = f"{self._base_url}/v1/policies/{scoped_id}"
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.put(
                url,
                content=rego_source.encode("utf-8"),
                headers={"Content-Type": "text/plain"},
            )
        if resp.status_code >= 300:
            raise RuntimeError(
                f"OPA rejected tenant policy '{scoped_id}' for tenant "
                f"'{self._tenant_id}': {resp.status_code} {resp.text}"
            )
        logger.info(
            "Published OPA policy rule_id=%s (scoped=%s) for tenant=%s",
            compiled.rule_id,
            scoped_id,
            self._tenant_id,
        )

    async def remove_policy(self, rule_id: str) -> None:
        scoped_id = self._scoped_rule_id(rule_id)
        url = f"{self._base_url}/v1/policies/{scoped_id}"
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.delete(url)
        if resp.status_code not in (200, 204, 404):
            raise RuntimeError(
                f"OPA rejected removal of '{scoped_id}' for tenant "
                f"'{self._tenant_id}': {resp.status_code} {resp.text}"
            )

    async def push_risk_overlay(self, overlay: dict[str, Any]) -> None:
        """Push the tenant's ``risk_overlay`` as an OPA data document.

        Stored at ``data.tenants.<tenant_id>.overlay`` so that Rego rules
        can reference thresholds via::

            import data.tenants.stockbroker_a.overlay
            default margin_threshold = overlay.upfront_margin_pct
        """
        path = f"tenants/{self._tenant_id}/overlay"
        url = f"{self._base_url}/v1/data/{path}"
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.put(url, json=overlay)
        if resp.status_code >= 300:
            raise RuntimeError(
                f"OPA rejected risk overlay for tenant '{self._tenant_id}': "
                f"{resp.status_code} {resp.text}"
            )
        logger.info("Pushed risk overlay to OPA for tenant=%s", self._tenant_id)

    async def evaluate(
        self, package: str, input_doc: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Query the tenant-scoped OPA decision rule.

        Mirrors ``OPAEngine.evaluate`` but routes to the tenant namespace.
        Returns ``None`` if OPA reports the result as undefined.
        """
        scoped_package = self._scoped_package(package)
        path = scoped_package.replace(".", "/")
        url = f"{self._base_url}/v1/data/{path}/decision"
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(url, json={"input": input_doc})
        except httpx.HTTPError as exc:
            raise RuntimeError(
                f"OPA request failed for tenant '{self._tenant_id}', "
                f"package '{scoped_package}': {exc}"
            ) from exc

        if resp.status_code != 200:
            raise RuntimeError(
                f"OPA returned {resp.status_code} for tenant "
                f"'{self._tenant_id}', package '{scoped_package}': {resp.text}"
            )
        body = resp.json()
        return body.get("result")


def get_tenant_policy_registry(
    redis_client: redis.Redis,
    registry_key_prefix: str,
    tenant_id: str,
) -> TenantPolicyRegistry:
    """Factory used by the execution dependencies module to build a
    ``TenantPolicyRegistry`` that satisfies ``PolicyLookup`` for injection
    into ``Evaluator`` when the calling context has a known tenant."""
    return TenantPolicyRegistry(
        redis_client=redis_client,
        registry_key_prefix=registry_key_prefix,
        tenant_id=tenant_id,
    )


def get_tenant_opa_engine(
    base_url: str,
    timeout_seconds: float,
    tenant_id: str,
) -> TenantOPAEngine:
    """Factory for ``TenantOPAEngine`` — mirrors the pattern of
    ``get_opa_engine`` in app/execution/dependencies.py."""
    return TenantOPAEngine(
        base_url=base_url,
        timeout_seconds=timeout_seconds,
        tenant_id=tenant_id,
    )
