"""Tests for the tamper-evident audit ledger: hash-chain primitives run
against a real (in-memory SQLite) database via app.ledger.service /
app.ledger.verifier. SQLite stands in for Postgres here only to exercise
the DB-agnostic chain logic in `LedgerService`/`verify_chain` without a
live Postgres instance; the advisory lock and immutability triggers in
sql/ledger_schema.sql are Postgres-specific and are not exercised here."""
from __future__ import annotations

import datetime as dt

import pytest
import pytest_asyncio
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
        circular_id="SEBI/HO/MIRSD/2026/01",
        clause_hash="a" * 64,
        section_reference="3.2.1",
        rule_id=f"a{'0'*63}:3.2.1",
        evaluation_result=EvaluationOutcome.PASS,
    )
    defaults.update(overrides)
    return ComplianceEvaluationEvent(**defaults)


class TestHashChainPrimitives:
    def test_canonical_payload_is_order_independent(self):
        event_a = _event(1).model_dump(mode="json")
        event_a["evaluated_at"] = _event(1).evaluated_at
        event_b = dict(reversed(list(event_a.items())))
        assert compute_payload_digest(event_a) == compute_payload_digest(event_b)

    def test_payload_digest_changes_when_a_field_changes(self):
        base = _event(1).model_dump(mode="json")
        base["evaluated_at"] = _event(1).evaluated_at
        mutated = dict(base, rule_id="different-rule-id")
        assert compute_payload_digest(base) != compute_payload_digest(mutated)

    def test_block_hash_chains_to_previous(self):
        digest = "b" * 64
        now = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
        h1 = compute_block_hash(previous_hash=GENESIS_HASH, payload_digest=digest, sequence_num=0, evaluated_at=now)
        h2 = compute_block_hash(previous_hash=h1, payload_digest=digest, sequence_num=1, evaluated_at=now)
        assert h1 != h2  # same digest, different position -> different block hash


@pytest.mark.asyncio
class TestLedgerServiceAndVerifier:
    async def test_append_creates_genesis_block(self, engine):
        service = LedgerService(engine)
        entry = await service.append_entry(_event(1))
        assert entry.sequence_num == 0
        assert entry.previous_hash == GENESIS_HASH
        assert len(entry.current_hash) == 64

    async def test_sequential_appends_chain_together(self, engine):
        service = LedgerService(engine)
        first = await service.append_entry(_event(1))
        second = await service.append_entry(_event(2, evaluation_result=EvaluationOutcome.FAIL))
        assert second.sequence_num == first.sequence_num + 1
        assert second.previous_hash == first.current_hash

    async def test_verify_chain_valid_over_full_range(self, engine):
        service = LedgerService(engine)
        for i in range(5):
            await service.append_entry(_event(i))
        result = await verify_chain(engine)
        assert result.valid is True
        assert result.entries_checked == 5
        assert result.breaks == []

    async def test_verify_chain_valid_over_partial_time_range(self, engine):
        service = LedgerService(engine)
        for i in range(10):
            await service.append_entry(_event(i))
        start = dt.datetime(2026, 1, 1, 0, 3, tzinfo=dt.timezone.utc)
        end = dt.datetime(2026, 1, 1, 0, 6, tzinfo=dt.timezone.utc)
        result = await verify_chain(engine, start_time=start, end_time=end)
        assert result.valid is True
        assert result.entries_checked == 4  # minutes 3,4,5,6
        assert result.range_start_sequence == 3
        assert result.range_end_sequence == 6

    async def test_verify_chain_detects_content_tampering(self, engine):
        service = LedgerService(engine)
        for i in range(3):
            await service.append_entry(_event(i))

        async with engine.begin() as conn:
            await conn.execute(
                compliance_audit_ledger.update()
                .where(compliance_audit_ledger.c.sequence_num == 1)
                .values(evaluation_result=EvaluationOutcome.FAIL.value)  # simulate a forged row, bypassing the append path
            )

        result = await verify_chain(engine)
        assert result.valid is False
        reasons = {b.reason for b in result.breaks}
        assert any("payload_digest" in r for r in reasons)

    async def test_verify_chain_detects_broken_previous_hash_link(self, engine):
        service = LedgerService(engine)
        for i in range(3):
            await service.append_entry(_event(i))

        async with engine.begin() as conn:
            await conn.execute(
                compliance_audit_ledger.update()
                .where(compliance_audit_ledger.c.sequence_num == 2)
                .values(previous_hash="f" * 64)
            )

        result = await verify_chain(engine)
        assert result.valid is False
        reasons = {b.reason for b in result.breaks}
        assert any("previous_hash" in r for r in reasons)

    async def test_verify_chain_empty_range_is_trivially_valid(self, engine):
        service = LedgerService(engine)
        await service.append_entry(_event(1))
        far_future_start = dt.datetime(2030, 1, 1, tzinfo=dt.timezone.utc)
        result = await verify_chain(engine, start_time=far_future_start)
        assert result.valid is True
        assert result.entries_checked == 0

    async def test_hitl_review_id_required_iff_hitl_review(self):
        with pytest.raises(ValueError):
            _event(1, evaluation_result=EvaluationOutcome.HITL_REVIEW)  # missing hitl_review_id
        with pytest.raises(ValueError):
            _event(1, hitl_review_id="case-123")  # PASS with a hitl_review_id set
        # valid combination does not raise
        _event(1, evaluation_result=EvaluationOutcome.HITL_REVIEW, hitl_review_id="case-123")
