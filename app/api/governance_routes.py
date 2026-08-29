"""Board-Level Governance & Kill-Switch Control Engine -- FastAPI
administrative control routes.

Kill-switch activation/deactivation/drill routes require BOTH
System_Admin-or-Compliance_Officer AND (by default,
`settings.governance_kill_switch_require_step_up_mfa`) fresh step-up MFA
(app.security.step_up.require_step_up_mfa) -- halting live trade
evaluation for an entire tenant (or the whole platform) is exactly the
kind of high-privilege, rarely-exercised action that must never be
reachable from a long-lived, possibly-stale bearer token alone. Both
roles are deliberately granted this authority (not System_Admin only):
a compliance officer spotting a live violation pattern must be able to
halt automated decisioning without waiting on an infrastructure
operator, matching this platform's board-level accountability framing.

Agent-inventory and reporting routes are read/maintain-oriented and use
the ordinary role check without step-up.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.analytics.models import Granularity, ReportPeriod
from app.config import Settings, get_settings
from app.db.tenant_session import get_admin_db_session
from app.execution.dependencies import get_kill_switch_store
from app.governance import inventory as inventory_service
from app.governance.drills import run_kill_switch_drill
from app.governance.kill_switch import KillSwitchStore, kill_switch
from app.governance.reporting import build_governance_report
from app.governance.schemas import (
    AgentInventoryCreate,
    AgentInventoryOut,
    AgentInventoryUpdate,
    GovernanceReport,
    KillSwitchActionRequest,
    KillSwitchActionResult,
    KillSwitchDrillResult,
    KillSwitchScope,
    KillSwitchStatus,
)
from app.ledger.db import get_ledger_engine
from app.security.dependencies import require_roles
from app.security.models import Principal, Role
from app.security.step_up import require_step_up_mfa

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/governance", tags=["AI Governance & Kill Switch"])

_READ = require_roles(Role.SYSTEM_ADMIN, Role.COMPLIANCE_OFFICER)


async def _require_kill_switch_authority(
    principal: Principal = Depends(require_roles(Role.SYSTEM_ADMIN, Role.COMPLIANCE_OFFICER)),
    settings: Settings = Depends(get_settings),
) -> Principal:
    if settings.governance_kill_switch_require_step_up_mfa:
        return await require_step_up_mfa(principal=principal, settings=settings)
    return principal


# --------------------------------------------------------------------------
# Kill switch
# --------------------------------------------------------------------------


@router.get("/kill-switch/status", response_model=KillSwitchStatus, dependencies=[Depends(_READ)])
async def get_kill_switch_status(store: KillSwitchStore = Depends(get_kill_switch_store)) -> KillSwitchStatus:
    return await store.get_status()


@router.post("/kill-switch/global/activate", response_model=KillSwitchActionResult)
async def activate_global_kill_switch(
    body: KillSwitchActionRequest,
    store: KillSwitchStore = Depends(get_kill_switch_store),
    db: AsyncSession = Depends(get_admin_db_session),
    principal: Principal = Depends(_require_kill_switch_authority),
) -> KillSwitchActionResult:
    return await kill_switch(store, db, scope=KillSwitchScope.GLOBAL, activate=True, reason=body.reason, actor=principal.subject)


@router.post("/kill-switch/global/deactivate", response_model=KillSwitchActionResult)
async def deactivate_global_kill_switch(
    body: KillSwitchActionRequest,
    store: KillSwitchStore = Depends(get_kill_switch_store),
    db: AsyncSession = Depends(get_admin_db_session),
    principal: Principal = Depends(_require_kill_switch_authority),
) -> KillSwitchActionResult:
    return await kill_switch(store, db, scope=KillSwitchScope.GLOBAL, activate=False, reason=body.reason, actor=principal.subject)


@router.post("/kill-switch/tenant/{tenant_id}/activate", response_model=KillSwitchActionResult)
async def activate_tenant_kill_switch(
    tenant_id: str,
    body: KillSwitchActionRequest,
    store: KillSwitchStore = Depends(get_kill_switch_store),
    db: AsyncSession = Depends(get_admin_db_session),
    principal: Principal = Depends(_require_kill_switch_authority),
) -> KillSwitchActionResult:
    return await kill_switch(store, db, scope=KillSwitchScope.TENANT, activate=True, reason=body.reason, actor=principal.subject, tenant_id=tenant_id)


@router.post("/kill-switch/tenant/{tenant_id}/deactivate", response_model=KillSwitchActionResult)
async def deactivate_tenant_kill_switch(
    tenant_id: str,
    body: KillSwitchActionRequest,
    store: KillSwitchStore = Depends(get_kill_switch_store),
    db: AsyncSession = Depends(get_admin_db_session),
    principal: Principal = Depends(_require_kill_switch_authority),
) -> KillSwitchActionResult:
    return await kill_switch(store, db, scope=KillSwitchScope.TENANT, activate=False, reason=body.reason, actor=principal.subject, tenant_id=tenant_id)


@router.post("/kill-switch/drill", response_model=KillSwitchDrillResult)
async def run_drill(
    scope: KillSwitchScope,
    tenant_id: str | None = None,
    store: KillSwitchStore = Depends(get_kill_switch_store),
    db: AsyncSession = Depends(get_admin_db_session),
    principal: Principal = Depends(_require_kill_switch_authority),
) -> KillSwitchDrillResult:
    """Requirement 3's drill-test source: a REAL activate -> verify ->
    deactivate -> verify cycle (app.governance.drills), not a
    hypothetical. Momentarily halts real traffic for `scope` while it
    runs -- see that module's docstring."""
    if scope == KillSwitchScope.TENANT and not tenant_id:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="tenant_id is required when scope='tenant'.")
    return await run_kill_switch_drill(store, db, scope=scope, actor=principal.subject, tenant_id=tenant_id)


# --------------------------------------------------------------------------
# Agent inventory
# --------------------------------------------------------------------------


@router.get("/agents", response_model=list[AgentInventoryOut], dependencies=[Depends(_READ)])
async def list_agents_route(active_only: bool = True, db: AsyncSession = Depends(get_admin_db_session)) -> list[AgentInventoryOut]:
    return await inventory_service.list_agents(db, active_only=active_only)


@router.post("/agents", response_model=AgentInventoryOut, status_code=status.HTTP_201_CREATED)
async def register_agent_route(
    body: AgentInventoryCreate,
    db: AsyncSession = Depends(get_admin_db_session),
    _principal: Principal = Depends(require_roles(Role.SYSTEM_ADMIN)),
) -> AgentInventoryOut:
    existing = await inventory_service.get_agent(db, body.agent_key)
    if existing is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"agent_key '{body.agent_key}' is already registered.")
    return await inventory_service.register_agent(db, body)


@router.get("/agents/{agent_key}", response_model=AgentInventoryOut, dependencies=[Depends(_READ)])
async def get_agent_route(agent_key: str, db: AsyncSession = Depends(get_admin_db_session)) -> AgentInventoryOut:
    agent = await inventory_service.get_agent(db, agent_key)
    if agent is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"No agent '{agent_key}'.")
    return agent


@router.patch("/agents/{agent_key}", response_model=AgentInventoryOut, dependencies=[Depends(_READ)])
async def update_agent_route(agent_key: str, body: AgentInventoryUpdate, db: AsyncSession = Depends(get_admin_db_session)) -> AgentInventoryOut:
    updated = await inventory_service.update_agent(db, agent_key, body)
    if updated is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"No agent '{agent_key}'.")
    return updated


@router.post("/agents/{agent_key}/retire", response_model=AgentInventoryOut)
async def retire_agent_route(
    agent_key: str,
    db: AsyncSession = Depends(get_admin_db_session),
    _principal: Principal = Depends(require_roles(Role.SYSTEM_ADMIN)),
) -> AgentInventoryOut:
    retired = await inventory_service.retire_agent(db, agent_key)
    if retired is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"No agent '{agent_key}'.")
    return retired


# --------------------------------------------------------------------------
# Governance report
# --------------------------------------------------------------------------


@router.get("/reports", response_model=GovernanceReport, dependencies=[Depends(_READ)])
async def get_governance_report(
    period_start: str,
    period_end: str,
    tenant_id: str | None = None,
    db: AsyncSession = Depends(get_admin_db_session),
    principal: Principal = Depends(_READ),
) -> GovernanceReport:
    import datetime as dt

    period = ReportPeriod(start_date=dt.date.fromisoformat(period_start), end_date=dt.date.fromisoformat(period_end), granularity=Granularity.MONTHLY)
    return await build_governance_report(get_ledger_engine(), db, period, generated_by=principal.subject, tenant_id=tenant_id)
