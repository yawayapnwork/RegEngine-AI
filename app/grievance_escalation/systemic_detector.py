"""Requirement 1's "systemic non-compliance" test: was THIS broker's
breach of THIS rule the first time, or has it happened repeatedly?

No existing module in this codebase answers this question live, per-
transaction (confirmed during design: `app.analytics.aggregator.BrokerStats`
is a batch/report-time pandas job over a full reporting period, built
for a PDF/Excel executive summary, not a fast per-transaction gate
check). This queries `compliance_audit_ledger` directly via SQLAlchemy
Core (the same table, same access style `app.ledger.verifier` already
uses), scoped to exactly the one broker+rule combination and rolling
window a single incoming FAIL needs to be judged against -- deliberately
NOT reusing BrokerStats, whose full-table-scan-per-period shape is the
wrong tool for a check that runs on every denied transaction.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from app.ledger.models import EvaluationOutcome, compliance_audit_ledger


@dataclass(frozen=True)
class SystemicFailureCheck:
    broker_id: str
    rule_id: str
    window_days: int
    threshold_count: int
    failure_count: int  # includes the current transaction if it was already appended to the ledger before this check runs

    @property
    def is_systemic(self) -> bool:
        return self.failure_count >= self.threshold_count


async def check_systemic_failure(
    engine: AsyncEngine,
    broker_id: str,
    rule_id: str,
    *,
    window_days: int,
    threshold_count: int,
    as_of: dt.datetime | None = None,
) -> SystemicFailureCheck:
    """Counts FAIL rows for exactly this (broker_id, rule_id) pair
    within the trailing `window_days` ending at `as_of` (defaults to
    now) -- scoped to one rule, not "any violation," since a broker
    failing five DIFFERENT unrelated rules once each is a different
    (and arguably less actionable-as-one-grievance) pattern than
    failing the SAME rule five times, and SCORES' own grievance
    category model (schemas.py) expects one grievance to concern one
    identified pattern of non-compliance."""
    as_of = as_of or dt.datetime.now(dt.timezone.utc)
    window_start = as_of - dt.timedelta(days=window_days)

    stmt = select(func.count()).select_from(compliance_audit_ledger).where(
        compliance_audit_ledger.c.broker_id == broker_id,
        compliance_audit_ledger.c.rule_id == rule_id,
        compliance_audit_ledger.c.evaluation_result == EvaluationOutcome.FAIL.value,
        compliance_audit_ledger.c.evaluated_at >= window_start,
        compliance_audit_ledger.c.evaluated_at <= as_of,
    )

    async with engine.connect() as conn:
        result = await conn.execute(stmt)
        failure_count = result.scalar_one()

    return SystemicFailureCheck(
        broker_id=broker_id, rule_id=rule_id, window_days=window_days,
        threshold_count=threshold_count, failure_count=failure_count,
    )
