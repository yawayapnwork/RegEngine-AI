"""FastAPI reporting and analytics endpoints.

All endpoints require Compliance_Officer or System_Admin roles —
Broker_API_Client tokens are excluded from cross-tenant analytics access.

Endpoint summary
----------------

  GET  /v1/analytics/summary
       Build and return an AggregatedReport as JSON for the given period.
       Runs the full telemetry aggregation pipeline + anomaly detection.
       Optional: skip_chain_verify=true for faster preview responses.

  GET  /v1/analytics/anomalies
       Return only the AnomalyEvent list for a period (lighter-weight than
       the full summary; useful for dashboards polling for alerts).

  GET  /v1/analytics/audit-trail
       Return a paginated AuditTrailReport as JSON.  Includes chain proof.

  POST /v1/analytics/reports/executive-pdf
       Generate and stream the executive compliance summary PDF.

  POST /v1/analytics/reports/audit-trail-pdf
       Generate and stream the SEBI audit trail PDF.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.analytics.aggregator import ComplianceAggregator
from app.analytics.anomaly import detect_anomalies
from app.analytics.models import (
    AggregatedReport,
    AnomalyEvent,
    AuditTrailReport,
    Granularity,
    ReportPeriod,
)
from app.analytics.pdf_report import render_audit_trail, render_executive_summary
from app.config import Settings, get_settings
from app.db.tenant_session import get_admin_db_session
from app.ledger.db import get_ledger_engine
from app.security.dependencies import get_current_principal, require_roles
from app.security.models import Principal, Role

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/v1/analytics",
    tags=["Analytics & Reporting"],
)

# ---------------------------------------------------------------------------
# Shared types
# ---------------------------------------------------------------------------

# Roles permitted to access any analytics endpoint
_ALLOWED = require_roles(Role.COMPLIANCE_OFFICER, Role.SYSTEM_ADMIN)


def _current_quarter_start() -> dt.date:
    today = dt.date.today()
    q_start_month = ((today.month - 1) // 3) * 3 + 1
    return dt.date(today.year, q_start_month, 1)


def _validate_period(date_from: str, date_to: str) -> ReportPeriod:
    """Parse date strings and return a ReportPeriod, raising 422 on bad input."""
    try:
        start = dt.date.fromisoformat(date_from)
        end = dt.date.fromisoformat(date_to)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid date format (expected YYYY-MM-DD): {e}",
        )
    if start > end:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="date_from must be on or before date_to.",
        )
    return ReportPeriod(start_date=start, end_date=end)


# Shared query parameters
_DateFrom = Annotated[
    str,
    Query(
        description="Report window start date (YYYY-MM-DD, inclusive).",
        pattern=r"^\d{4}-\d{2}-\d{2}$",
    ),
]
_DateTo = Annotated[
    str,
    Query(
        description="Report window end date (YYYY-MM-DD, inclusive).",
        pattern=r"^\d{4}-\d{2}-\d{2}$",
    ),
]
_TenantScope = Annotated[
    str | None,
    Query(
        description=(
            "Restrict report to a single tenant_id / broker_id.  "
            "Omit for a cross-tenant executive summary."
        ),
    ),
]
_Granularity = Annotated[
    Granularity,
    Query(description="Bucket granularity: 'monthly' or 'quarterly'."),
]


# ---------------------------------------------------------------------------
# Dependency: build the aggregator from injected resources
# ---------------------------------------------------------------------------

async def _get_aggregator(
    db: AsyncSession = Depends(get_admin_db_session),
) -> ComplianceAggregator:
    """Construct a ComplianceAggregator wired to the ledger engine and admin DB."""
    return ComplianceAggregator(
        ledger_engine=get_ledger_engine(),
        db_session=db,
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get(
    "/summary",
    response_model=AggregatedReport,
    summary="Build executive compliance summary",
    description=(
        "Aggregates rule evaluation telemetry across all broker systems "
        "(or a single tenant) for the specified period and returns a structured "
        "AggregatedReport including headline KPIs, time-series buckets, broker "
        "breakdown, top violations, detected anomalies, and cryptographic chain proof."
    ),
)
async def get_summary(
    date_from: _DateFrom,
    date_to: _DateTo,
    granularity: _Granularity = Granularity.MONTHLY,
    tenant_id: _TenantScope = None,
    skip_chain_verify: bool = Query(
        False,
        description="Skip ledger hash-chain verification for faster responses.",
    ),
    principal: Principal = Depends(_ALLOWED),
    aggregator: ComplianceAggregator = Depends(_get_aggregator),
    settings: Settings = Depends(get_settings),
) -> AggregatedReport:
    period = _validate_period(date_from, date_to)
    period = ReportPeriod(
        start_date=period.start_date,
        end_date=period.end_date,
        granularity=granularity,
    )
    report_id = str(uuid.uuid4())

    logger.info(
        "Building analytics summary: report_id=%s period=%s tenant=%s principal=%s",
        report_id, period.label(), tenant_id or "all", principal.subject,
    )

    try:
        report = await aggregator.build_aggregated_report(
            period=period,
            report_id=report_id,
            generated_by=principal.subject,
            tenant_id=tenant_id,
            verify_chain_integrity=not skip_chain_verify,
        )
    except Exception as exc:
        logger.exception("Failed to build aggregated report: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Analytics pipeline failed: {exc}",
        )

    # Run anomaly detection in-process (fast; all data already in memory)
    report = detect_anomalies(report)
    return report


@router.get(
    "/anomalies",
    response_model=list[AnomalyEvent],
    summary="Get anomaly events for a period",
    description=(
        "Lighter-weight endpoint that runs the full aggregation and anomaly "
        "detection pipeline but returns only the AnomalyEvent list — suitable "
        "for compliance dashboards polling for alerts without fetching the full report."
    ),
)
async def get_anomalies(
    date_from: _DateFrom,
    date_to: _DateTo,
    granularity: _Granularity = Granularity.MONTHLY,
    tenant_id: _TenantScope = None,
    severity: str | None = Query(
        None,
        description="Filter by severity: 'high' or 'low'.  Omit for all.",
        pattern="^(high|low)$",
    ),
    principal: Principal = Depends(_ALLOWED),
    aggregator: ComplianceAggregator = Depends(_get_aggregator),
) -> list[AnomalyEvent]:
    period = _validate_period(date_from, date_to)
    period = ReportPeriod(
        start_date=period.start_date,
        end_date=period.end_date,
        granularity=granularity,
    )

    report = await aggregator.build_aggregated_report(
        period=period,
        report_id=str(uuid.uuid4()),
        generated_by=principal.subject,
        tenant_id=tenant_id,
        verify_chain_integrity=False,  # Not needed for anomaly-only queries
    )
    report = detect_anomalies(report)

    events = report.anomalies
    if severity:
        from app.analytics.models import AnomalySeverity
        target = AnomalySeverity(severity)
        events = [e for e in events if e.severity == target]

    return events


@router.get(
    "/audit-trail",
    response_model=AuditTrailReport,
    summary="Get paginated SEBI audit trail",
    description=(
        "Returns a paginated, cryptographically attested export of the compliance "
        "audit ledger for the specified period.  Every entry includes its "
        "payload_digest (SHA-256 of business fields) and current_hash (block hash) "
        "for independent verification against the production ledger."
    ),
)
async def get_audit_trail(
    date_from: _DateFrom,
    date_to: _DateTo,
    tenant_id: _TenantScope = None,
    page: int = Query(1, ge=1, description="Page number (1-indexed)."),
    page_size: int = Query(
        500, ge=1, le=2000,
        description="Entries per page.  Max 2000.",
    ),
    principal: Principal = Depends(_ALLOWED),
    aggregator: ComplianceAggregator = Depends(_get_aggregator),
) -> AuditTrailReport:
    period = _validate_period(date_from, date_to)

    report_id = str(uuid.uuid4())
    entries, chain_proof, total = await aggregator.build_audit_trail(
        period=period,
        report_id=report_id,
        generated_by=principal.subject,
        tenant_id=tenant_id,
        page=page,
        page_size=page_size,
    )

    import math
    total_pages = max(1, math.ceil(total / page_size))

    return AuditTrailReport(
        report_id=report_id,
        generated_by=principal.subject,
        period=period,
        tenant_scope=tenant_id or "all",
        total_entries=total,
        entries=entries,
        chain_proof=chain_proof,
        page=page,
        page_size=page_size,
        total_pages=total_pages,
    )


@router.post(
    "/reports/executive-pdf",
    summary="Generate executive compliance summary PDF",
    description=(
        "Runs the full analytics pipeline and renders a multi-section PDF report "
        "suitable for board-level review and internal compliance sign-off.  "
        "The PDF includes KPI scorecards, trend charts, broker breakdowns, "
        "violation tables, anomaly callouts, and a cryptographic chain-proof section."
    ),
    response_class=Response,
    responses={
        200: {
            "content": {"application/pdf": {}},
            "description": "PDF binary stream.",
        }
    },
)
async def generate_executive_pdf(
    date_from: _DateFrom,
    date_to: _DateTo,
    granularity: _Granularity = Granularity.MONTHLY,
    tenant_id: _TenantScope = None,
    skip_chain_verify: bool = Query(False),
    principal: Principal = Depends(_ALLOWED),
    aggregator: ComplianceAggregator = Depends(_get_aggregator),
) -> Response:
    period = _validate_period(date_from, date_to)
    period = ReportPeriod(
        start_date=period.start_date,
        end_date=period.end_date,
        granularity=granularity,
    )

    report = await aggregator.build_aggregated_report(
        period=period,
        report_id=str(uuid.uuid4()),
        generated_by=principal.subject,
        tenant_id=tenant_id,
        verify_chain_integrity=not skip_chain_verify,
    )
    report = detect_anomalies(report)

    logger.info(
        "Generating executive PDF: report_id=%s period=%s principal=%s",
        report.report_id, period.label(), principal.subject,
    )

    # render_executive_summary is synchronous + CPU-bound — run in thread pool
    pdf_bytes = await asyncio.to_thread(render_executive_summary, report)

    filename = (
        f"regengine_compliance_summary_{period.start_date.isoformat()}"
        f"_{period.end_date.isoformat()}.pdf"
    )
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Report-ID": report.report_id,
            "X-Chain-Valid": str(report.chain_proof.chain_valid)
            if report.chain_proof
            else "not-verified",
        },
    )


@router.post(
    "/reports/audit-trail-pdf",
    summary="Generate SEBI audit trail PDF",
    description=(
        "Produces a regulator-facing PDF containing the full paginated ledger "
        "extract with cryptographic proof fields (payload_digest, current_hash) "
        "and a chain-integrity attestation block.  Intended for submission to SEBI "
        "during regulatory audits or inspections."
    ),
    response_class=Response,
    responses={
        200: {
            "content": {"application/pdf": {}},
            "description": "PDF binary stream.",
        }
    },
)
async def generate_audit_trail_pdf(
    date_from: _DateFrom,
    date_to: _DateTo,
    tenant_id: _TenantScope = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(500, ge=1, le=2000),
    principal: Principal = Depends(_ALLOWED),
    aggregator: ComplianceAggregator = Depends(_get_aggregator),
) -> Response:
    period = _validate_period(date_from, date_to)

    report_id = str(uuid.uuid4())
    entries, chain_proof, total = await aggregator.build_audit_trail(
        period=period,
        report_id=report_id,
        generated_by=principal.subject,
        tenant_id=tenant_id,
        page=page,
        page_size=page_size,
    )

    import math
    total_pages = max(1, math.ceil(total / page_size))

    audit_report = AuditTrailReport(
        report_id=report_id,
        generated_by=principal.subject,
        period=period,
        tenant_scope=tenant_id or "all",
        total_entries=total,
        entries=entries,
        chain_proof=chain_proof,
        page=page,
        page_size=page_size,
        total_pages=total_pages,
    )

    logger.info(
        "Generating audit trail PDF: report_id=%s period=%s principal=%s",
        report_id, period.label(), principal.subject,
    )

    pdf_bytes = await asyncio.to_thread(render_audit_trail, audit_report)

    filename = (
        f"regengine_sebi_audit_trail_{period.start_date.isoformat()}"
        f"_{period.end_date.isoformat()}_p{page}.pdf"
    )
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Report-ID": report_id,
            "X-Chain-Valid": str(chain_proof.chain_valid),
            "X-Total-Entries": str(total),
        },
    )
