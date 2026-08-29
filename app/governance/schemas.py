"""Pydantic I/O models for the governance API and reporting pipeline."""
from __future__ import annotations

import datetime as dt
from enum import Enum

from pydantic import BaseModel, EmailStr, Field

from app.analytics.models import AggregatedReport
from app.reporting.data_collector import HITLApprovalRecord


class KillSwitchScope(str, Enum):
    GLOBAL = "global"
    TENANT = "tenant"


class KillSwitchRedisState(BaseModel):
    """Internal Redis-serialized payload for one active kill-switch key
    -- not part of the public API schema (see KillSwitchStatusEntry for
    that), kept minimal since it is written/read on the request hot
    path."""

    reason: str
    activated_by: str
    activated_at: dt.datetime


class KillSwitchStatusEntry(BaseModel):
    scope: KillSwitchScope
    tenant_id: str | None = None
    active: bool
    reason: str | None = None
    activated_by: str | None = None
    activated_at: dt.datetime | None = None


class KillSwitchStatus(BaseModel):
    global_status: KillSwitchStatusEntry
    tenant_statuses: list[KillSwitchStatusEntry] = Field(default_factory=list)


class KillSwitchActionRequest(BaseModel):
    reason: str = Field(..., min_length=8, max_length=2000, description="Required, specific justification -- persisted permanently in KillSwitchEvent for the SEBI governance audit trail.")


class KillSwitchActionResult(BaseModel):
    event_id: str
    scope: KillSwitchScope
    tenant_id: str | None
    action: str  # "activated" | "deactivated" | "drill"
    actor: str
    occurred_at: dt.datetime
    is_drill: bool = False


# --------------------------------------------------------------------------
# Agent inventory (Requirement 2)
# --------------------------------------------------------------------------


class AgentInventoryCreate(BaseModel):
    agent_key: str = Field(..., min_length=2, max_length=64, pattern=r"^[a-z][a-z0-9_]*$")
    display_name: str = Field(..., min_length=2, max_length=200)
    model_provider: str = Field(..., min_length=1, max_length=64)
    model_weight_version: str = Field(..., min_length=1, max_length=128)
    business_domain: str = Field(..., min_length=2)
    is_critical_operation: bool = False
    owner_name: str = Field(..., min_length=2, max_length=200)
    owner_email: EmailStr
    owner_role: str = "Compliance_Officer"


class AgentInventoryUpdate(BaseModel):
    model_weight_version: str | None = None
    business_domain: str | None = None
    is_critical_operation: bool | None = None
    owner_name: str | None = None
    owner_email: EmailStr | None = None
    owner_role: str | None = None
    mark_reviewed: bool = Field(False, description="If true, sets last_reviewed_at to now() -- the SEBI-mandated periodic ownership re-attestation.")


class AgentInventoryOut(BaseModel):
    id: int
    agent_key: str
    display_name: str
    model_provider: str
    model_weight_version: str
    business_domain: str
    is_critical_operation: bool
    owner_name: str
    owner_email: str
    owner_role: str
    deployed_at: dt.datetime
    last_reviewed_at: dt.datetime | None
    is_active: bool
    retired_at: dt.datetime | None

    model_config = {"from_attributes": True}


# --------------------------------------------------------------------------
# Governance report (Requirement 3)
# --------------------------------------------------------------------------


class KillSwitchDrillResult(BaseModel):
    event_id: str
    scope: KillSwitchScope
    tenant_id: str | None
    reason: str
    actor: str
    occurred_at: dt.datetime
    passed: bool
    detail: str


class GovernanceReport(BaseModel):
    report_id: str
    generated_at: dt.datetime = Field(default_factory=lambda: dt.datetime.now(dt.timezone.utc))
    generated_by: str
    period_start: dt.date
    period_end: dt.date
    tenant_scope: str = "all"

    # Agent inventory snapshot
    active_agent_count: int
    critical_operation_agent_count: int
    agents_overdue_for_review: list[str] = Field(default_factory=list, description="agent_key values whose last_reviewed_at is more than 180 days old, or was never set.")

    # Execution count + decision error rate -- reuses
    # app.analytics.aggregator.ComplianceAggregator's real ledger
    # aggregation, not a parallel computation.
    total_agent_executions: int
    decision_error_rate_pct: float = Field(..., description="overall_fail_rate_pct from the underlying AggregatedReport.")
    hitl_flag_rate_pct: float

    # Human override frequency -- reuses
    # app.reporting.data_collector.collect_hitl_approvals.
    human_reviews_total: int
    human_overrides_approved: int
    human_override_rate_pct: float = Field(..., description="human_overrides_approved / human_reviews_total * 100.")

    # Kill-switch drill test results in this period.
    kill_switch_events_total: int
    kill_switch_drills_total: int
    kill_switch_drills_passed: int
    kill_switch_drill_results: list[KillSwitchDrillResult] = Field(default_factory=list)

    underlying_compliance_report: AggregatedReport
    underlying_hitl_approvals: list[HITLApprovalRecord]
