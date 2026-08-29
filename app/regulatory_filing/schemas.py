"""Pydantic models for the two filing types Requirement 1 names:
evaluated compliance logs and daily collateral reporting metrics.

The exact field layout below is this project's own reasonable, clearly-
scoped modeling of what a SEBI/MII e-filing envelope looks like (a
header identifying the reporting entity/period/filing type, a record
list, and an integrity digest over the records) -- the real SEBI XSD/
JSON Schema for a specific filing type is published per-circular and
isn't a fixed, fetchable artifact this codebase can pin to; deploying
this against a live SEBI/MII endpoint means substituting the schema
files in app/regulatory_filing/schemas/ for whichever XSD/JSON Schema
that specific filing mandate publishes, without changing the
serializer/signing/submission code around it.
"""
from __future__ import annotations

import datetime as dt
from enum import Enum

from pydantic import BaseModel, Field


class FilingType(str, Enum):
    COMPLIANCE_LOG = "compliance_log"
    DAILY_COLLATERAL = "daily_collateral"


class FilingTarget(str, Enum):
    """Requirement 3's "SEBI and market infrastructure institutions" --
    which regulator/MII this specific filing is addressed to. Distinct
    from `submission.SubmissionChannel` (SFTP vs. portal API), since the
    same target can support either channel depending on deployment."""

    SEBI = "SEBI"
    NSE = "NSE"
    BSE = "BSE"
    NSDL = "NSDL"
    CDSL = "CDSL"


class FilingHeader(BaseModel):
    """Common envelope header for every filing type -- the part a
    regulator's intake system checks before it even looks at the record
    payload (who is filing, for what period, of what type, how many
    records, and a content digest it can use to detect truncation/
    corruption independent of the digital signature)."""

    filing_id: str
    filing_type: FilingType
    target: FilingTarget
    reporting_entity_code: str = Field(..., description="SEBI broker/intermediary registration number.")
    period_start: dt.date
    period_end: dt.date
    generated_at: dt.datetime = Field(default_factory=lambda: dt.datetime.now(dt.timezone.utc))
    record_count: int
    content_sha256: str = Field(..., description="SHA-256 over the canonical (sorted-keys, compact) JSON encoding of the record list -- independent of and computed BEFORE the PKI signature; see app.regulatory_filing.signing.")


class ComplianceLogRecord(BaseModel):
    """One row of Requirement 1's "evaluated compliance logs" --
    sourced 1:1 from a real `app.ledger.models.LedgerEntry` (never
    re-derived), so a regulator cross-checking a filed record against
    this system's own hash-chained audit ledger by `sequence_num` gets
    an exact match."""

    sequence_num: int
    broker_id: str
    transaction_id: str
    evaluated_at: dt.datetime
    circular_id: str
    clause_hash: str
    section_reference: str
    rule_id: str
    evaluation_result: str
    ledger_current_hash: str = Field(..., description="app.ledger.models.LedgerEntry.current_hash -- lets a regulator verify this record against the ledger's own hash chain independently of this filing's PKI signature.")


class ComplianceLogFiling(BaseModel):
    header: FilingHeader
    records: list[ComplianceLogRecord]


class CollateralMetricRecord(BaseModel):
    """One row of Requirement 1's "daily collateral reporting metrics":
    one broker's aggregated collateral-adequacy picture for one calendar
    day, derived from the same ledger entries' `details.facts` snapshot
    every compliance evaluation already carries (see
    app.regulatory_filing.collateral_aggregator) -- never a parallel
    collateral bookkeeping system."""

    report_date: dt.date
    broker_id: str
    transactions_evaluated: int
    transactions_passed: int
    transactions_failed: int
    transactions_flagged_hitl: int
    avg_upfront_margin_pct: float | None = Field(None, description="Mean of `facts.upfront_margin_pct` across every transaction this broker had evaluated on report_date that carried that fact; None if no transaction did.")
    min_upfront_margin_pct: float | None = None
    shortfall_count: int = Field(..., description="Number of transactions_failed specifically attributable to an upfront-margin-shortfall violation (a FAIL outcome whose `details.violations` mentions margin) -- SEBI's collateral-shortfall reporting mandate's core figure.")


class CollateralReportFiling(BaseModel):
    header: FilingHeader
    records: list[CollateralMetricRecord]
