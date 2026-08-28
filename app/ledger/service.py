"""Appends compliance-evaluation events to the hash-chained ledger.

Concurrency: two evaluations finishing at the same instant must not both
read the same "last row" and compute sibling blocks that both claim to
follow it (a fork, silently breaking the single linear history an auditor
expects). This is serialized with `pg_advisory_xact_lock`, a session-scoped
Postgres lock held only for the duration of the append transaction — cheap
compared to locking the whole table, and automatically released on
commit/rollback so a crashed writer can never leave the ledger stuck.
"""
from __future__ import annotations

import datetime as dt
import logging

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from app.ledger.hash_chain import GENESIS_HASH, compute_block_hash, compute_payload_digest
from app.ledger.models import ComplianceEvaluationEvent, LedgerEntry, compliance_audit_ledger

logger = logging.getLogger(__name__)

# Arbitrary fixed key identifying "the compliance_audit_ledger append lock"
# in Postgres's advisory lock keyspace. Any distinct int64 works; it only
# needs to be stable and not collide with other advisory locks this
# deployment takes elsewhere.
_LEDGER_ADVISORY_LOCK_KEY = 0x5EB1_1EDC  # "SEBI LEDGer" as a memorable hex constant


class ChainIntegrityError(RuntimeError):
    """Raised when the last row read back does not match what append_entry
    itself just wrote — indicates a concurrent writer bypassed the
    advisory lock (e.g. a second connection pool not going through this
    service) or a driver-level transaction isolation misconfiguration."""


class LedgerService:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def _acquire_ledger_lock(self, conn: AsyncConnection) -> None:
        if conn.dialect.name == "postgresql":
            await conn.execute(select(func.pg_advisory_xact_lock(_LEDGER_ADVISORY_LOCK_KEY)))
        # Non-Postgres dialects (e.g. SQLite in unit tests) have no
        # equivalent primitive; those tests are single-writer so the lock
        # is a no-op rather than a correctness requirement there.

    async def _last_entry(self, conn: AsyncConnection) -> tuple[int, str] | None:
        row = (
            await conn.execute(
                select(compliance_audit_ledger.c.sequence_num, compliance_audit_ledger.c.current_hash)
                .order_by(compliance_audit_ledger.c.sequence_num.desc())
                .limit(1)
            )
        ).first()
        return (row.sequence_num, row.current_hash) if row else None

    async def append_entry(self, event: ComplianceEvaluationEvent) -> LedgerEntry:
        event_dict = event.model_dump(mode="json")
        # model_dump(mode="json") stringifies datetimes; hash_chain needs the
        # real datetime object for a stable ISO format independent of
        # pydantic's json-mode formatting choices.
        event_dict["evaluated_at"] = event.evaluated_at

        async with self._engine.begin() as conn:
            await self._acquire_ledger_lock(conn)

            last = await self._last_entry(conn)
            sequence_num = (last[0] + 1) if last else 0
            previous_hash = last[1] if last else GENESIS_HASH

            payload_digest = compute_payload_digest(event_dict)
            current_hash = compute_block_hash(
                previous_hash=previous_hash,
                payload_digest=payload_digest,
                sequence_num=sequence_num,
                evaluated_at=event.evaluated_at,
            )

            values = {
                "sequence_num": sequence_num,
                "broker_id": event.broker_id,
                "transaction_id": event.transaction_id,
                "evaluated_at": event.evaluated_at,
                "circular_id": event.circular_id,
                "clause_hash": event.clause_hash,
                "section_reference": event.section_reference,
                "rule_id": event.rule_id,
                "evaluation_result": event.evaluation_result.value,
                "hitl_review_id": event.hitl_review_id,
                "details": event.details,
                "payload_digest": payload_digest,
                "previous_hash": previous_hash,
                "current_hash": current_hash,
                "created_at": dt.datetime.now(dt.timezone.utc),
            }
            result = await conn.execute(compliance_audit_ledger.insert().values(**values).returning(compliance_audit_ledger.c.id))
            row_id = result.scalar_one()

        logger.info(
            "Ledger entry appended: sequence_num=%d transaction_id=%s rule_id=%s result=%s",
            sequence_num,
            event.transaction_id,
            event.rule_id,
            event.evaluation_result.value,
        )
        return LedgerEntry(id=row_id, **values)
