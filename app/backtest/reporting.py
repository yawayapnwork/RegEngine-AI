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

import hashlib
import uuid

import pandas as pd

from app.backtest.models import (
    AggregateFinancialImpact,
    BacktestOutcome,
    BacktestSummary,
    BrokerImpactBreakdown,
    DeltaChangeType,
    HistoricalTransaction,
    PreviewDatasetScope,
    RepresentativeExample,
    RuleImpactPreviewReport,
)
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
            financial_amount=txn.financial_amount,
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


# --- Rule-Impact Preview / Digital Twin Reporting Functions ---

_REDACTED_FACT_KEYS = frozenset({
    "pan", "aadhaar", "trader_id", "client_id", "account_number", "ssn",
    "secret", "token", "password", "internal_id", "client_name", "tax_id",
})


def mask_transaction_id(txn_id: str) -> str:
    """Redacts raw transaction ID to prevent leaking proprietary trade details."""
    if len(txn_id) <= 8:
        return f"txn_***{txn_id[-2:]}"
    return f"{txn_id[:4]}...{txn_id[-4:]}"


def build_representative_examples(
    replayed: list[tuple[HistoricalTransaction, str, list[str]]],
    max_per_category: int = 3,
) -> list[RepresentativeExample]:
    """Extracts sanitized, redacted sample transactions for key impact categories."""
    examples_by_type: dict[DeltaChangeType, list[RepresentativeExample]] = {
        DeltaChangeType.NEW_FAILURE: [],
        DeltaChangeType.NEWLY_PASSING: [],
        DeltaChangeType.UNDEFINED_NOW: [],
        DeltaChangeType.UNCHANGED_FAIL: [],
    }

    for txn, new_decision, new_violations in replayed:
        change_type = _classify(txn.old_decision, new_decision)
        if change_type not in examples_by_type:
            continue
        if len(examples_by_type[change_type]) >= max_per_category:
            continue

        # Redact any proprietary or PII keys from facts
        sanitized_facts = {
            k: v for k, v in txn.facts.items()
            if k.lower() not in _REDACTED_FACT_KEYS
        }

        examples_by_type[change_type].append(
            RepresentativeExample(
                masked_transaction_id=mask_transaction_id(txn.transaction_id),
                evaluated_at=txn.evaluated_at,
                change_type=change_type,
                old_decision=txn.old_decision,
                new_decision=new_decision,
                old_violations=txn.old_violations,
                new_violations=new_violations,
                relevant_facts=sanitized_facts,
                financial_amount=txn.financial_amount,
            )
        )

    # Flatten categories
    flattened: list[RepresentativeExample] = []
    for cat in (DeltaChangeType.NEW_FAILURE, DeltaChangeType.NEWLY_PASSING, DeltaChangeType.UNDEFINED_NOW, DeltaChangeType.UNCHANGED_FAIL):
        flattened.extend(examples_by_type.get(cat, []))
    return flattened


def build_aggregate_financial_impact(outcomes: list[BacktestOutcome]) -> AggregateFinancialImpact:
    """Computes aggregate financial magnitude across changed transactions."""
    new_failure_amt = 0.0
    newly_passing_amt = 0.0
    new_failure_count_with_amt = 0

    for o in outcomes:
        if o.financial_amount is not None:
            if o.change_type == DeltaChangeType.NEW_FAILURE:
                new_failure_amt += o.financial_amount
                new_failure_count_with_amt += 1
            elif o.change_type == DeltaChangeType.NEWLY_PASSING:
                newly_passing_amt += o.financial_amount

    avg_new_failure = (new_failure_amt / new_failure_count_with_amt) if new_failure_count_with_amt > 0 else 0.0

    return AggregateFinancialImpact(
        currency="INR",
        total_new_failure_amount=round(new_failure_amt, 2),
        total_newly_passing_amount=round(newly_passing_amt, 2),
        avg_new_failure_amount=round(avg_new_failure, 2),
        affected_transactions_with_amounts=new_failure_count_with_amt,
    )


def compute_preview_result_digest(
    candidate_hash: str,
    dataset_snapshot_hash: str,
    total: int,
    new_fail: int,
    newly_pass: int,
    delta_pct: float,
) -> str:
    """Generates cryptographic SHA-256 seal of the preview outcomes and inputs."""
    payload = f"{candidate_hash}:{dataset_snapshot_hash}:{total}:{new_fail}:{newly_pass}:{delta_pct:.4f}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def build_rule_impact_preview_report(
    candidate_rule_id: str,
    candidate_policy_hash: str,
    scope: PreviewDatasetScope,
    dataset_snapshot_hash: str,
    replayed: list[tuple[HistoricalTransaction, str, list[str]]],
    candidate_version: int | None = None,
    baseline_policy_hash: str | None = None,
    evaluator_version: str = "regengine-jsonlogic-v1",
    preview_id: str | None = None,
) -> RuleImpactPreviewReport:
    """Assembles the complete RuleImpactPreviewReport for HITL review."""
    outcomes = build_outcomes(replayed)
    summary = build_summary(candidate_rule_id, scope.lookback_days, outcomes)
    financial_impact = build_aggregate_financial_impact(outcomes)
    representative_examples = build_representative_examples(replayed)
    result_digest = compute_preview_result_digest(
        candidate_hash=candidate_policy_hash,
        dataset_snapshot_hash=dataset_snapshot_hash,
        total=summary.total_transactions,
        new_fail=summary.new_failures,
        newly_pass=summary.newly_passing,
        delta_pct=summary.delta_failure_rate_pct,
    )

    return RuleImpactPreviewReport(
        preview_id=preview_id or summary.run_id,
        candidate_rule_id=candidate_rule_id,
        candidate_policy_hash=candidate_policy_hash,
        candidate_version=candidate_version,
        baseline_policy_hash=baseline_policy_hash,
        evaluator_version=evaluator_version,
        scope=scope,
        dataset_snapshot_hash=dataset_snapshot_hash,
        total_evaluated=summary.total_transactions,
        newly_affected=summary.new_failures,
        no_longer_affected=summary.newly_passing,
        unchanged_count=summary.unchanged_count,
        undefined_count=summary.undefined_count,
        old_fail_count=summary.old_fail_count,
        new_fail_count=summary.new_fail_count,
        old_failure_rate_pct=summary.old_failure_rate_pct,
        new_failure_rate_pct=summary.new_failure_rate_pct,
        delta_failure_rate_pct=summary.delta_failure_rate_pct,
        financial_impact=financial_impact,
        representative_examples=representative_examples,
        result_digest=result_digest,
        is_live_policy_safe=True,
    )

