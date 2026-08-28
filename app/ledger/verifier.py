"""Verifies the cryptographic integrity of the audit ledger's hash chain
over an arbitrary time range, for SEBI audit requests of the shape "prove
nothing in [start, end] was altered after the fact".

Verifying a *range* still requires anchoring it to history: if row N is
the first row in the requested window, we must also fetch row N-1 (even
though it's outside [start, end]) to confirm row N's `previous_hash`
correctly points at it — otherwise a forged row N with a self-consistent
but fabricated `previous_hash` would verify "clean" in isolation.
"""
from __future__ import annotations

import datetime as dt

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from app.ledger.hash_chain import GENESIS_HASH, compute_block_hash, compute_payload_digest
from app.ledger.models import ChainBreak, ChainVerificationResult, compliance_audit_ledger


async def verify_chain(
    engine: AsyncEngine,
    start_time: dt.datetime | None = None,
    end_time: dt.datetime | None = None,
) -> ChainVerificationResult:
    """Recomputes payload_digest and current_hash for every row in
    [start_time, end_time] (inclusive; None on either side means
    unbounded) and compares against what is stored. Any mismatch, any gap
    in sequence_num, or a previous_hash that does not equal the prior
    row's current_hash is reported as a `ChainBreak` — the result is
    `valid=True` iff the list of breaks is empty."""
    async with engine.connect() as conn:
        query = select(compliance_audit_ledger).order_by(compliance_audit_ledger.c.sequence_num.asc())
        if start_time is not None:
            query = query.where(compliance_audit_ledger.c.evaluated_at >= start_time)
        if end_time is not None:
            query = query.where(compliance_audit_ledger.c.evaluated_at <= end_time)
        rows = (await conn.execute(query)).mappings().all()

        if not rows:
            return ChainVerificationResult(valid=True, entries_checked=0)

        first_sequence = rows[0]["sequence_num"]
        anchor_hash = GENESIS_HASH
        if first_sequence > 0:
            anchor = (
                await conn.execute(
                    select(compliance_audit_ledger.c.current_hash).where(
                        compliance_audit_ledger.c.sequence_num == first_sequence - 1
                    )
                )
            ).first()
            if anchor is None:
                return ChainVerificationResult(
                    valid=False,
                    entries_checked=len(rows),
                    range_start_sequence=first_sequence,
                    range_end_sequence=rows[-1]["sequence_num"],
                    breaks=[
                        ChainBreak(
                            sequence_num=first_sequence,
                            reason="predecessor row (sequence_num - 1) is missing; chain cannot be anchored into history",
                            expected="a row with the previous sequence_num",
                            actual="no row found",
                        )
                    ],
                )
            anchor_hash = anchor.current_hash

    breaks: list[ChainBreak] = []
    previous_hash = anchor_hash
    previous_sequence = first_sequence - 1

    for row in rows:
        seq = row["sequence_num"]

        if seq != previous_sequence + 1:
            breaks.append(
                ChainBreak(
                    sequence_num=seq,
                    reason="gap in sequence_num — a row is missing between this and the prior one",
                    expected=str(previous_sequence + 1),
                    actual=str(seq),
                )
            )

        if row["previous_hash"] != previous_hash:
            breaks.append(
                ChainBreak(
                    sequence_num=seq,
                    reason="previous_hash does not match the prior row's current_hash",
                    expected=previous_hash,
                    actual=row["previous_hash"],
                )
            )

        recomputed_digest = compute_payload_digest(dict(row))
        if recomputed_digest != row["payload_digest"]:
            breaks.append(
                ChainBreak(
                    sequence_num=seq,
                    reason="payload_digest does not match recomputed hash of stored business fields — row content was altered",
                    expected=recomputed_digest,
                    actual=row["payload_digest"],
                )
            )

        recomputed_block_hash = compute_block_hash(
            previous_hash=row["previous_hash"],
            payload_digest=row["payload_digest"],
            sequence_num=seq,
            evaluated_at=row["evaluated_at"],
        )
        if recomputed_block_hash != row["current_hash"]:
            breaks.append(
                ChainBreak(
                    sequence_num=seq,
                    reason="current_hash does not match recomputed block hash",
                    expected=recomputed_block_hash,
                    actual=row["current_hash"],
                )
            )

        previous_hash = row["current_hash"]
        previous_sequence = seq

    return ChainVerificationResult(
        valid=not breaks,
        entries_checked=len(rows),
        range_start_sequence=rows[0]["sequence_num"],
        range_end_sequence=rows[-1]["sequence_num"],
        breaks=breaks,
    )
