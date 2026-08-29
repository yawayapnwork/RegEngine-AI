"""Machine-readable JSON export -- the same data as the Excel workbook
and summary PDF, in a form a downstream system (a SEBI e-filing portal, a
broker's own compliance data warehouse) can ingest programmatically
without parsing a spreadsheet or PDF.
"""
from __future__ import annotations

import datetime as dt
import json

from pydantic import BaseModel, Field

from app.analytics.models import AggregatedReport, AuditTrailEntry
from app.reporting.data_collector import HITLApprovalRecord, RuleChangeRecord, SourceCircularRecord


class AuditBinderJSON(BaseModel):
    """Top-level shape of `audit_binder.json` inside the ZIP -- one
    document covering everything the PDF/Excel artifacts also present,
    for a consumer that wants a single parse rather than three formats."""

    report_id: str
    period_label: str
    period_start: dt.date
    period_end: dt.date
    generated_at: dt.datetime = Field(default_factory=lambda: dt.datetime.now(dt.timezone.utc))
    generated_by: str
    tenant_scope: str

    executive_summary: AggregatedReport
    rule_changes: list[RuleChangeRecord]
    hitl_approvals: list[HITLApprovalRecord]
    ledger_entries: list[AuditTrailEntry]
    source_circulars: list[SourceCircularRecord]


def build_json_export(binder: AuditBinderJSON) -> bytes:
    return binder.model_dump_json(indent=2).encode("utf-8")
