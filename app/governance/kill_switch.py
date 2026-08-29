"""Requirement 1 -- Agent Kill-Switch Engine.

Two-tier design, deliberately not Postgres-only or Redis-only:

  * `KillSwitchStore` (Redis) is the LIVE state every request/evaluation
    checks -- a sub-millisecond lookup, since this is on the hot path
    of `KillSwitchMiddleware` (every HTTP request) and
    `Evaluator.evaluate_transaction` (every live trade evaluation).
  * `kill_switch()` is the single entrypoint that ALSO writes a durable
    `app.db.models.KillSwitchEvent` row to Postgres -- the permanent
    record a SEBI governance audit (Requirement 3) reads from, which
    must survive a Redis restart/flush intact. Every activation,
    deactivation, and drill test goes through this one function; there
    is no code path that flips the Redis flag without also durably
    logging it.

`kill_switch()` is the literal name Requirement 1 asks for -- it is the
function an anomaly-detection hook, an admin CLI, or (see
app.api.governance_routes) an authenticated FastAPI route all call to
actually flip the switch.
"""
from __future__ import annotations

import datetime as dt
import logging
import uuid

import redis.asyncio as redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import KillSwitchEvent
from app.governance.schemas import (
    KillSwitchActionResult,
    KillSwitchRedisState,
    KillSwitchScope,
    KillSwitchStatus,
    KillSwitchStatusEntry,
)

logger = logging.getLogger(__name__)


class KillSwitchStore:
    """Storage shape:

        {prefix}:global                -> KillSwitchRedisState JSON, present iff the global switch is active
        {prefix}:tenant:{tenant_id}    -> KillSwitchRedisState JSON, present iff that tenant's switch is active
        {prefix}:active_tenants        -> set of tenant_ids with an active switch (for status listing without a KEYS scan)
    """

    def __init__(self, redis_client: redis.Redis, key_prefix: str) -> None:
        self._redis = redis_client
        self._prefix = key_prefix

    @property
    def _global_key(self) -> str:
        return f"{self._prefix}:global"

    def _tenant_key(self, tenant_id: str) -> str:
        return f"{self._prefix}:tenant:{tenant_id}"

    @property
    def _active_tenants_key(self) -> str:
        return f"{self._prefix}:active_tenants"

    async def is_global_active(self) -> bool:
        return await self._redis.exists(self._global_key) == 1

    async def is_tenant_active(self, tenant_id: str) -> bool:
        return await self._redis.exists(self._tenant_key(tenant_id)) == 1

    async def is_active_for(self, tenant_id: str | None) -> bool:
        """The single check both KillSwitchMiddleware and
        Evaluator.evaluate_transaction actually call: true if EITHER
        the global switch is on, or (when `tenant_id` is known) that
        tenant's own switch is on."""
        if await self.is_global_active():
            return True
        if tenant_id is not None:
            return await self.is_tenant_active(tenant_id)
        return False

    async def activate_global(self, reason: str, actor: str) -> None:
        state = KillSwitchRedisState(reason=reason, activated_by=actor, activated_at=dt.datetime.now(dt.timezone.utc))
        await self._redis.set(self._global_key, state.model_dump_json())

    async def deactivate_global(self) -> None:
        await self._redis.delete(self._global_key)

    async def activate_tenant(self, tenant_id: str, reason: str, actor: str) -> None:
        state = KillSwitchRedisState(reason=reason, activated_by=actor, activated_at=dt.datetime.now(dt.timezone.utc))
        async with self._redis.pipeline() as pipe:
            pipe.set(self._tenant_key(tenant_id), state.model_dump_json())
            pipe.sadd(self._active_tenants_key, tenant_id)
            await pipe.execute()

    async def deactivate_tenant(self, tenant_id: str) -> None:
        async with self._redis.pipeline() as pipe:
            pipe.delete(self._tenant_key(tenant_id))
            pipe.srem(self._active_tenants_key, tenant_id)
            await pipe.execute()

    async def get_status(self) -> KillSwitchStatus:
        global_raw = await self._redis.get(self._global_key)
        global_state = KillSwitchRedisState.model_validate_json(global_raw) if global_raw else None
        global_entry = KillSwitchStatusEntry(
            scope=KillSwitchScope.GLOBAL,
            active=global_state is not None,
            reason=global_state.reason if global_state else None,
            activated_by=global_state.activated_by if global_state else None,
            activated_at=global_state.activated_at if global_state else None,
        )

        tenant_ids = await self._redis.smembers(self._active_tenants_key)
        tenant_entries = []
        for tenant_id in tenant_ids:
            tenant_id = tenant_id if isinstance(tenant_id, str) else tenant_id.decode()
            raw = await self._redis.get(self._tenant_key(tenant_id))
            if raw is None:
                continue  # drifted index entry (e.g. a key expired) -- skip rather than fail the whole status call
            state = KillSwitchRedisState.model_validate_json(raw)
            tenant_entries.append(
                KillSwitchStatusEntry(
                    scope=KillSwitchScope.TENANT, tenant_id=tenant_id, active=True,
                    reason=state.reason, activated_by=state.activated_by, activated_at=state.activated_at,
                )
            )

        return KillSwitchStatus(global_status=global_entry, tenant_statuses=tenant_entries)


async def kill_switch(
    store: KillSwitchStore,
    db: AsyncSession,
    *,
    scope: KillSwitchScope,
    activate: bool,
    reason: str,
    actor: str,
    tenant_id: str | None = None,
    is_drill: bool = False,
) -> KillSwitchActionResult:
    """THE kill switch. Flips the live Redis state for `scope` (and
    `tenant_id`, if scope is TENANT) and durably records the action.
    `is_drill=True` (see app.governance.drills) still flips the real
    state -- a drill that doesn't actually exercise
    KillSwitchMiddleware/Evaluator's real behavior would not be a
    meaningful test of Requirement 1 -- but the caller is expected to
    immediately deactivate again once the drill's assertions run (see
    that module for the full activate -> verify -> deactivate cycle).
    """
    if scope == KillSwitchScope.TENANT and not tenant_id:
        raise ValueError("tenant_id is required when scope='tenant'.")
    if scope == KillSwitchScope.GLOBAL and tenant_id:
        raise ValueError("tenant_id must not be set when scope='global'.")

    if scope == KillSwitchScope.GLOBAL:
        if activate:
            await store.activate_global(reason, actor)
        else:
            await store.deactivate_global()
    else:
        if activate:
            await store.activate_tenant(tenant_id, reason, actor)  # type: ignore[arg-type]
        else:
            await store.deactivate_tenant(tenant_id)  # type: ignore[arg-type]

    event = KillSwitchEvent(
        event_id=str(uuid.uuid4()),
        scope=scope.value,
        tenant_id=tenant_id,
        action="activated" if activate else "deactivated",
        reason=reason,
        actor=actor,
        is_drill=is_drill,
    )
    db.add(event)
    await db.commit()
    await db.refresh(event)

    logger.warning(
        "KILL SWITCH %s: scope=%s tenant_id=%s actor=%s reason=%r is_drill=%s",
        event.action.upper(), scope.value, tenant_id, actor, reason, is_drill,
    )

    return KillSwitchActionResult(
        event_id=event.event_id, scope=scope, tenant_id=tenant_id,
        action=event.action, actor=actor, occurred_at=event.occurred_at, is_drill=is_drill,
    )
