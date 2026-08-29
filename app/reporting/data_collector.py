"""Row-level (not aggregated) data collection for the audit binder.

Distinct from `app.analytics.aggregator.ComplianceAggregator`, which
computes STATISTICS (pass rates, time series, broker breakdowns) for the
executive summary PDF -- this module returns the raw enumerations a SEBI
auditor's binder actually needs to inspect: every rule version that
changed, every HITL approval/rejection decision, and every source
circular referenced, each as a full row, not a count.
"""
from __future__ import annotations

import datetime as dt

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Circular, Clause, CompiledRule, HITLReview


class RuleChangeRecord(BaseModel):
    rule_id: str
    rule_version: int
    clause_number: str | None
    circular_number: str
    is_active: bool
    hitl_status: str
    compiler_version: str | None
    created_at: dt.datetime
    opa_package_name: str | None


class HITLApprovalRecord(BaseModel):
    review_id: str
    clause_number: str | None
    circular_number: str
    reason_code: str
    severity: str
    status: str
    compliance_officer_id: str | None
    review_notes: str | None
    resolution_notes: str | None
    flagged_at: dt.datetime
    resolved_at: dt.datetime | None


class SourceCircularRecord(BaseModel):
    circular_number: str
    title: str | None
    issue_date: dt.date | None
    source_url: str | None
    raw_text_digest: str
    department: str | None


async def collect_rule_changes(db: AsyncSession, start: dt.datetime, end: dt.datetime, tenant_id: str | None = None) -> list[RuleChangeRecord]:
    """Every `CompiledRule` version CREATED within the window -- a
    version is immutable once created (app.db.models.CompiledRule's
    partial-unique-index comment), so "created in this window" and
    "changed in this window" are the same set of rows; there is no
    separate update-timestamp to reconcile against."""
    query = (
        select(CompiledRule, Clause, Circular)
        .join(Clause, Clause.id == CompiledRule.clause_id)
        .join(Circular, Circular.id == Clause.circular_id)
        .where(CompiledRule.created_at >= start, CompiledRule.created_at <= end)
        .order_by(CompiledRule.created_at.asc())
    )
    if tenant_id:
        query = query.where(CompiledRule.tenant_id == tenant_id)

    rows = (await db.execute(query)).all()
    return [
        RuleChangeRecord(
            rule_id=compiled_rule.rule_id,
            rule_version=compiled_rule.rule_version,
            clause_number=clause.clause_number,
            circular_number=circular.circular_number,
            is_active=compiled_rule.is_active,
            hitl_status=compiled_rule.hitl_status,
            compiler_version=compiled_rule.compiler_version,
            created_at=compiled_rule.created_at,
            opa_package_name=compiled_rule.opa_package_name,
        )
        for compiled_rule, clause, circular in rows
    ]


async def collect_hitl_approvals(db: AsyncSession, start: dt.datetime, end: dt.datetime, tenant_id: str | None = None) -> list[HITLApprovalRecord]:
    """Every `HITLReview` FLAGGED within the window, regardless of
    whether it was subsequently resolved inside or after the window --
    an auditor reviewing "what needed human sign-off this quarter" needs
    to see a review flagged on the last day of Q2 even if it wasn't
    resolved until Q3; `resolved_at`/`status` on the row shows its actual
    current disposition either way."""
    query = (
        select(HITLReview, Clause, Circular)
        .join(Clause, Clause.id == HITLReview.clause_id)
        .join(Circular, Circular.id == Clause.circular_id)
        .where(HITLReview.flagged_at >= start, HITLReview.flagged_at <= end)
        .order_by(HITLReview.flagged_at.asc())
    )
    if tenant_id:
        query = query.where(HITLReview.tenant_id == tenant_id)

    rows = (await db.execute(query)).all()
    return [
        HITLApprovalRecord(
            review_id=review.review_id,
            clause_number=clause.clause_number,
            circular_number=circular.circular_number,
            reason_code=review.reason_code,
            severity=review.severity,
            status=review.status,
            compliance_officer_id=review.compliance_officer_id,
            review_notes=review.review_notes,
            resolution_notes=review.resolution_notes,
            flagged_at=review.flagged_at,
            resolved_at=review.resolved_at,
        )
        for review, clause, circular in rows
    ]


async def collect_referenced_circulars(db: AsyncSession, rule_changes: list[RuleChangeRecord], hitl_approvals: list[HITLApprovalRecord]) -> list[SourceCircularRecord]:
    """Every distinct source circular touched by this period's rule
    changes or HITL activity -- these are what get embedded as raw PDFs
    (app.reporting.audit_binder) in the audit package, so the binder is
    self-contained rather than pointing an auditor at a URL that might
    not exist by the time they read it years later."""
    circular_numbers = {r.circular_number for r in rule_changes} | {r.circular_number for r in hitl_approvals}
    if not circular_numbers:
        return []

    rows = (await db.execute(select(Circular).where(Circular.circular_number.in_(circular_numbers)))).scalars().all()
    return [
        SourceCircularRecord(
            circular_number=c.circular_number,
            title=c.title,
            issue_date=c.issue_date,
            source_url=c.source_url,
            raw_text_digest=c.raw_text_digest,
            department=c.department,
        )
        for c in rows
    ]
