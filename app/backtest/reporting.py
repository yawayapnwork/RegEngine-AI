"""Pandas-based breach predictive analytics and delta reporting
(Requirements 2 and 3).

`build_outcomes` classifies each replayed transaction's old-vs-new
decision pair into a `DeltaChangeType`; `build_summary` aggregates those
classifications (projected failure rate, false-positive/new-block counts,
per-broker breakdown) via a `pandas.DataFrame` groupby, which is both the
simplest correct way to do this aggregation and keeps the arithmetic
inspectable/testable independent of the async replay machinery that
produced the rows.
"""
from __future__ import annotations

import uuid

import pandas as pd

from app.backtest.models import BacktestOutcome, BacktestSummary, BrokerImpactBreakdown, DeltaChangeType, HistoricalTransaction
from app.execution.models import Decision


def _classify(old_decision: str, new_decision: str) -> DeltaChangeType:
    if new_decision == Decision.FLAGGED.value:
        return DeltaChangeType.UNDEFINED_NOW
    old_fail = old_decision == Decision.DENY.value
    new_fail = new_decision == Decision.DENY.value
    if old_fail and new_fail:
        return DeltaChangeType.UNCHANGED_FAIL
    if not old_fail and not new_fail:
        return DeltaChangeType.UNCHANGED_PASS
    if not old_fail and new_fail:
        return DeltaChangeType.NEW_FAILURE
    return DeltaChangeType.NEWLY_PASSING


def build_outcomes(replayed: list[tuple[HistoricalTransaction, str, list[str]]]) -> list[BacktestOutcome]:
    return [
        BacktestOutcome(
            transaction_id=txn.transaction_id,
            broker_id=txn.broker_id,
            evaluated_at=txn.evaluated_at,
            old_decision=txn.old_decision,
            old_violations=txn.old_violations,
            new_decision=new_decision,
            new_violations=new_violations,
            change_type=_classify(txn.old_decision, new_decision),
        )
        for txn, new_decision, new_violations in replayed
    ]


def outcomes_to_dataframe(outcomes: list[BacktestOutcome]) -> pd.DataFrame:
    """The side-by-side delta comparison (Requirement 3) as a DataFrame --
    one row per historical transaction, old and new decisions in adjacent
    columns, ready for `.to_csv()`/`.to_excel()` or direct display."""
    if not outcomes:
        return pd.DataFrame(
            columns=["transaction_id", "broker_id", "evaluated_at", "old_decision", "new_decision", "change_type", "old_violations", "new_violations"]
        )
    return pd.DataFrame(
        [
            {
                "transaction_id": o.transaction_id,
                "broker_id": o.broker_id,
                "evaluated_at": o.evaluated_at,
                "old_decision": o.old_decision,
                "new_decision": o.new_decision,
                "change_type": o.change_type.value,
                "old_violations": "; ".join(o.old_violations),
                "new_violations": "; ".join(o.new_violations),
            }
            for o in outcomes
        ]
    )


def build_summary(candidate_rule_id: str, lookback_days: int, outcomes: list[BacktestOutcome], run_id: str | None = None) -> BacktestSummary:
    total = len(outcomes)
    if total == 0:
        return BacktestSummary(
            run_id=run_id or str(uuid.uuid4()), candidate_rule_id=candidate_rule_id, lookback_days=lookback_days,
            total_transactions=0, old_fail_count=0, new_fail_count=0, old_failure_rate_pct=0.0, new_failure_rate_pct=0.0,
            delta_failure_rate_pct=0.0, new_failures=0, newly_passing=0, undefined_count=0, unchanged_count=0, broker_breakdown=[],
        )

    df = outcomes_to_dataframe(outcomes)

    old_fail_count = int((df["old_decision"] == Decision.DENY.value).sum())
    new_fail_count = int((df["new_decision"] == Decision.DENY.value).sum())
    old_failure_rate = old_fail_count / total * 100.0
    new_failure_rate = new_fail_count / total * 100.0

    change_counts = df["change_type"].value_counts().to_dict()
    new_failures = int(change_counts.get(DeltaChangeType.NEW_FAILURE.value, 0))
    newly_passing = int(change_counts.get(DeltaChangeType.NEWLY_PASSING.value, 0))
    undefined_count = int(change_counts.get(DeltaChangeType.UNDEFINED_NOW.value, 0))
    unchanged_count = int(change_counts.get(DeltaChangeType.UNCHANGED_PASS.value, 0)) + int(change_counts.get(DeltaChangeType.UNCHANGED_FAIL.value, 0))

    broker_breakdown: list[BrokerImpactBreakdown] = []
    for broker_id, group in df.groupby("broker_id"):
        group_total = len(group)
        group_new_fail = int((group["new_decision"] == Decision.DENY.value).sum())
        broker_breakdown.append(
            BrokerImpactBreakdown(
                broker_id=str(broker_id),
                total_transactions=group_total,
                new_failures=int((group["change_type"] == DeltaChangeType.NEW_FAILURE.value).sum()),
                newly_passing=int((group["change_type"] == DeltaChangeType.NEWLY_PASSING.value).sum()),
                projected_failure_rate_pct=round(group_new_fail / group_total * 100.0, 2) if group_total else 0.0,
            )
        )
    broker_breakdown.sort(key=lambda b: b.projected_failure_rate_pct, reverse=True)

    return BacktestSummary(
        run_id=run_id or str(uuid.uuid4()),
        candidate_rule_id=candidate_rule_id,
        lookback_days=lookback_days,
        total_transactions=total,
        old_fail_count=old_fail_count,
        new_fail_count=new_fail_count,
        old_failure_rate_pct=round(old_failure_rate, 2),
        new_failure_rate_pct=round(new_failure_rate, 2),
        delta_failure_rate_pct=round(new_failure_rate - old_failure_rate, 2),
        new_failures=new_failures,
        newly_passing=newly_passing,
        undefined_count=undefined_count,
        unchanged_count=unchanged_count,
        broker_breakdown=broker_breakdown,
    )
