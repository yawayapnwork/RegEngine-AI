"""Pydantic models for the analytics and reporting layer.

These are the shared data contracts consumed by:
  - app.analytics.aggregator   (populates them from DB queries)
  - app.analytics.anomaly      (produces AnomalyEvent lists)
  - app.analytics.pdf_report   (renders them to PDF)
  - app.api.analytics_routes   (returns them as JSON responses)
"""
from __future__ import annotations

import datetime as dt
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------

class Granularity(str, Enum):
    MONTHLY = "monthly"
    QUARTERLY = "quarterly"


class AnomалySeverity(str, Enum):
    LOW = "low"       # 2σ–3σ
    HIGH = "high"     # >3σ


# Alias with ASCII-only name for import ergonomics
AnomalySeverity = AnomалySeverity


class AnomalyType(str, Enum):
    DENIAL_SPIKE = "denial_spike"         # sudden jump in FAIL rate
    HITL_SPIKE = "hitl_spike"             # sudden jump in HITL_REVIEW rate
    PASS_RATE_DROP = "pass_rate_drop"     # sudden drop in PASS rate
    VOLUME_SPIKE = "volume_spike"         # unusual transaction volume


# ---------------------------------------------------------------------------
# Report period
# ---------------------------------------------------------------------------

class ReportPeriod(BaseModel):
    """Specifies the evaluation window for an analytics query or report."""

    start_date: dt.date = Field(..., description="Inclusive start of the reporting window.")
    end_date: dt.date = Field(..., description="Inclusive end of the reporting window.")
    granularity: Granularity = Granularity.MONTHLY

    @property
    def start_datetime(self) -> dt.datetime:
        return dt.datetime.combine(self.start_date, dt.time.min, tzinfo=dt.timezone.utc)

    @property
    def end_datetime(self) -> dt.datetime:
        return dt.datetime.combine(self.end_date, dt.time.max, tzinfo=dt.timezone.utc)

    def label(self) -> str:
        return f"{self.start_date.isoformat()} to {self.end_date.isoformat()}"


# ---------------------------------------------------------------------------
# Per-period bucket (one row in the time-series)
# ---------------------------------------------------------------------------

class PeriodBucket(BaseModel):
    """Aggregated compliance metrics for a single monthly or quarterly bucket."""

    period_label: str = Field(..., description="e.g. '2026-08' (monthly) or '2026-Q3' (quarterly).")
    period_start: dt.date
    period_end: dt.date

    total_transactions: int = 0
    passed: int = 0
    failed: int = 0
    hitl_review: int = 0

    pass_rate_pct: float = 0.0          # passed / total * 100
    fail_rate_pct: float = 0.0
    hitl_rate_pct: float = 0.0

    unique_rules_triggered: int = 0
    unique_circulars_referenced: int = 0
    unique_brokers: int = 0


# ---------------------------------------------------------------------------
# Per-broker / per-tenant breakdown row
# ---------------------------------------------------------------------------

class BrokerStats(BaseModel):
    """Compliance metrics for a single broker (tenant) over the full report period."""

    broker_id: str
    display_name: str | None = None
    tenant_type: str | None = None

    total_transactions: int = 0
    passed: int = 0
    failed: int = 0
    hitl_review: int = 0

    pass_rate_pct: float = 0.0
    fail_rate_pct: float = 0.0
    hitl_rate_pct: float = 0.0

    top_violated_rules: list[str] = Field(
        default_factory=list,
        description="Up to 5 rule_ids with the most FAIL outcomes for this broker.",
    )
    top_violation_circulars: list[str] = Field(
        default_factory=list,
        description="Up to 5 circular_ids most frequently cited in violations.",
    )

    # HITL review disposition
    hitl_pending: int = 0
    hitl_resolved: int = 0
    hitl_rejected: int = 0


# ---------------------------------------------------------------------------
# Rule-level violation breakdown
# ---------------------------------------------------------------------------

class RuleViolationSummary(BaseModel):
    rule_id: str
    circular_id: str
    section_reference: str
    total_failures: int
    affected_brokers: int
    first_seen: dt.datetime | None = None
    last_seen: dt.datetime | None = None


# ---------------------------------------------------------------------------
# Anomaly event
# ---------------------------------------------------------------------------

class AnomalyEvent(BaseModel):
    """One detected statistical anomaly in the compliance telemetry."""

    anomaly_type: AnomalyType
    severity: AnomalySeverity
    broker_id: str | None = Field(None, description="None means a system-wide anomaly.")
    period_label: str = Field(..., description="The bucket (month/quarter) where the anomaly was detected.")
    metric_name: str = Field(..., description="Human-readable metric that spiked, e.g. 'fail_rate_pct'.")
    observed_value: float
    baseline_mean: float
    baseline_std: float
    z_score: float
    description: str
    detected_at: dt.datetime = Field(
        default_factory=lambda: dt.datetime.now(dt.timezone.utc)
    )


# ---------------------------------------------------------------------------
# Chain proof summary (for SEBI audit trail)
# ---------------------------------------------------------------------------

class ChainProofSummary(BaseModel):
    """Cryptographic integrity summary for the ledger window covered by the report."""

    verified_at: dt.datetime
    entries_checked: int
    chain_valid: bool
    break_count: int = 0

    # First and last sequence numbers in the verified window
    range_start_sequence: int | None = None
    range_end_sequence: int | None = None

    # SHA-256 of the last verified block hash — acts as the 'digest seal'
    # for this report window that an auditor can independently recompute.
    window_seal_hash: str | None = Field(
        None,
        description=(
            "current_hash of the last ledger row in the report window. "
            "An auditor can verify this against their own copy of the ledger "
            "to confirm no rows were added, removed, or altered after report generation."
        ),
    )
    note: str = (
        "Chain integrity is verified by recomputing SHA-256(previous_hash ‖ payload_digest "
        "‖ sequence_num ‖ evaluated_at) for every row and comparing to stored current_hash."
    )


# ---------------------------------------------------------------------------
# Top-level aggregated report (JSON + PDF source)
# ---------------------------------------------------------------------------

class AggregatedReport(BaseModel):
    """Complete compliance analytics report for a given period and tenant scope.

    This is the single object that:
      - analytics_routes returns as JSON from GET /v1/analytics/summary
      - pdf_report renders into a PDF from POST /v1/analytics/reports/pdf
    """

    report_id: str = Field(..., description="UUID identifying this report generation run.")
    generated_at: dt.datetime = Field(
        default_factory=lambda: dt.datetime.now(dt.timezone.utc)
    )
    generated_by: str = Field(..., description="Principal subject (compliance officer / admin) who triggered the report.")
    period: ReportPeriod
    tenant_scope: str = Field(
        "all",
        description="'all' for cross-tenant reports; a specific tenant_id for single-tenant views.",
    )

    # ---- Headline KPIs ----
    total_transactions: int = 0
    total_passed: int = 0
    total_failed: int = 0
    total_hitl_review: int = 0
    overall_pass_rate_pct: float = 0.0
    overall_fail_rate_pct: float = 0.0
    overall_hitl_rate_pct: float = 0.0

    unique_brokers_evaluated: int = 0
    unique_rules_evaluated: int = 0
    unique_circulars_referenced: int = 0

    # ---- Time series (monthly or quarterly buckets) ----
    time_series: list[PeriodBucket] = Field(default_factory=list)

    # ---- Broker breakdown ----
    broker_stats: list[BrokerStats] = Field(default_factory=list)

    # ---- Rule violation breakdown ----
    top_violations: list[RuleViolationSummary] = Field(
        default_factory=list,
        description="Top 10 most-violated rules across the period.",
    )

    # ---- Anomalies ----
    anomalies: list[AnomalyEvent] = Field(default_factory=list)
    anomaly_count_high: int = 0
    anomaly_count_low: int = 0

    # ---- Cryptographic proof ----
    chain_proof: ChainProofSummary | None = None


# ---------------------------------------------------------------------------
# SEBI Audit Trail report (a stricter, regulator-facing subset)
# ---------------------------------------------------------------------------

class AuditTrailEntry(BaseModel):
    """One row in the SEBI audit trail report — maps directly to a ledger row."""

    sequence_num: int
    broker_id: str
    transaction_id: str
    evaluated_at: dt.datetime
    circular_id: str
    section_reference: str
    rule_id: str
    evaluation_result: str       # PASS / FAIL / HITL_REVIEW
    hitl_review_id: str | None
    payload_digest: str          # SHA-256 of business fields
    current_hash: str            # block hash (cryptographic proof)


class AuditTrailReport(BaseModel):
    """SEBI-facing audit trail: a cryptographically bound, paginated ledger
    extract covering a specified window, with chain-integrity attestation."""

    report_id: str
    generated_at: dt.datetime = Field(
        default_factory=lambda: dt.datetime.now(dt.timezone.utc)
    )
    generated_by: str
    period: ReportPeriod
    tenant_scope: str

    total_entries: int = 0
    entries: list[AuditTrailEntry] = Field(default_factory=list)

    chain_proof: ChainProofSummary

    # Paginated response context
    page: int = 1
    page_size: int = 500
    total_pages: int = 1

    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="Arbitrary key-value pairs an operator can attach (e.g. auditor_name, submission_reference).",
    )
