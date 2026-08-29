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

from app.backtest.candidate_evaluator import CandidateEvaluator
from app.backtest.models import HistoricalTransaction
from app.execution.models import Decision
from app.ledger.models import compliance_audit_ledger

logger = logging.getLogger(__name__)


async def fetch_historical_transactions(
    ledger_engine: AsyncEngine,
    rule_id: str,
    lookback_days: int,
    tenant_id: str | None = None,
) -> list[HistoricalTransaction]:
    """Reconstructs replayable transactions from `compliance_audit_ledger`
    rows for `rule_id` within the lookback window. Rows whose
    `details.facts` is absent (written before this ledger integration
    started snapshotting facts -- see app.ledger.integration's comment on
    that field) are skipped, not fabricated -- a backtest run's own
    summary reports how many historical rows were actually replayable so
    that gap is visible, not silently masked."""
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=lookback_days)

    query = (
        select(compliance_audit_ledger)
        .where(compliance_audit_ledger.c.rule_id == rule_id, compliance_audit_ledger.c.evaluated_at >= cutoff)
        .order_by(compliance_audit_ledger.c.evaluated_at.asc())
    )
    if tenant_id:
        query = query.where(compliance_audit_ledger.c.broker_id == tenant_id)

    async with ledger_engine.connect() as conn:
        rows = (await conn.execute(query)).mappings().all()

    transactions: list[HistoricalTransaction] = []
    skipped = 0
    for row in rows:
        details = row["details"] or {}
        facts = details.get("facts")
        entity_type = details.get("entity_type")
        if facts is None or entity_type is None:
            skipped += 1
            continue
        transactions.append(
            HistoricalTransaction(
                transaction_id=row["transaction_id"],
                broker_id=row["broker_id"],
                entity_type=entity_type,
                facts=facts,
                evaluated_at=row["evaluated_at"],
                rule_id=row["rule_id"],
                circular_number=row["circular_id"],
                clause_number=row["section_reference"],
                old_decision=Decision.ALLOW.value if row["evaluation_result"] == "PASS" else (
                    Decision.FLAGGED.value if row["evaluation_result"] == "HITL_REVIEW" else Decision.DENY.value
                ),
                old_violations=list(details.get("violations", []) or []),
            )
        )

    if skipped:
        logger.warning(
            "Backtest for rule_id=%s: skipped %d/%d ledger row(s) with no 'facts' snapshot (recorded before "
            "app.ledger.integration started capturing it); only %d transaction(s) are replayable.",
            rule_id, skipped, len(rows), len(transactions),
        )
    return transactions


async def replay_transaction(transaction: HistoricalTransaction, evaluator: CandidateEvaluator) -> tuple[str, list[str]]:
    return await evaluator.evaluate(transaction.entity_type, transaction.facts)


async def replay_all(
    transactions: list[HistoricalTransaction],
    evaluator: CandidateEvaluator,
    concurrency: int,
) -> list[tuple[HistoricalTransaction, str, list[str]]]:
    semaphore = asyncio.Semaphore(concurrency)

    async def _bounded(txn: HistoricalTransaction) -> tuple[HistoricalTransaction, str, list[str]]:
        async with semaphore:
            new_decision, new_violations = await replay_transaction(txn, evaluator)
            return txn, new_decision, new_violations

    return list(await asyncio.gather(*(_bounded(t) for t in transactions)))
