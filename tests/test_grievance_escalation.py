"""Tests for the automated grievance escalation agent
(app.grievance_escalation). Follows tests/test_ledger.py's real
in-memory SQLite convention for the ledger-touching pieces (systemic
detection, single-entry proof, evidence assembly) and this session's
established hand-rolled-fake-Redis / httpx.MockTransport conventions
for everything else -- no mocking of the ledger/hash-chain logic itself.
"""
from __future__ import annotations

import datetime as dt

import httpx
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine

from app.execution.models import TransactionPayload
from app.grievance_escalation.escalation import evaluate_and_trigger_grievance_escalation
from app.grievance_escalation.evidence import build_evidence_package
from app.grievance_escalation.ledger_evidence import build_single_entry_proof, get_ledger_entry_by_transaction_id
from app.grievance_escalation.queue import GrievanceQueue, GrievanceStatus
from app.grievance_escalation.schemas import GrievanceSubmissionRequest, ScoresGrievanceStatus
from app.grievance_escalation.scores_client import ScoresApiClient, ScoresApiNotConfiguredError, _map_scores_status
from app.grievance_escalation.systemic_detector import check_systemic_failure
from app.ledger.models import ComplianceEvaluationEvent, EvaluationOutcome, compliance_audit_ledger
from app.ledger.service import LedgerService

BROKER = "BRK0001"
RULE_ID = "a" * 64 + ":4.2.b"


@pytest_asyncio.fixture
async def ledger_engine():
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as conn:
        await conn.run_sync(compliance_audit_ledger.metadata.create_all)
    yield eng
    await eng.dispose()


def _event(i: int, *, result: EvaluationOutcome = EvaluationOutcome.FAIL, rule_id: str = RULE_ID, broker_id: str = BROKER, **overrides) -> ComplianceEvaluationEvent:
    defaults = dict(
        broker_id=broker_id, transaction_id=f"TXN{i:04d}",
        evaluated_at=dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=(100 - i)),
        circular_id="SEBI/HO/MIRSD/2024/100", clause_hash="c" * 64, section_reference="4.2.b",
        rule_id=rule_id, evaluation_result=result,
        details={"violations": ["Daily collateral report was filed 3 days late."]},
    )
    defaults.update(overrides)
    return ComplianceEvaluationEvent(**defaults)


class _FakeRedis:
    def __init__(self) -> None:
        self.strings: dict[str, str] = {}
        self.sets: dict[str, set[str]] = {}
        self.published: list[tuple[str, str]] = []

    async def set(self, key: str, value: str) -> None:
        self.strings[key] = value

    async def get(self, key: str) -> str | None:
        return self.strings.get(key)

    async def sadd(self, key: str, member: str) -> None:
        self.sets.setdefault(key, set()).add(member)

    async def srem(self, key: str, member: str) -> None:
        self.sets.get(key, set()).discard(member)

    async def smembers(self, key: str) -> set[str]:
        return set(self.sets.get(key, set()))

    async def publish(self, channel: str, message: str) -> int:
        self.published.append((channel, message))
        return 0


@pytest.mark.asyncio
class TestSystemicDetector:
    async def test_below_threshold_is_not_systemic(self, ledger_engine) -> None:
        ledger = LedgerService(ledger_engine)
        await ledger.append_entry(_event(1))

        check = await check_systemic_failure(ledger_engine, BROKER, RULE_ID, window_days=30, threshold_count=3)
        assert check.failure_count == 1
        assert check.is_systemic is False

    async def test_at_threshold_is_systemic(self, ledger_engine) -> None:
        ledger = LedgerService(ledger_engine)
        for i in range(3):
            await ledger.append_entry(_event(i))

        check = await check_systemic_failure(ledger_engine, BROKER, RULE_ID, window_days=30, threshold_count=3)
        assert check.failure_count == 3
        assert check.is_systemic is True

    async def test_different_rule_ids_are_not_conflated(self, ledger_engine) -> None:
        ledger = LedgerService(ledger_engine)
        await ledger.append_entry(_event(1, rule_id="rule-a"))
        await ledger.append_entry(_event(2, rule_id="rule-b"))
        await ledger.append_entry(_event(3, rule_id="rule-a"))

        check = await check_systemic_failure(ledger_engine, BROKER, "rule-a", window_days=30, threshold_count=3)
        assert check.failure_count == 2  # only rule-a's two failures count, not rule-b's

    async def test_different_brokers_are_not_conflated(self, ledger_engine) -> None:
        ledger = LedgerService(ledger_engine)
        await ledger.append_entry(_event(1, broker_id="BRK_A"))
        await ledger.append_entry(_event(2, broker_id="BRK_B"))
        await ledger.append_entry(_event(3, broker_id="BRK_A"))

        check = await check_systemic_failure(ledger_engine, "BRK_A", RULE_ID, window_days=30, threshold_count=3)
        assert check.failure_count == 2

    async def test_pass_entries_are_never_counted(self, ledger_engine) -> None:
        ledger = LedgerService(ledger_engine)
        await ledger.append_entry(_event(1, result=EvaluationOutcome.PASS))
        await ledger.append_entry(_event(2, result=EvaluationOutcome.FAIL))

        check = await check_systemic_failure(ledger_engine, BROKER, RULE_ID, window_days=30, threshold_count=1)
        assert check.failure_count == 1

    async def test_failures_outside_the_window_are_excluded(self, ledger_engine) -> None:
        ledger = LedgerService(ledger_engine)
        await ledger.append_entry(_event(1, evaluated_at=dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=60)))
        await ledger.append_entry(_event(2))

        check = await check_systemic_failure(ledger_engine, BROKER, RULE_ID, window_days=30, threshold_count=2)
        assert check.failure_count == 1
        assert check.is_systemic is False


@pytest.mark.asyncio
class TestSingleEntryLedgerProof:
    async def test_genesis_entry_proof_is_verifiable_and_matches(self, ledger_engine) -> None:
        ledger = LedgerService(ledger_engine)
        await ledger.append_entry(_event(0))

        entry = await get_ledger_entry_by_transaction_id(ledger_engine, "TXN0000")
        assert entry is not None
        proof = await build_single_entry_proof(ledger_engine, entry)

        assert proof.chain_linkage_verifiable is True
        assert proof.current_hash_matches is True
        from app.ledger.hash_chain import GENESIS_HASH
        assert proof.previous_hash_used == GENESIS_HASH

    async def test_second_entry_chains_to_the_first(self, ledger_engine) -> None:
        ledger = LedgerService(ledger_engine)
        await ledger.append_entry(_event(0))
        await ledger.append_entry(_event(1))

        first = await get_ledger_entry_by_transaction_id(ledger_engine, "TXN0000")
        second = await get_ledger_entry_by_transaction_id(ledger_engine, "TXN0001")
        proof = await build_single_entry_proof(ledger_engine, second)

        assert proof.previous_hash_used == first.current_hash
        assert proof.current_hash_matches is True

    async def test_tampered_current_hash_is_detected(self, ledger_engine) -> None:
        ledger = LedgerService(ledger_engine)
        await ledger.append_entry(_event(0))
        entry = await get_ledger_entry_by_transaction_id(ledger_engine, "TXN0000")

        tampered = entry.model_copy(update={"current_hash": "f" * 64})
        proof = await build_single_entry_proof(ledger_engine, tampered)
        assert proof.current_hash_matches is False

    async def test_missing_transaction_returns_none(self, ledger_engine) -> None:
        assert await get_ledger_entry_by_transaction_id(ledger_engine, "does-not-exist") is None


@pytest.mark.asyncio
class TestEvidencePackage:
    async def test_build_evidence_package_without_db_session(self, ledger_engine) -> None:
        ledger = LedgerService(ledger_engine)
        await ledger.append_entry(_event(0))
        transaction = TransactionPayload(transaction_id="TXN0000", entity_type="Stockbroker", broker_id=BROKER, facts={})

        package = await build_evidence_package(ledger_engine, None, transaction)

        assert package.ledger_clause_hash == "c" * 64
        assert package.canonical_clause_hash is None  # no db session supplied
        assert package.clause_number == "4.2.b"
        assert package.ledger_proof.current_hash_matches is True

        docs = package.to_evidence_documents()
        assert {d.label for d in docs} == {"transaction_payload", "ledger_entry", "ledger_chain_proof", "clause_hash"}
        for doc in docs:
            import hashlib
            assert hashlib.sha256(doc.content.encode("utf-8")).hexdigest() == doc.sha256

    async def test_missing_ledger_entry_raises(self, ledger_engine) -> None:
        transaction = TransactionPayload(transaction_id="nonexistent", entity_type="Stockbroker", broker_id=BROKER, facts={})
        with pytest.raises(ValueError, match="No ledger entry"):
            await build_evidence_package(ledger_engine, None, transaction)


@pytest.mark.asyncio
class TestGrievanceQueue:
    async def _draft(self, queue: GrievanceQueue, grievance_id: str = "g1") -> None:
        from app.grievance_escalation.schemas import GrievanceCategory, GrievanceComplainant, GrievanceRespondent
        from app.grievance_escalation.queue import GrievanceRecord

        request = GrievanceSubmissionRequest(
            reference_id=grievance_id, category=GrievanceCategory.DELAYED_COLLATERAL_REPORTING,
            respondent=GrievanceRespondent(sebi_registration_number=BROKER, broker_id=BROKER),
            complainant=GrievanceComplainant(entity_name="RegEngine AI Compliance Monitoring", contact_email="x@y.com"),
            description="test", evidence=[],
        )
        record = GrievanceRecord(grievance_id=grievance_id, request=request, max_retries=5, response_due_at=dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=21))
        await queue.create_draft(record)

    async def test_full_lifecycle(self) -> None:
        queue = GrievanceQueue(_FakeRedis(), key_prefix="regengine:grievance")
        await self._draft(queue)

        drafted = await queue.get("g1")
        assert drafted.status == GrievanceStatus.DRAFTED

        confirmed = await queue.confirm_for_submission("g1")
        assert confirmed.status == GrievanceStatus.PENDING_SUBMISSION
        assert [r.grievance_id for r in await queue.list_pending_submission()] == ["g1"]

        submitting = await queue.mark_submitting("g1")
        assert submitting.attempt_count == 1

        submitted = await queue.mark_submitted("g1", "SCORES-REF-001")
        assert submitted.status == GrievanceStatus.SUBMITTED
        assert await queue.list_pending_submission() == []
        assert [r.grievance_id for r in await queue.list_open()] == ["g1"]

        resolved = await queue.update_status("g1", ScoresGrievanceStatus.RESOLVED, "Broker fined and remediated.")
        assert resolved.status == GrievanceStatus.RESOLVED
        assert resolved.resolved_at is not None
        assert await queue.list_open() == []

    async def test_confirm_twice_raises(self) -> None:
        queue = GrievanceQueue(_FakeRedis(), key_prefix="regengine:grievance")
        await self._draft(queue)
        await queue.confirm_for_submission("g1")
        with pytest.raises(ValueError):
            await queue.confirm_for_submission("g1")

    async def test_mark_retry_keeps_it_pending(self) -> None:
        queue = GrievanceQueue(_FakeRedis(), key_prefix="regengine:grievance")
        await self._draft(queue)
        await queue.confirm_for_submission("g1")
        await queue.mark_submitting("g1")
        retried = await queue.mark_retry("g1", "timeout")
        assert retried.status == GrievanceStatus.PENDING_SUBMISSION
        assert retried.last_error == "timeout"


class TestScoresStatusMapping:
    def test_recognized_status_maps_correctly(self) -> None:
        assert _map_scores_status("Resolved") == ScoresGrievanceStatus.RESOLVED
        assert _map_scores_status("under_review") == ScoresGrievanceStatus.UNDER_REVIEW

    def test_unrecognized_status_fails_closed_to_unknown(self) -> None:
        assert _map_scores_status("some_new_status_sebi_invented") == ScoresGrievanceStatus.UNKNOWN


_RealAsyncClient = httpx.AsyncClient


def _patch_transport(monkeypatch: pytest.MonkeyPatch, handler) -> None:
    def _fake_async_client(**kwargs):
        kwargs.pop("transport", None)
        return _RealAsyncClient(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _fake_async_client)


@pytest.mark.asyncio
class TestScoresApiClient:
    async def test_refuses_to_submit_without_configured_base_url(self) -> None:
        from app.config import get_settings

        settings = get_settings().model_copy(update={"scores_api_base_url": None})
        client = ScoresApiClient(settings)
        with pytest.raises(ScoresApiNotConfiguredError):
            await client.submit_grievance(_dummy_request())

    async def test_submit_grievance_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from app.config import get_settings

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/v2/grievances"
            return httpx.Response(200, json={"scores_reference_number": "SCORES-REF-001", "status": "submitted"})

        _patch_transport(monkeypatch, handler)
        settings = get_settings().model_copy(update={"scores_api_base_url": "https://scores.example.gov.in"})
        client = ScoresApiClient(settings)

        response = await client.submit_grievance(_dummy_request())
        assert response.scores_reference_number == "SCORES-REF-001"
        assert response.status == ScoresGrievanceStatus.SUBMITTED

    async def test_get_status_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from app.config import get_settings

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/v2/grievances/SCORES-REF-001"
            return httpx.Response(200, json={"status": "resolved", "last_updated_at": "2026-01-01T00:00:00+00:00", "resolution_summary": "Fine imposed."})

        _patch_transport(monkeypatch, handler)
        settings = get_settings().model_copy(update={"scores_api_base_url": "https://scores.example.gov.in"})
        client = ScoresApiClient(settings)

        response = await client.get_grievance_status("SCORES-REF-001")
        assert response.status == ScoresGrievanceStatus.RESOLVED
        assert response.resolution_summary == "Fine imposed."

    async def test_submit_grievance_error_response_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from app.config import get_settings
        from app.grievance_escalation.scores_client import ScoresApiError

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(400, text="malformed payload")

        _patch_transport(monkeypatch, handler)
        settings = get_settings().model_copy(update={"scores_api_base_url": "https://scores.example.gov.in"})
        client = ScoresApiClient(settings)

        with pytest.raises(ScoresApiError):
            await client.submit_grievance(_dummy_request())


def _dummy_request() -> GrievanceSubmissionRequest:
    from app.grievance_escalation.schemas import GrievanceCategory, GrievanceComplainant, GrievanceRespondent

    return GrievanceSubmissionRequest(
        reference_id="g1", category=GrievanceCategory.DELAYED_COLLATERAL_REPORTING,
        respondent=GrievanceRespondent(sebi_registration_number=BROKER, broker_id=BROKER),
        complainant=GrievanceComplainant(entity_name="RegEngine AI Compliance Monitoring", contact_email="x@y.com"),
        description="test", evidence=[],
    )


@pytest.mark.asyncio
class TestEscalationTrigger:
    async def test_isolated_failure_drafts_nothing(self, ledger_engine) -> None:
        from app.config import get_settings

        ledger = LedgerService(ledger_engine)
        entry = await ledger.append_entry(_event(0))
        transaction = TransactionPayload(transaction_id="TXN0000", entity_type="Stockbroker", broker_id=BROKER, facts={})
        settings = get_settings().model_copy(update={
            "grievance_escalation_enabled": True, "grievance_escalation_systemic_failure_threshold_count": 3,
        })

        result = await evaluate_and_trigger_grievance_escalation(entry, transaction, ledger_engine, _FakeRedis(), settings)
        assert result is None

    async def test_systemic_failure_drafts_a_grievance_with_correct_evidence(self, ledger_engine) -> None:
        from app.config import get_settings

        ledger = LedgerService(ledger_engine)
        for i in range(3):
            entry = await ledger.append_entry(_event(i))
        transaction = TransactionPayload(transaction_id="TXN0002", entity_type="Stockbroker", broker_id=BROKER, facts={})
        settings = get_settings().model_copy(update={
            "grievance_escalation_enabled": True, "grievance_escalation_systemic_failure_threshold_count": 3,
            "grievance_escalation_auto_submit_enabled": False,
        })
        redis_client = _FakeRedis()

        record = await evaluate_and_trigger_grievance_escalation(entry, transaction, ledger_engine, redis_client, settings)

        assert record is not None
        assert record.status.value == "drafted"
        assert record.respondent.broker_id == BROKER
        assert record.request.category.value == "delayed_collateral_reporting"
        assert len(record.request.evidence) == 4
        assert "3 time(s)" in record.request.description

        queue = GrievanceQueue(redis_client, settings.grievance_escalation_key_prefix)
        stored = await queue.get(record.grievance_id)
        assert stored is not None and stored.status.value == "drafted"

    async def test_disabled_flag_short_circuits(self, ledger_engine) -> None:
        from app.config import get_settings

        ledger = LedgerService(ledger_engine)
        entry = await ledger.append_entry(_event(0))
        transaction = TransactionPayload(transaction_id="TXN0000", entity_type="Stockbroker", broker_id=BROKER, facts={})
        settings = get_settings().model_copy(update={"grievance_escalation_enabled": False})

        result = await evaluate_and_trigger_grievance_escalation(entry, transaction, ledger_engine, _FakeRedis(), settings)
        assert result is None
