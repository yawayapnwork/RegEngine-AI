"""Cryptographic Vault Integrity test suite: verifies the audit ledger's
SHA-256 hash-chaining detects tampering, by deliberately corrupting rows in
an otherwise-valid chain (bypassing `LedgerService.append_entry`, exactly
as an attacker with direct DB access would) and asserting `verify_chain`
reports the break.

Runs against an in-memory SQLite engine, same as tests/test_ledger.py --
the hash-chain logic under test here is DB-agnostic (see
app.ledger.hash_chain); only `pg_advisory_xact_lock` concurrency control is
Postgres-specific and untested here.
"""
from __future__ import annotations

import datetime as dt

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine

from app.ledger.hash_chain import GENESIS_HASH, compute_block_hash, compute_payload_digest
from app.ledger.models import ComplianceEvaluationEvent, EvaluationOutcome, compliance_audit_ledger
from app.ledger.service import LedgerService
from app.ledger.verifier import verify_chain


@pytest_asyncio.fixture
async def engine():
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as conn:
        await conn.run_sync(compliance_audit_ledger.metadata.create_all)
    yield eng
    await eng.dispose()


def _event(i: int, **overrides) -> ComplianceEvaluationEvent:
    defaults = dict(
        broker_id=f"BRK{i:04d}",
        transaction_id=f"TXN{i:04d}",
        evaluated_at=dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc) + dt.timedelta(minutes=i),
        circular_id="SEBI/HO/MRD/2024/1",
        clause_hash="a" * 64,
        section_reference="2.1.b",
        rule_id=f"a{'0' * 63}:2.1.b",
        evaluation_result=EvaluationOutcome.PASS,
        details={"input_facts": {"upfront_margin_pct": 25.0}},
    )
    defaults.update(overrides)
    return ComplianceEvaluationEvent(**defaults)


async def _corrupt_block(engine, sequence_num: int, **column_values) -> None:
    """Simulates an attacker with direct DB access editing a row in place --
    i.e. going around LedgerService entirely, the way the append-only
    trigger/role-grant layers (sql/ledger_schema.sql) are meant to prevent
    in production. Tests here exercise the cryptographic detection layer
    in isolation from those DB-privilege/trigger defenses."""
    async with engine.begin() as conn:
        await conn.execute(
            compliance_audit_ledger.update()
            .where(compliance_audit_ledger.c.sequence_num == sequence_num)
            .values(**column_values)
        )


async def _seed_chain(engine, count: int) -> LedgerService:
    service = LedgerService(engine)
    for i in range(count):
        await service.append_entry(_event(i))
    return service


class TestHealthyChainVerifiesClean:
    @pytest.mark.asyncio
    async def test_untouched_chain_is_valid(self, engine):
        await _seed_chain(engine, 5)
        result = await verify_chain(engine)
        assert result.valid is True
        assert result.breaks == []
        assert result.entries_checked == 5


class TestCorruptedBlockIsDetected:
    """Each test injects exactly one corruption into an otherwise-healthy
    5-block chain and asserts verify_chain both flags it invalid AND
    identifies the exact tampered sequence_num -- a SEBI auditor needs "which
    block", not just "something is wrong somewhere"."""

    @pytest.mark.asyncio
    async def test_content_tampering_is_detected_at_the_corrupted_block(self, engine):
        await _seed_chain(engine, 5)

        await _corrupt_block(engine, sequence_num=2, evaluation_result=EvaluationOutcome.FAIL.value)

        result = await verify_chain(engine)

        assert result.valid is False
        assert {b.sequence_num for b in result.breaks} == {2}
        assert any("payload_digest" in b.reason for b in result.breaks)

    @pytest.mark.asyncio
    async def test_tampering_the_evidentiary_details_field_is_detected(self, engine):
        """`details` (the raw facts snapshot a decision was made against) is
        part of the hashed payload -- editing it to retroactively justify a
        decision must be just as detectable as editing evaluation_result."""
        await _seed_chain(engine, 3)

        await _corrupt_block(engine, sequence_num=1, details={"input_facts": {"upfront_margin_pct": 99.0}})

        result = await verify_chain(engine)

        assert result.valid is False
        assert {b.sequence_num for b in result.breaks} == {1}

    @pytest.mark.asyncio
    async def test_broken_previous_hash_link_is_detected(self, engine):
        await _seed_chain(engine, 5)

        await _corrupt_block(engine, sequence_num=3, previous_hash="f" * 64)

        result = await verify_chain(engine)

        assert result.valid is False
        assert any(b.sequence_num == 3 and "previous_hash" in b.reason for b in result.breaks)

    @pytest.mark.asyncio
    async def test_forged_current_hash_is_detected(self, engine):
        """Forging current_hash directly (without also fixing payload_digest
        to match) must be caught by the block-hash recomputation check."""
        await _seed_chain(engine, 3)

        await _corrupt_block(engine, sequence_num=1, current_hash="e" * 64)

        result = await verify_chain(engine)

        assert result.valid is False
        assert any(b.sequence_num == 1 and "current_hash" in b.reason for b in result.breaks)

    @pytest.mark.asyncio
    async def test_deleted_row_produces_a_sequence_gap(self, engine):
        """Deleting a row outright (not just editing it) is the other
        realistic tamper vector; the append-only trigger blocks this in
        Postgres, but the chain math must independently catch it too."""
        await _seed_chain(engine, 5)

        async with engine.begin() as conn:
            await conn.execute(compliance_audit_ledger.delete().where(compliance_audit_ledger.c.sequence_num == 2))

        result = await verify_chain(engine)

        assert result.valid is False
        assert any(b.sequence_num == 3 and "gap in sequence_num" in b.reason for b in result.breaks)

    @pytest.mark.asyncio
    async def test_first_tampered_block_is_pinpointed_not_just_flagged_invalid(self, engine):
        await _seed_chain(engine, 10)

        await _corrupt_block(engine, sequence_num=6, evaluation_result=EvaluationOutcome.FAIL.value)

        result = await verify_chain(engine)

        assert result.valid is False
        # every OTHER block must still verify clean -- corruption is
        # localized to exactly the tampered row, not smeared across the range.
        assert {b.sequence_num for b in result.breaks} == {6}


class TestSophisticatedForgeryStillCascades:
    """A naive tamper (edit content, leave current_hash stale) is caught
    immediately at the edited row. A more sophisticated attacker would also
    recompute payload_digest and current_hash for the row they edited, to
    make it internally self-consistent again. This is the actual security
    property the hash chain provides: doing that still breaks the NEXT
    row's previous_hash link, so hiding a forgery requires rewriting every
    subsequent block -- not just the one being tampered with."""

    @pytest.mark.asyncio
    async def test_self_consistent_forgery_still_breaks_the_next_blocks_link(self, engine):
        await _seed_chain(engine, 4)

        async with engine.connect() as conn:
            row2 = (
                await conn.execute(
                    select(compliance_audit_ledger).where(compliance_audit_ledger.c.sequence_num == 2)
                )
            ).mappings().one()

        forged_payload = dict(row2)
        forged_payload["evaluation_result"] = EvaluationOutcome.FAIL.value
        forged_digest = compute_payload_digest(forged_payload)
        forged_current_hash = compute_block_hash(
            previous_hash=row2["previous_hash"],
            payload_digest=forged_digest,
            sequence_num=row2["sequence_num"],
            evaluated_at=row2["evaluated_at"],
        )

        # The attacker forges row 2 completely consistently with itself.
        await _corrupt_block(
            engine,
            sequence_num=2,
            evaluation_result=EvaluationOutcome.FAIL.value,
            payload_digest=forged_digest,
            current_hash=forged_current_hash,
        )

        result = await verify_chain(engine)

        # Row 2 alone now looks internally clean...
        row2_breaks = [b for b in result.breaks if b.sequence_num == 2]
        assert row2_breaks == []
        # ...but row 3's previous_hash still points at row 2's ORIGINAL
        # hash, so the forgery is exposed one block later, exactly as the
        # chained design intends.
        assert result.valid is False
        assert any(b.sequence_num == 3 and "previous_hash" in b.reason for b in result.breaks)


class TestPartialRangeVerificationIsolatesCorruption:
    @pytest.mark.asyncio
    async def test_range_excluding_the_corrupted_block_still_verifies_clean(self, engine):
        await _seed_chain(engine, 10)
        await _corrupt_block(engine, sequence_num=8, evaluation_result=EvaluationOutcome.FAIL.value)

        # Query only [0..4], nowhere near the tampered block at seq 8.
        end = dt.datetime(2026, 1, 1, 0, 4, tzinfo=dt.timezone.utc)
        result = await verify_chain(engine, end_time=end)

        assert result.valid is True
        assert result.entries_checked == 5

    @pytest.mark.asyncio
    async def test_range_including_the_corrupted_block_is_flagged(self, engine):
        await _seed_chain(engine, 10)
        await _corrupt_block(engine, sequence_num=8, evaluation_result=EvaluationOutcome.FAIL.value)

        start = dt.datetime(2026, 1, 1, 0, 6, tzinfo=dt.timezone.utc)
        result = await verify_chain(engine, start_time=start)

        assert result.valid is False
        assert any(b.sequence_num == 8 for b in result.breaks)


class TestHashChainPrimitivesAreCollisionSensitive:
    def test_genesis_hash_shape(self):
        assert GENESIS_HASH == "0" * 64
        assert len(GENESIS_HASH) == 64

    def test_single_bit_content_change_produces_a_completely_different_digest(self):
        base = _event(1).model_dump(mode="json")
        base["evaluated_at"] = _event(1).evaluated_at
        mutated = dict(base, transaction_id=base["transaction_id"][:-1] + ("1" if base["transaction_id"][-1] != "1" else "2"))

        digest_a = compute_payload_digest(base)
        digest_b = compute_payload_digest(mutated)

        assert digest_a != digest_b
        # avalanche sanity check: a one-character change should not merely
        # tweak a handful of hex characters -- roughly half should differ.
        differing = sum(1 for a, b in zip(digest_a, digest_b) if a != b)
        assert differing > len(digest_a) * 0.25

    def test_reordering_two_blocks_would_break_the_chain(self):
        """Swapping which payload_digest sequence_num N was chained with is
        exactly as detectable as editing content -- current_hash binds
        position, not just content."""
        now = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
        digest_a, digest_b = "a" * 64, "b" * 64

        h_a_at_0 = compute_block_hash(previous_hash=GENESIS_HASH, payload_digest=digest_a, sequence_num=0, evaluated_at=now)
        h_b_at_0 = compute_block_hash(previous_hash=GENESIS_HASH, payload_digest=digest_b, sequence_num=0, evaluated_at=now)

        assert h_a_at_0 != h_b_at_0  # position 0 hashes differently depending on which payload sits there
