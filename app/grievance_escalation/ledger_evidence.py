"""Requirement 2's "SHA-256 audit ledger proof" ingredient: fetches ONE
ledger entry by transaction_id and re-derives its block hash, proving
that specific entry's chain linkage independently rather than trusting
its stored `current_hash` at face value.

Neither `app.ledger.service.LedgerService` (append-only) nor
`app.ledger.verifier.verify_chain` (whole-range verification, anchored
to the range's first row) expose a "prove exactly this one entry"
operation -- this module is the missing piece, built from the same
`app.ledger.hash_chain` primitives `verify_chain` itself uses, so the
two never risk disagreeing about what "correct" means.
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from app.ledger.hash_chain import GENESIS_HASH, compute_block_hash
from app.ledger.models import LedgerEntry, compliance_audit_ledger


@dataclass(frozen=True)
class SingleEntryLedgerProof:
    entry: LedgerEntry
    previous_hash_used: str | None  # None only if this entry's predecessor row (sequence_num - 1) is itself missing from the ledger -- see `chain_linkage_verifiable`
    recomputed_current_hash: str | None
    chain_linkage_verifiable: bool  # False only when previous_hash_used is None -- can't recompute without it
    current_hash_matches: bool | None  # None iff not chain_linkage_verifiable


async def get_ledger_entry_by_transaction_id(engine: AsyncEngine, transaction_id: str) -> LedgerEntry | None:
    stmt = select(compliance_audit_ledger).where(compliance_audit_ledger.c.transaction_id == transaction_id)
    async with engine.connect() as conn:
        row = (await conn.execute(stmt)).mappings().first()
    return LedgerEntry.model_validate(dict(row)) if row is not None else None


async def build_single_entry_proof(engine: AsyncEngine, entry: LedgerEntry) -> SingleEntryLedgerProof:
    """Re-derives `entry.current_hash` from `entry.previous_hash`,
    `entry.payload_digest`, `entry.sequence_num`, and `entry.evaluated_at`
    -- exactly `app.ledger.verifier.verify_chain`'s per-row block-hash
    check (step (d) in that function), applied to one row instead of a
    range. Does NOT re-derive `payload_digest` itself from raw fields
    (that would need `clause_hash`/`details`, which this function
    already has via `entry` -- unlike the offline browser verifier built
    earlier this session, which only had an EXPORT lacking those fields
    -- so a more thorough caller could extend this to also call
    `compute_payload_digest` and compare; left as a documented
    extension point since block-hash linkage is what Requirement 2's
    "ledger proof" is centrally about)."""
    if entry.sequence_num == 0:
        previous_hash_used = GENESIS_HASH
    else:
        predecessor = await get_ledger_entry_by_sequence_num(engine, entry.sequence_num - 1)
        previous_hash_used = predecessor.current_hash if predecessor is not None else None

    if previous_hash_used is None:
        return SingleEntryLedgerProof(entry=entry, previous_hash_used=None, recomputed_current_hash=None, chain_linkage_verifiable=False, current_hash_matches=None)

    recomputed = compute_block_hash(
        previous_hash=previous_hash_used, payload_digest=entry.payload_digest,
        sequence_num=entry.sequence_num, evaluated_at=entry.evaluated_at,
    )
    return SingleEntryLedgerProof(
        entry=entry, previous_hash_used=previous_hash_used, recomputed_current_hash=recomputed,
        chain_linkage_verifiable=True, current_hash_matches=(recomputed == entry.current_hash),
    )


async def get_ledger_entry_by_sequence_num(engine: AsyncEngine, sequence_num: int) -> LedgerEntry | None:
    stmt = select(compliance_audit_ledger).where(compliance_audit_ledger.c.sequence_num == sequence_num)
    async with engine.connect() as conn:
        row = (await conn.execute(stmt)).mappings().first()
    return LedgerEntry.model_validate(dict(row)) if row is not None else None
