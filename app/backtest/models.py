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


class BacktestOutcome(BaseModel):
    transaction_id: str
    broker_id: str
    evaluated_at: dt.datetime
    old_decision: str
    old_violations: list[str] = Field(default_factory=list)
    new_decision: str
    new_violations: list[str] = Field(default_factory=list)
    change_type: DeltaChangeType


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
