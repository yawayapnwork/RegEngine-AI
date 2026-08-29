"""LLM cost-analytics dashboard endpoints.

  GET /v1/llm-cost/summary
      Platform-wide token usage, spend, cache hit ratio, and model-tier
      distribution for a period, plus a per-tenant breakdown.

  GET /v1/llm-cost/tenants/{tenant_id}
      Same breakdown scoped to a single intermediary tenant -- what a
      tenant-facing billing/usage view would read from.

Access mirrors app.api.analytics_routes: Compliance_Officer and
System_Admin only for the platform-wide view (it's cross-tenant spend
data), but a tenant is allowed to see its own breakdown.
"""
from __future__ import annotations

import datetime as dt
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.db.session import get_engine
from app.llm_ops.aggregator import CostAggregator
from app.llm_ops.models import CostSummary, TenantCostBreakdown
from app.security.dependencies import get_current_principal, require_roles
from app.security.models import Principal, Role

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/llm-cost", tags=["LLM Cost Analytics"])

_ALLOWED_PLATFORM_WIDE = require_roles(Role.COMPLIANCE_OFFICER, Role.SYSTEM_ADMIN)

_DateFrom = Annotated[str, Query(description="Window start (YYYY-MM-DD, inclusive).", pattern=r"^\d{4}-\d{2}-\d{2}$")]
_DateTo = Annotated[str, Query(description="Window end (YYYY-MM-DD, inclusive).", pattern=r"^\d{4}-\d{2}-\d{2}$")]


def _parse_window(date_from: str, date_to: str) -> tuple[dt.datetime, dt.datetime]:
    try:
        start = dt.date.fromisoformat(date_from)
        end = dt.date.fromisoformat(date_to)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=f"Invalid date format: {e}")
    if start > end:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="date_from must be on or before date_to.")
    return (
        dt.datetime.combine(start, dt.time.min, tzinfo=dt.timezone.utc),
        dt.datetime.combine(end, dt.time.max, tzinfo=dt.timezone.utc),
    )


@router.get("/summary", response_model=CostSummary, dependencies=[Depends(_ALLOWED_PLATFORM_WIDE)])
async def get_cost_summary(date_from: _DateFrom, date_to: _DateTo) -> CostSummary:
    start, end = _parse_window(date_from, date_to)
    aggregator = CostAggregator(get_engine())
    return await aggregator.build_summary(start, end)


@router.get("/tenants/{tenant_id}", response_model=TenantCostBreakdown)
async def get_tenant_cost_breakdown(
    tenant_id: str,
    date_from: _DateFrom,
    date_to: _DateTo,
    principal: Principal = Depends(get_current_principal),
) -> TenantCostBreakdown:
    # A tenant's own Broker_API_Client principal may view its own spend;
    # anyone else needs a platform-wide role.
    is_self = principal.tenant_id == tenant_id
    is_platform_role = principal.has_role(Role.COMPLIANCE_OFFICER, Role.SYSTEM_ADMIN)
    if not (is_self or is_platform_role):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized to view this tenant's LLM cost breakdown.")

    start, end = _parse_window(date_from, date_to)
    aggregator = CostAggregator(get_engine())
    summary = await aggregator.build_summary(start, end, tenant_id=tenant_id)
    for row in summary.tenant_breakdown:
        if row.tenant_id == tenant_id:
            return row

    return TenantCostBreakdown(tenant_id=tenant_id)
