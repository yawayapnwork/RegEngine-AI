"""Requirement 2 -- Named Owner & Inventory Registry: CRUD over
`app.db.models.AgentInventory`, plus the seed roster reflecting this
platform's ACTUAL deployed AI/ML agents (every CrewAI agent
`app.agents.crew` / `app.healing.repair_agent` builds), not placeholder
data -- the model provider/weight version below are this codebase's own
real defaults (`app.agents.crew._build_llm`'s default model id).
"""
from __future__ import annotations

import datetime as dt

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AgentInventory
from app.governance.schemas import AgentInventoryCreate, AgentInventoryUpdate

# Matches app.agents.crew._build_llm's default `model` argument exactly --
# update this alongside that default if it ever changes, since this
# registry's whole purpose is to disclose the model ACTUALLY in
# production use, not a stale snapshot.
_DEFAULT_MODEL_PROVIDER = "anthropic"
_DEFAULT_MODEL_WEIGHT_VERSION = "claude-3-5-sonnet-20241022"

SEED_AGENTS: list[AgentInventoryCreate] = [
    AgentInventoryCreate(
        agent_key="extraction_agent",
        display_name="SEBI Compliance Clause Extraction Agent",
        model_provider=_DEFAULT_MODEL_PROVIDER,
        model_weight_version=_DEFAULT_MODEL_WEIGHT_VERSION,
        business_domain="Structures a raw SEBI circular clause into ExtractedComplianceRule JSON (app.agents.crew.build_extraction_agent) -- the first stage of the pipeline that ultimately produces enforced OPA policy.",
        is_critical_operation=True,
        owner_name="Priya Sharma",
        owner_email="priya.sharma@compliance.regengine.ai",
    ),
    AgentInventoryCreate(
        agent_key="logic_auditor_agent",
        display_name="Compliance Logic Auditor Agent",
        model_provider=_DEFAULT_MODEL_PROVIDER,
        model_weight_version=_DEFAULT_MODEL_WEIGHT_VERSION,
        business_domain="Adversarially audits the Extraction Agent's output against source text for hallucinated thresholds/entities (app.agents.crew.build_audit_agent) -- the gate a rule must pass before compilation.",
        is_critical_operation=True,
        owner_name="Priya Sharma",
        owner_email="priya.sharma@compliance.regengine.ai",
    ),
    AgentInventoryCreate(
        agent_key="quantitative_parsing_agent",
        display_name="Quantitative Formula Parsing Agent",
        model_provider=_DEFAULT_MODEL_PROVIDER,
        model_weight_version=_DEFAULT_MODEL_WEIGHT_VERSION,
        business_domain="Decomposes VaR/CRAR-style mathematical compliance formulas into constituent NumericalThreshold fields (app.agents.crew.build_quantitative_parsing_agent).",
        is_critical_operation=True,
        owner_name="Arjun Mehta",
        owner_email="arjun.mehta@compliance.regengine.ai",
    ),
    AgentInventoryCreate(
        agent_key="reference_resolution_agent",
        display_name="Cross-Reference Resolution Agent",
        model_provider=_DEFAULT_MODEL_PROVIDER,
        model_weight_version=_DEFAULT_MODEL_WEIGHT_VERSION,
        business_domain="Resolves inter-clause/inter-circular cross-references before extraction (app.agents.crew.build_reference_resolution_agent), so a referring clause's extraction reflects the referenced content's actual effect.",
        is_critical_operation=True,
        owner_name="Arjun Mehta",
        owner_email="arjun.mehta@compliance.regengine.ai",
    ),
    AgentInventoryCreate(
        agent_key="policy_repair_agent",
        display_name="Self-Healing Policy Repair Agent",
        model_provider=_DEFAULT_MODEL_PROVIDER,
        model_weight_version=_DEFAULT_MODEL_WEIGHT_VERSION,
        business_domain="LLM-based repair of a compiled policy that failed OPA publish/runtime testing (app.healing.repair_agent), escalated to only after a deterministic fast-path fix declines -- every repair still carries an advisory HITL flag before being trusted.",
        is_critical_operation=True,
        owner_name="Priya Sharma",
        owner_email="priya.sharma@compliance.regengine.ai",
    ),
]

# SEBI's expectation of periodic human re-attestation of AI/ML ownership
# -- an agent whose owner hasn't reviewed it within this window is
# surfaced by app.governance.reporting.build_governance_report as
# `agents_overdue_for_review`, not silently carried forward.
OWNERSHIP_REVIEW_WINDOW_DAYS = 180


async def seed_agent_inventory(db: AsyncSession) -> list[AgentInventory]:
    """Idempotent: registers each SEED_AGENTS entry only if its
    `agent_key` isn't already present, so this is safe to call on every
    app startup (mirrors app.db's other "ensure baseline row exists"
    seeding, e.g. the `sebi_baseline` sentinel Tenant)."""
    created: list[AgentInventory] = []
    for spec in SEED_AGENTS:
        existing = (await db.execute(select(AgentInventory).where(AgentInventory.agent_key == spec.agent_key))).scalar_one_or_none()
        if existing is not None:
            continue
        row = AgentInventory(**spec.model_dump())
        db.add(row)
        created.append(row)
    if created:
        await db.commit()
        for row in created:
            await db.refresh(row)
    return created


async def register_agent(db: AsyncSession, spec: AgentInventoryCreate) -> AgentInventory:
    row = AgentInventory(**spec.model_dump())
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


async def list_agents(db: AsyncSession, *, active_only: bool = True) -> list[AgentInventory]:
    query = select(AgentInventory).order_by(AgentInventory.agent_key.asc())
    if active_only:
        query = query.where(AgentInventory.is_active.is_(True))
    return list((await db.execute(query)).scalars().all())


async def get_agent(db: AsyncSession, agent_key: str) -> AgentInventory | None:
    return (await db.execute(select(AgentInventory).where(AgentInventory.agent_key == agent_key))).scalar_one_or_none()


async def update_agent(db: AsyncSession, agent_key: str, update: AgentInventoryUpdate) -> AgentInventory | None:
    row = await get_agent(db, agent_key)
    if row is None:
        return None
    for field, value in update.model_dump(exclude_unset=True, exclude={"mark_reviewed"}).items():
        setattr(row, field, value)
    if update.mark_reviewed:
        row.last_reviewed_at = dt.datetime.now(dt.timezone.utc)
    await db.commit()
    await db.refresh(row)
    return row


async def retire_agent(db: AsyncSession, agent_key: str) -> AgentInventory | None:
    row = await get_agent(db, agent_key)
    if row is None:
        return None
    row.is_active = False
    row.retired_at = dt.datetime.now(dt.timezone.utc)
    await db.commit()
    await db.refresh(row)
    return row


def agents_overdue_for_review(agents: list[AgentInventory], *, now: dt.datetime | None = None) -> list[str]:
    now = now or dt.datetime.now(dt.timezone.utc)
    cutoff = now - dt.timedelta(days=OWNERSHIP_REVIEW_WINDOW_DAYS)
    overdue = []
    for agent in agents:
        reference = agent.last_reviewed_at or agent.deployed_at
        if reference is None or reference.replace(tzinfo=reference.tzinfo or dt.timezone.utc) < cutoff:
            overdue.append(agent.agent_key)
    return overdue
