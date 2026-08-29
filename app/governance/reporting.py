"""Requirement 3 -- SEBI Governance Reporting: composes the EXISTING
compliance analytics pipeline (app.analytics.aggregator.ComplianceAggregator)
and HITL reporting (app.reporting.data_collector.collect_hitl_approvals)
with the agent inventory (app.governance.inventory) and kill-switch
audit trail (app.db.models.KillSwitchEvent) into one periodic governance
report -- never a parallel computation of numbers those modules already
compute correctly from the same ledger/HITL data.
"""
from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.analytics.aggregator import ComplianceAggregator
from app.analytics.models import ReportPeriod
from app.db.models import KillSwitchEvent
from app.governance.inventory import agents_overdue_for_review, list_agents
from app.governance.schemas import GovernanceReport, KillSwitchDrillResult, KillSwitchScope
from app.reporting.data_collector import collect_hitl_approvals


async def build_governance_report(
    ledger_engine: AsyncEngine,
    db: AsyncSession,
    period: ReportPeriod,
    generated_by: str,
    tenant_id: str | None = None,
) -> GovernanceReport:
    report_id = str(uuid.uuid4())

    aggregator = ComplianceAggregator(ledger_engine, db)
    compliance_report = await aggregator.build_aggregated_report(
        period=period, report_id=report_id, generated_by=generated_by, tenant_id=tenant_id,
    )

    hitl_approvals = await collect_hitl_approvals(db, period.start_datetime, period.end_datetime, tenant_id=tenant_id)
    human_reviews_total = len(hitl_approvals)
    # "RESOLVED" is the exact status app.api.hitl_review_routes.approve_review
    # sets (see that module) -- a human compliance officer overriding the
    # automated pipeline's own block/flag and letting the rule through.
    human_overrides_approved = sum(1 for r in hitl_approvals if r.status == "RESOLVED")
    override_rate = (human_overrides_approved / human_reviews_total * 100) if human_reviews_total else 0.0

    agents = await list_agents(db, active_only=True)
    all_agents_including_retired = await list_agents(db, active_only=False)

    event_query = select(KillSwitchEvent).where(
        KillSwitchEvent.occurred_at >= period.start_datetime, KillSwitchEvent.occurred_at <= period.end_datetime,
    )
    if tenant_id:
        event_query = event_query.where(KillSwitchEvent.tenant_id == tenant_id)
    events = list((await db.execute(event_query.order_by(KillSwitchEvent.occurred_at.asc()))).scalars().all())

    # Each app.governance.drills.run_kill_switch_drill call durably
    # writes TWO rows (an "activated" event and a "deactivated" event --
    # see that module's docstring), both `is_drill=True`. A single drill
    # RUN is represented by its "activated" row alone here -- counting
    # both would double the reported drill count for every actual test
    # performed (caught by this module's own test suite: a single drill
    # run was initially reported as 2, not 1).
    drills = [e for e in events if e.is_drill and e.action == "activated"]
    drill_results = [
        KillSwitchDrillResult(
            event_id=e.event_id,
            scope=KillSwitchScope(e.scope),
            tenant_id=e.tenant_id,
            reason=e.reason,
            actor=e.actor,
            occurred_at=e.occurred_at,
            passed=e.details.get("passed", True) if isinstance(e.details, dict) else True,
            detail=e.details.get("detail", "") if isinstance(e.details, dict) else "",
        )
        for e in drills
    ]

    return GovernanceReport(
        report_id=report_id,
        generated_by=generated_by,
        period_start=period.start_date,
        period_end=period.end_date,
        tenant_scope=tenant_id or "all",
        active_agent_count=len(agents),
        critical_operation_agent_count=sum(1 for a in agents if a.is_critical_operation),
        agents_overdue_for_review=agents_overdue_for_review(all_agents_including_retired),
        total_agent_executions=compliance_report.total_transactions,
        decision_error_rate_pct=compliance_report.overall_fail_rate_pct,
        hitl_flag_rate_pct=compliance_report.overall_hitl_rate_pct,
        human_reviews_total=human_reviews_total,
        human_overrides_approved=human_overrides_approved,
        human_override_rate_pct=round(override_rate, 2),
        kill_switch_events_total=len(events),
        kill_switch_drills_total=len(drills),
        kill_switch_drills_passed=sum(1 for d in drill_results if d.passed),
        kill_switch_drill_results=drill_results,
        underlying_compliance_report=compliance_report,
        underlying_hitl_approvals=hitl_approvals,
    )
