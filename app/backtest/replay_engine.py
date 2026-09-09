"""The replay engine: pulls historical transactions for a rule_id out of
the audit ledger (Requirement 1: "ingest historical broker transaction
logs... last 30 to 90 days of order flow") and re-evaluates each one's
`facts` snapshot against a candidate policy (Requirement 1's "run them
through the OPA execution engine using a newly generated policy bundle" --
see app.backtest.candidate_evaluator for why the default path achieves
this without an OPA server), producing one `BacktestOutcome` per
transaction.

Fully async, bounded-concurrency (`settings.backtest_concurrency`) --
replaying 90 days of order flow for an active rule can be tens of
thousands of transactions; unbounded `asyncio.gather` would either exhaust
memory/file descriptors or (for the OPA-backed evaluator) flood the
backtest OPA instance with a request storm.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

import hashlib

from app.backtest.candidate_evaluator import CandidateEvaluator
from app.backtest.models import HistoricalTransaction, PreviewDatasetScope
from app.execution.models import Decision
from app.ledger.models import compliance_audit_ledger

logger = logging.getLogger(__name__)

_FINANCIAL_KEYS = (
    "order_value",
    "trade_amount",
    "notional_value",
    "margin_shortfall",
    "transaction_amount",
    "amount",
    "portfolio_value",
)


def extract_financial_value(facts: dict) -> float | None:
    """Extracts a numerical financial magnitude from transaction facts if present."""
    for key in _FINANCIAL_KEYS:
        val = facts.get(key)
        if val is not None and isinstance(val, (int, float)):
            return float(val)
        if val is not None and isinstance(val, str):
            try:
                return float(val.replace(",", "").strip())
            except ValueError:
                continue
    return None


def compute_dataset_snapshot_hash(transactions: list[HistoricalTransaction]) -> str:
    """Computes a deterministic cryptographic SHA-256 digest over the transaction dataset snapshot."""
    hasher = hashlib.sha256()
    # Sort deterministically by transaction_id and evaluated_at
    sorted_txns = sorted(transactions, key=lambda t: (t.transaction_id, str(t.evaluated_at)))
    for t in sorted_txns:
        eval_str = t.evaluated_at.isoformat() if isinstance(t.evaluated_at, dt.datetime) else str(t.evaluated_at)
        chunk = f"{t.transaction_id}:{t.broker_id}:{t.rule_id}:{eval_str}:{t.old_decision}\n"
        hasher.update(chunk.encode("utf-8"))
    return hasher.hexdigest()


async def fetch_historical_transactions(
    ledger_engine: AsyncEngine,
    rule_id: str,
    lookback_days: int = 30,
    tenant_id: str | None = None,
    start_time: dt.datetime | None = None,
    end_time: dt.datetime | None = None,
    entity_type: str | None = None,
    limit: int | None = None,
    scope: PreviewDatasetScope | None = None,
) -> list[HistoricalTransaction]:
    """Reconstructs replayable transactions from `compliance_audit_ledger` rows.

    Enforces strict parameterized querying (preventing arbitrary SQL) and strict tenant isolation.
    """
    if scope is not None:
        rule_id = scope.rule_id
        lookback_days = scope.lookback_days
        tenant_id = scope.tenant_id
        start_time = scope.start_time
        end_time = scope.end_time
        entity_type = scope.entity_type
        limit = scope.max_transactions

    cutoff = start_time or (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=lookback_days))

    query = (
        select(compliance_audit_ledger)
        .where(compliance_audit_ledger.c.rule_id == rule_id, compliance_audit_ledger.c.evaluated_at >= cutoff)
        .order_by(compliance_audit_ledger.c.evaluated_at.asc())
    )
    if end_time:
        query = query.where(compliance_audit_ledger.c.evaluated_at <= end_time)
    if tenant_id:
        query = query.where(compliance_audit_ledger.c.broker_id == tenant_id)
    if limit:
        query = query.limit(limit)

    async with ledger_engine.connect() as conn:
        rows = (await conn.execute(query)).mappings().all()

    transactions: list[HistoricalTransaction] = []
    skipped = 0
    for row in rows:
        details = row["details"] or {}
        facts = details.get("facts")
        row_entity_type = details.get("entity_type")
        if facts is None or row_entity_type is None:
            skipped += 1
            continue

        if entity_type and row_entity_type != entity_type:
            continue

        # Strict multi-tenancy verification: runtime defense-in-depth
        row_broker_id = str(row["broker_id"])
        if tenant_id and row_broker_id != tenant_id:
            raise ValueError(
                f"Tenant isolation breach: transaction {row['transaction_id']} has broker_id={row_broker_id} "
                f"which does not match requested tenant_id={tenant_id}."
            )

        transactions.append(
            HistoricalTransaction(
                transaction_id=row["transaction_id"],
                broker_id=row_broker_id,
                entity_type=row_entity_type,
                facts=facts,
                evaluated_at=row["evaluated_at"],
                rule_id=row["rule_id"],
                circular_number=row["circular_id"],
                clause_number=row["section_reference"],
                old_decision=Decision.ALLOW.value if row["evaluation_result"] == "PASS" else (
                    Decision.FLAGGED.value if row["evaluation_result"] == "HITL_REVIEW" else Decision.DENY.value
                ),
                old_violations=list(details.get("violations", []) or []),
                financial_amount=extract_financial_value(facts),
            )
        )

    if skipped:
        logger.warning(
            "Backtest for rule_id=%s: skipped %d/%d ledger row(s) with no 'facts' snapshot; %d transaction(s) replayable.",
            rule_id, skipped, len(rows), len(transactions),
        )
    return transactions


async def replay_transaction(transaction: HistoricalTransaction, evaluator: CandidateEvaluator) -> tuple[str, list[str]]:
    return await evaluator.evaluate(transaction.entity_type, transaction.facts)


async def replay_all(
    transactions: list[HistoricalTransaction],
    evaluator: CandidateEvaluator,
    concurrency: int,
    cancellation_token: asyncio.Event | None = None,
) -> list[tuple[HistoricalTransaction, str, list[str]]]:
    semaphore = asyncio.Semaphore(concurrency)

    async def _bounded(txn: HistoricalTransaction) -> tuple[HistoricalTransaction, str, list[str]]:
        if cancellation_token and cancellation_token.is_set():
            raise asyncio.CancelledError("Rule impact replay cancelled by user.")
        async with semaphore:
            if cancellation_token and cancellation_token.is_set():
                raise asyncio.CancelledError("Rule impact replay cancelled by user.")
            new_decision, new_violations = await replay_transaction(txn, evaluator)
            return txn, new_decision, new_violations

    return list(await asyncio.gather(*(_bounded(t) for t in transactions)))

