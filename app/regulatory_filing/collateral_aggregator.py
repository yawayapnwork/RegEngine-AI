"""Sources both filing types directly from the real, hash-chained
`compliance_audit_ledger` -- never a parallel bookkeeping table -- so a
filed record is always traceable back to the exact ledger row (by
`sequence_num`/`current_hash`) that produced it.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from app.ledger.models import compliance_audit_ledger
from app.regulatory_filing.schemas import (
    CollateralMetricRecord,
    CollateralReportFiling,
    ComplianceLogFiling,
    ComplianceLogRecord,
    FilingHeader,
    FilingTarget,
    FilingType,
)

# `details.violations` message text app.explainability's deterministic
# templates use for a margin-shortfall violation (see
# app.ledger.integration.build_ledger_events' `details.violations`,
# populated from app.execution.models.PolicyOutcome.violations, itself
# OPA's own `violation` rule message text -- app.compiler.rego_compiler's
# generated messages always name the metric first).
_MARGIN_VIOLATION_MARKER = "Margin"


def _content_sha256(records: list[dict]) -> str:
    canonical = json.dumps(records, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


async def collect_compliance_log_filing(
    engine: AsyncEngine,
    *,
    period_start: dt.date,
    period_end: dt.date,
    reporting_entity_code: str,
    target: FilingTarget = FilingTarget.SEBI,
) -> ComplianceLogFiling:
    """Every ledger row `evaluated_at` within [period_start, period_end]
    (inclusive), oldest first -- Requirement 1's "evaluated compliance
    logs" filing."""
    async with engine.connect() as conn:
        query = (
            select(compliance_audit_ledger)
            .where(compliance_audit_ledger.c.evaluated_at >= dt.datetime.combine(period_start, dt.time.min, tzinfo=dt.timezone.utc))
            .where(compliance_audit_ledger.c.evaluated_at <= dt.datetime.combine(period_end, dt.time.max, tzinfo=dt.timezone.utc))
            .order_by(compliance_audit_ledger.c.sequence_num.asc())
        )
        rows = (await conn.execute(query)).mappings().all()

    records = [
        ComplianceLogRecord(
            sequence_num=row["sequence_num"],
            broker_id=row["broker_id"],
            transaction_id=row["transaction_id"],
            evaluated_at=row["evaluated_at"],
            circular_id=row["circular_id"],
            clause_hash=row["clause_hash"],
            section_reference=row["section_reference"],
            rule_id=row["rule_id"],
            evaluation_result=row["evaluation_result"],
            ledger_current_hash=row["current_hash"],
        )
        for row in rows
    ]

    record_dicts = [r.model_dump(mode="json") for r in records]
    header = FilingHeader(
        filing_id=str(uuid.uuid4()),
        filing_type=FilingType.COMPLIANCE_LOG,
        target=target,
        reporting_entity_code=reporting_entity_code,
        period_start=period_start,
        period_end=period_end,
        record_count=len(records),
        content_sha256=_content_sha256(record_dicts),
    )
    return ComplianceLogFiling(header=header, records=records)


async def collect_daily_collateral_filing(
    engine: AsyncEngine,
    *,
    report_date: dt.date,
    reporting_entity_code: str,
    target: FilingTarget = FilingTarget.SEBI,
) -> CollateralReportFiling:
    """Aggregates every ledger row evaluated on `report_date`, grouped by
    `broker_id`, into one CollateralMetricRecord per broker --
    Requirement 1's "daily collateral reporting metrics" filing. Reads
    `details.facts.upfront_margin_pct` from each row's already-persisted
    JSONB `details` (the same input-facts snapshot
    app.ledger.integration.build_ledger_events writes for every
    evaluation, kept specifically so a later system -- app.backtest,
    and now this one -- can recompute something new from history without
    re-deriving facts from anywhere else); a row with no such fact
    (e.g. a zk-SNARK-verified evaluation, which deliberately carries no
    `facts` at all -- see app.zkp.verification_service's module
    docstring) simply doesn't contribute to the margin statistics, but
    still counts toward transactions_evaluated/passed/failed/flagged.
    """
    async with engine.connect() as conn:
        query = (
            select(compliance_audit_ledger)
            .where(compliance_audit_ledger.c.evaluated_at >= dt.datetime.combine(report_date, dt.time.min, tzinfo=dt.timezone.utc))
            .where(compliance_audit_ledger.c.evaluated_at <= dt.datetime.combine(report_date, dt.time.max, tzinfo=dt.timezone.utc))
            .order_by(compliance_audit_ledger.c.broker_id.asc())
        )
        rows = (await conn.execute(query)).mappings().all()

    by_broker: dict[str, dict] = {}
    for row in rows:
        bucket = by_broker.setdefault(
            row["broker_id"],
            {"evaluated": 0, "passed": 0, "failed": 0, "flagged": 0, "margins": [], "shortfalls": 0},
        )
        bucket["evaluated"] += 1
        result = row["evaluation_result"]
        if result == "PASS":
            bucket["passed"] += 1
        elif result == "FAIL":
            bucket["failed"] += 1
        elif result == "HITL_REVIEW":
            bucket["flagged"] += 1

        details = row["details"] or {}
        facts = details.get("facts") or {}
        margin = facts.get("upfront_margin_pct")
        if isinstance(margin, (int, float)):
            bucket["margins"].append(float(margin))

        if result == "FAIL":
            violations = details.get("violations") or []
            if any(_MARGIN_VIOLATION_MARKER in v for v in violations):
                bucket["shortfalls"] += 1

    records = [
        CollateralMetricRecord(
            report_date=report_date,
            broker_id=broker_id,
            transactions_evaluated=b["evaluated"],
            transactions_passed=b["passed"],
            transactions_failed=b["failed"],
            transactions_flagged_hitl=b["flagged"],
            avg_upfront_margin_pct=(sum(b["margins"]) / len(b["margins"])) if b["margins"] else None,
            min_upfront_margin_pct=min(b["margins"]) if b["margins"] else None,
            shortfall_count=b["shortfalls"],
        )
        for broker_id, b in sorted(by_broker.items())
    ]

    record_dicts = [r.model_dump(mode="json") for r in records]
    header = FilingHeader(
        filing_id=str(uuid.uuid4()),
        filing_type=FilingType.DAILY_COLLATERAL,
        target=target,
        reporting_entity_code=reporting_entity_code,
        period_start=report_date,
        period_end=report_date,
        record_count=len(records),
        content_sha256=_content_sha256(record_dicts),
    )
    return CollateralReportFiling(header=header, records=records)
