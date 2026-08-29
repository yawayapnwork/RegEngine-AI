"""Requirement 3's "kill-switch drill test results": a REAL functional
test of app.governance.kill_switch's mechanism -- activates, verifies
the live Redis state actually flipped, deactivates, verifies it flipped
back -- not merely a log entry asserting the switch "works". Both the
activation and deactivation `KillSwitchEvent` rows are marked
`is_drill=True` and annotated with the verified pass/fail verdict in
`details`, which app.governance.reporting.build_governance_report reads
directly for the periodic governance report.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import KillSwitchEvent
from app.governance.kill_switch import KillSwitchStore, kill_switch
from app.governance.schemas import KillSwitchDrillResult, KillSwitchScope


async def _is_scope_active(store: KillSwitchStore, scope: KillSwitchScope, tenant_id: str | None) -> bool:
    """Checks EXACTLY the scope under test -- deliberately not
    `is_active_for` (which ORs global and tenant together), since a
    tenant-scope drill must prove the TENANT flag specifically flipped,
    independent of whatever the global flag happens to be doing."""
    if scope == KillSwitchScope.GLOBAL:
        return await store.is_global_active()
    return await store.is_tenant_active(tenant_id)  # type: ignore[arg-type]


async def _annotate_event(db: AsyncSession, event_id: str, passed: bool, detail: str) -> None:
    row = (await db.execute(select(KillSwitchEvent).where(KillSwitchEvent.event_id == event_id))).scalar_one()
    row.details = {"passed": passed, "detail": detail}
    await db.commit()


async def run_kill_switch_drill(
    store: KillSwitchStore,
    db: AsyncSession,
    *,
    scope: KillSwitchScope,
    actor: str,
    tenant_id: str | None = None,
) -> KillSwitchDrillResult:
    reason = f"Scheduled kill-switch drill test ({scope.value}{f':{tenant_id}' if tenant_id else ''})."

    activation = await kill_switch(store, db, scope=scope, activate=True, reason=reason, actor=actor, tenant_id=tenant_id, is_drill=True)
    activation_verified = await _is_scope_active(store, scope, tenant_id)

    deactivation = await kill_switch(store, db, scope=scope, activate=False, reason=reason, actor=actor, tenant_id=tenant_id, is_drill=True)
    deactivation_verified = not await _is_scope_active(store, scope, tenant_id)

    passed = activation_verified and deactivation_verified
    detail = f"activation_took_effect={activation_verified}, deactivation_took_effect={deactivation_verified}"

    await _annotate_event(db, activation.event_id, passed, detail)
    await _annotate_event(db, deactivation.event_id, passed, detail)

    return KillSwitchDrillResult(
        event_id=activation.event_id,
        scope=scope,
        tenant_id=tenant_id,
        reason=reason,
        actor=actor,
        occurred_at=activation.occurred_at,
        passed=passed,
        detail=detail,
    )
