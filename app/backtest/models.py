"""Data contracts for the backtesting service.

`old_decision`/`old_violations` come from the LEDGER (what actually
happened when the transaction was truly evaluated in production) --
never re-derived by re-running the old policy, since the ledger's
hash-chained record already IS the authoritative historical outcome
(app.ledger.hash_chain) and re-deriving it would just be a slower,
riskier way to arrive at the same number while introducing a second
possible source of disagreement.

`new_decision`/`new_violations` come from evaluating the SAME historical
`facts` snapshot against the candidate policy via
app.backtest.candidate_evaluator -- entirely offline, never touching a
production OPA server or the live policy registry (Requirement: "before
they are deployed to live production systems").
"""
from __future__ import annotations

import datetime as dt
import uuid
from enum import Enum

from pydantic import BaseModel, Field

from app.execution.models import Decision


class BacktestStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class DeltaChangeType(str, Enum):
    UNCHANGED_PASS = "unchanged_pass"        # allowed before, allowed now -- no impact
    UNCHANGED_FAIL = "unchanged_fail"        # denied before, denied now -- no impact
    NEW_FAILURE = "new_failure"              # allowed before, denied/flagged now -- Requirement 2's "unexpected compliance block"
    NEWLY_PASSING = "newly_passing"          # denied before, allowed now -- the new rule is a relaxation for this transaction
    UNDEFINED_NOW = "undefined_now"          # new policy can't evaluate this transaction (a fact the candidate rule needs is missing) -- must route to HITL, never silently pass or fail


class HistoricalTransaction(BaseModel):
    """One replay input, reconstructed from a ledger row's `details.facts`
    snapshot (app.ledger.integration.build_ledger_events)."""

    transaction_id: str
    broker_id: str
    entity_type: str
    facts: dict
    evaluated_at: dt.datetime
    rule_id: str
    circular_number: str | None
    clause_number: str | None
    old_decision: str  # Decision value, at the OUTCOME/rule level (PASS -> allow, FAIL -> deny), not the whole transaction's cross-rule decision
    old_violations: list[str] = Field(default_factory=list)
    financial_amount: float | None = None


class BacktestOutcome(BaseModel):
    transaction_id: str
    broker_id: str
    evaluated_at: dt.datetime
    old_decision: str
    old_violations: list[str] = Field(default_factory=list)
    new_decision: str
    new_violations: list[str] = Field(default_factory=list)
    change_type: DeltaChangeType
    financial_amount: float | None = None


class BrokerImpactBreakdown(BaseModel):
    broker_id: str
    total_transactions: int
    new_failures: int
    newly_passing: int
    projected_failure_rate_pct: float


class BacktestSummary(BaseModel):
    run_id: str
    candidate_rule_id: str
    lookback_days: int
    total_transactions: int

    old_fail_count: int
    new_fail_count: int
    old_failure_rate_pct: float
    new_failure_rate_pct: float
    delta_failure_rate_pct: float = Field(..., description="new_failure_rate_pct - old_failure_rate_pct; positive means the new rule is STRICTER.")

    new_failures: int = Field(..., description="Transactions that PASSED under the old rule but FAIL under the candidate -- Requirement 2's 'unexpected compliance blocks'.")
    newly_passing: int = Field(..., description="Transactions that FAILED under the old rule but PASS under the candidate -- a relaxation.")
    undefined_count: int = Field(0, description="Transactions the candidate rule cannot evaluate at all (a required fact is missing from the historical payload).")
    unchanged_count: int

    broker_breakdown: list[BrokerImpactBreakdown] = Field(default_factory=list)


class BacktestRunRequest(BaseModel):
    candidate_rule_id: str = Field(..., description="The rule_id whose historical transactions to replay (facts are matched against this rule_id's own ledger history).")
    lookback_days: int = Field(30, ge=1, le=365, description="How many days of historical transactions to replay -- Requirement 1's '30 to 90 days of order flow', configurable up to a year.")
    candidate_jsonlogic_ast: dict | None = Field(None, description="The new candidate policy's JSON-Logic AST (app.compiler.models.JsonLogicRule.logic) -- evaluated in-process via app.backtest.jsonlogic_evaluator, no OPA server required.")
    candidate_opa_package: str | None = Field(None, description="Alternative to candidate_jsonlogic_ast: a package already published to an ISOLATED backtest-only OPA instance (settings.backtest_opa_server_url) -- never the production server.")
    tenant_id: str | None = Field(None, description="Restrict replay to one broker tenant; omit for all tenants' historical transactions for this rule_id.")


class BacktestRun(BaseModel):
    run_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    status: BacktestStatus = BacktestStatus.PENDING
    request: BacktestRunRequest
    started_at: dt.datetime | None = None
    completed_at: dt.datetime | None = None
    summary: BacktestSummary | None = None
    error: str | None = None


# --- Real-Data Rule-Impact Preview / Digital Twin Models (PRD Addendum v2 Section 8.4) ---

ADVISORY_DISCLAIMER = (
    "ADVISORY ONLY: Historical rule-impact preview is an optional compliance aid before HITL review. "
    "Zero historical impact does NOT constitute approval and does NOT infer regulatory correctness. "
    "Historical order flow may not contain edge cases or future market conditions addressed by the circular."
)


class PreviewDatasetScope(BaseModel):
    """Typed, validated dataset scope preventing arbitrary SQL queries."""

    tenant_id: str = Field(..., description="Strict tenant isolation: preview queries only this tenant's historical records.")
    rule_id: str = Field(..., description="Target regulatory rule_id whose historical transactions are being replayed.")
    lookback_days: int = Field(30, ge=1, le=365, description="Lookback window in days.")
    start_time: dt.datetime | None = Field(None, description="Optional start datetime boundary.")
    end_time: dt.datetime | None = Field(None, description="Optional end datetime boundary.")
    entity_type: str | None = Field(None, description="Optional entity type filter (e.g. 'Stockbroker').")
    max_transactions: int = Field(5000, ge=1, le=50000, description="Upper bound on historical records to prevent runaway queries.")


class RepresentativeExample(BaseModel):
    """Sanitized, redacted sample transaction protecting proprietary trade data."""

    masked_transaction_id: str = Field(..., description="Masked/truncated transaction identifier to prevent leaking raw trade details.")
    evaluated_at: dt.datetime
    change_type: DeltaChangeType
    old_decision: str
    new_decision: str
    old_violations: list[str] = Field(default_factory=list)
    new_violations: list[str] = Field(default_factory=list)
    relevant_facts: dict = Field(default_factory=dict, description="Redacted, violation-relevant numerical metrics and thresholds only.")
    financial_amount: float | None = None


class AggregateFinancialImpact(BaseModel):
    """Aggregate financial impact metrics where transaction data includes financial amounts."""

    currency: str = "INR"
    total_new_failure_amount: float = 0.0
    total_newly_passing_amount: float = 0.0
    avg_new_failure_amount: float = 0.0
    affected_transactions_with_amounts: int = 0


class RuleImpactPreviewReport(BaseModel):
    """Full tamper-evident impact preview report for HITL review screen."""

    preview_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    candidate_rule_id: str
    candidate_policy_hash: str = Field(..., description="Deterministic SHA-256 hash of the candidate rule AST/policy text.")
    candidate_version: int | None = None
    baseline_policy_hash: str | None = None
    evaluator_version: str = "regengine-jsonlogic-v1"
    execution_timestamp: dt.datetime = Field(default_factory=lambda: dt.datetime.now(dt.timezone.utc))

    scope: PreviewDatasetScope
    dataset_snapshot_hash: str = Field(..., description="Deterministic SHA-256 hash over all replayed transaction IDs and timestamps.")

    total_evaluated: int
    newly_affected: int = Field(..., description="Transactions that PASSED previously but FAIL under candidate rule.")
    no_longer_affected: int = Field(..., description="Transactions that FAILED previously but PASS under candidate rule (relaxations).")
    unchanged_count: int
    undefined_count: int = Field(0, description="Transactions candidate rule cannot evaluate due to missing facts.")

    old_fail_count: int
    new_fail_count: int
    old_failure_rate_pct: float
    new_failure_rate_pct: float
    delta_failure_rate_pct: float

    financial_impact: AggregateFinancialImpact = Field(default_factory=AggregateFinancialImpact)
    representative_examples: list[RepresentativeExample] = Field(default_factory=list)

    result_digest: str = Field(..., description="Cryptographic SHA-256 digest over preview inputs and summary counts.")
    advisory_notes: str = Field(default=ADVISORY_DISCLAIMER)
    is_live_policy_safe: bool = Field(True, description="Invariant assurance that candidate rule remains inactive and un-deployed.")


class PreviewRunRequest(BaseModel):
    """Request payload for on-demand rule impact preview."""

    candidate_rule_id: str
    candidate_jsonlogic_ast: dict | None = None
    candidate_opa_package: str | None = None
    candidate_compiled_rule_id: int | None = None
    scope: PreviewDatasetScope

