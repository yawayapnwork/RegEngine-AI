"""Tests for app.regulatory_filing.

Real end-to-end proofs, not mocks standing in for the thing being
tested: schema serialization is validated against real XSD/JSON Schema
documents (lxml/jsonschema); PKI signing is independently verified by a
real `openssl cms -verify` subprocess (including a genuine tamper-
detection check); SFTP submission runs against a real, in-process
paramiko SFTP server (not a mocked transport) with a background thread
simulating an MII's asynchronous intake+receipt process. Only the
regulatory portal API (no sandboxed SEBI portal is reachable from here)
and the Celery task wiring (redis faked, matching this repo's
established `_FakeRedis` convention) use test doubles.
"""
from __future__ import annotations

import base64
import datetime as dt
import os
import shutil
import socket
import threading
import time

import httpx
import pytest
import pytest_asyncio

from app.config import Settings
from app.incident.models import BreachEventType
from app.ledger.models import ComplianceEvaluationEvent, EvaluationOutcome, compliance_audit_ledger
from app.ledger.service import LedgerService
from app.regulatory_filing.collateral_aggregator import collect_compliance_log_filing, collect_daily_collateral_filing
from app.regulatory_filing.json_serializer import (
    JsonSchemaValidationError,
    serialize_compliance_log_json,
    serialize_daily_collateral_json,
    validate_json,
)
from app.regulatory_filing.schemas import FilingTarget, FilingType
from app.regulatory_filing.signing import (
    SigningKeyNotConfiguredError,
    SoftwareX509SigningBackend,
    generate_self_signed_signing_identity,
    verify_signed_filing_with_openssl,
)
from app.regulatory_filing.submission import (
    FilingAcknowledgment,
    FilingQueue,
    FilingStatus,
    FilingSubmissionRecord,
    PortalApiFilingSubmitter,
    SftpFilingSubmitter,
    SubmissionChannel,
    SubmissionError,
)
from app.regulatory_filing.xml_serializer import (
    XmlSchemaValidationError,
    serialize_compliance_log_xml,
    serialize_daily_collateral_xml,
    validate_xml,
)

pytestmark_openssl = pytest.mark.skipif(shutil.which("openssl") is None, reason="requires a real openssl binary on PATH")


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


@pytest_asyncio.fixture
async def ledger_engine():
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(compliance_audit_ledger.metadata.create_all)
    yield engine
    await engine.dispose()


async def _seed_ledger(engine, day: dt.datetime) -> None:
    service = LedgerService(engine)
    await service.append_entry(ComplianceEvaluationEvent(
        broker_id="BRK0001", transaction_id="TXN0001", evaluated_at=day,
        circular_id="SEBI/HO/MIRSD/2026/01", clause_hash="a" * 64, section_reference="3.2.1",
        rule_id="a" * 64 + ":3.2.1", evaluation_result=EvaluationOutcome.PASS,
        details={"facts": {"upfront_margin_pct": 25}, "violations": []},
    ))
    await service.append_entry(ComplianceEvaluationEvent(
        broker_id="BRK0001", transaction_id="TXN0002", evaluated_at=day + dt.timedelta(minutes=1),
        circular_id="SEBI/HO/MIRSD/2026/01", clause_hash="a" * 64, section_reference="3.2.1",
        rule_id="a" * 64 + ":3.2.1", evaluation_result=EvaluationOutcome.FAIL,
        details={"facts": {"upfront_margin_pct": 10}, "violations": ["Upfront Margin is 10%, required >= 20%"]},
    ))
    await service.append_entry(ComplianceEvaluationEvent(
        broker_id="BRK0002", transaction_id="TXN0003", evaluated_at=day + dt.timedelta(minutes=2),
        circular_id="SEBI/HO/MIRSD/2026/01", clause_hash="a" * 64, section_reference="3.2.1",
        rule_id="a" * 64 + ":3.2.1", evaluation_result=EvaluationOutcome.HITL_REVIEW,
        hitl_review_id="hitl-1",
        details={},
    ))


DAY = dt.datetime(2026, 8, 29, 10, 0, tzinfo=dt.timezone.utc)


# --------------------------------------------------------------------------
# Aggregation + serialization
# --------------------------------------------------------------------------


@pytest.mark.asyncio
class TestCollateralAggregator:
    async def test_compliance_log_filing_reflects_every_ledger_row(self, ledger_engine):
        await _seed_ledger(ledger_engine, DAY)
        filing = await collect_compliance_log_filing(ledger_engine, period_start=DAY.date(), period_end=DAY.date(), reporting_entity_code="INZ000000001")
        assert filing.header.record_count == 3
        assert filing.header.filing_type == FilingType.COMPLIANCE_LOG
        assert {r.transaction_id for r in filing.records} == {"TXN0001", "TXN0002", "TXN0003"}
        assert filing.records[0].sequence_num == 0  # ordered by sequence_num ascending

    async def test_daily_collateral_filing_aggregates_per_broker(self, ledger_engine):
        await _seed_ledger(ledger_engine, DAY)
        filing = await collect_daily_collateral_filing(ledger_engine, report_date=DAY.date(), reporting_entity_code="INZ000000001")
        by_broker = {r.broker_id: r for r in filing.records}

        brk1 = by_broker["BRK0001"]
        assert brk1.transactions_evaluated == 2
        assert brk1.transactions_passed == 1
        assert brk1.transactions_failed == 1
        assert brk1.avg_upfront_margin_pct == pytest.approx(17.5)
        assert brk1.min_upfront_margin_pct == pytest.approx(10.0)
        assert brk1.shortfall_count == 1

        brk2 = by_broker["BRK0002"]
        assert brk2.transactions_evaluated == 1
        assert brk2.transactions_flagged_hitl == 1
        assert brk2.avg_upfront_margin_pct is None  # no facts.upfront_margin_pct on this row

    async def test_empty_period_produces_zero_records(self, ledger_engine):
        filing = await collect_compliance_log_filing(ledger_engine, period_start=dt.date(2020, 1, 1), period_end=dt.date(2020, 1, 1), reporting_entity_code="INZ000000001")
        assert filing.header.record_count == 0
        assert filing.records == []


@pytest.mark.asyncio
class TestXmlSerialization:
    async def test_compliance_log_xml_validates_against_xsd(self, ledger_engine):
        await _seed_ledger(ledger_engine, DAY)
        filing = await collect_compliance_log_filing(ledger_engine, period_start=DAY.date(), period_end=DAY.date(), reporting_entity_code="INZ000000001")
        xml_bytes = serialize_compliance_log_xml(filing)
        validate_xml(xml_bytes, "compliance_log_v1.xsd")  # raises on failure

    async def test_daily_collateral_xml_validates_against_xsd(self, ledger_engine):
        await _seed_ledger(ledger_engine, DAY)
        filing = await collect_daily_collateral_filing(ledger_engine, report_date=DAY.date(), reporting_entity_code="INZ000000001")
        xml_bytes = serialize_daily_collateral_xml(filing)
        validate_xml(xml_bytes, "daily_collateral_v1.xsd")


def test_malformed_xml_fails_validation():
    bad_xml = b'<?xml version="1.0"?><RegulatoryFiling xmlns="urn:regengine:sebi-filing:v1"><NotTheRightShape/></RegulatoryFiling>'
    with pytest.raises(XmlSchemaValidationError):
        validate_xml(bad_xml, "compliance_log_v1.xsd")


@pytest.mark.asyncio
class TestJsonSerialization:
    async def test_compliance_log_json_validates_against_schema(self, ledger_engine):
        await _seed_ledger(ledger_engine, DAY)
        filing = await collect_compliance_log_filing(ledger_engine, period_start=DAY.date(), period_end=DAY.date(), reporting_entity_code="INZ000000001")
        json_bytes = serialize_compliance_log_json(filing)
        validate_json(json_bytes, "compliance_log_v1.schema.json")

    async def test_daily_collateral_json_validates_against_schema(self, ledger_engine):
        await _seed_ledger(ledger_engine, DAY)
        filing = await collect_daily_collateral_filing(ledger_engine, report_date=DAY.date(), reporting_entity_code="INZ000000001")
        json_bytes = serialize_daily_collateral_json(filing)
        validate_json(json_bytes, "daily_collateral_v1.schema.json")


def test_malformed_json_fails_validation():
    bad = b'{"header": {}, "records": "not-a-list"}'
    with pytest.raises(JsonSchemaValidationError):
        validate_json(bad, "compliance_log_v1.schema.json")


# --------------------------------------------------------------------------
# Signing
# --------------------------------------------------------------------------


class TestSoftwareX509SigningBackend:
    def test_sign_without_configured_key_raises(self):
        backend = SoftwareX509SigningBackend(Settings())
        with pytest.raises(SigningKeyNotConfiguredError):
            backend.sign(b"data", filing_id="f1")

    @pytestmark_openssl
    def test_sign_and_independently_verify_with_openssl(self):
        priv, cert = generate_self_signed_signing_identity()
        settings = Settings(regulatory_filing_signing_private_key_pem=priv, regulatory_filing_signing_cert_pem=cert)
        backend = SoftwareX509SigningBackend(settings)

        data = b'{"filing_id": "f1", "records": []}'
        signed = backend.sign(data, filing_id="f1")

        assert signed.backend == "software"
        assert signed.filing_id == "f1"
        assert verify_signed_filing_with_openssl(data, signed) is True

    @pytestmark_openssl
    def test_tampered_payload_fails_verification(self):
        priv, cert = generate_self_signed_signing_identity()
        settings = Settings(regulatory_filing_signing_private_key_pem=priv, regulatory_filing_signing_cert_pem=cert)
        backend = SoftwareX509SigningBackend(settings)

        data = b'{"filing_id": "f1", "records": []}'
        signed = backend.sign(data, filing_id="f1")

        tampered = data.replace(b"f1", b"f2")
        assert verify_signed_filing_with_openssl(tampered, signed) is False

    @pytestmark_openssl
    def test_signature_from_a_different_key_fails_verification(self):
        priv1, cert1 = generate_self_signed_signing_identity()
        priv2, cert2 = generate_self_signed_signing_identity()
        data = b"payload"

        signed_with_key1 = SoftwareX509SigningBackend(Settings(regulatory_filing_signing_private_key_pem=priv1, regulatory_filing_signing_cert_pem=cert1)).sign(data, "f1")
        # Swap in a certificate that does NOT match the key that produced the signature.
        forged = signed_with_key1.model_copy(update={"signer_certificate_pem": cert2})
        assert verify_signed_filing_with_openssl(data, forged) is False


# --------------------------------------------------------------------------
# FilingQueue
# --------------------------------------------------------------------------


class _FakeRedis:
    def __init__(self) -> None:
        self.strings: dict[str, str] = {}
        self.sets: dict[str, set[str]] = {}

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

    async def aclose(self) -> None:
        pass


def _sample_record(**overrides) -> FilingSubmissionRecord:
    from app.regulatory_filing.signing import SignedFiling

    defaults = dict(
        filing_type=FilingType.COMPLIANCE_LOG,
        target=FilingTarget.SEBI,
        channel=SubmissionChannel.SFTP,
        filename="compliance_log_2026-08-29.json",
        payload_b64=base64.b64encode(b'{"records": []}').decode(),
        content_type="application/json",
        signature=SignedFiling(filing_id="f1", payload_sha256="a" * 64, signature_der_b64="ZGVhZGJlZWY=", signer_certificate_pem="-----BEGIN CERTIFICATE-----\nx\n-----END CERTIFICATE-----", backend="software"),
        max_retries=3,
    )
    defaults.update(overrides)
    return FilingSubmissionRecord(**defaults)


@pytest.mark.asyncio
class TestFilingQueue:
    async def test_enqueue_and_get_round_trips(self):
        queue = FilingQueue(_FakeRedis(), "test:filing")
        record = _sample_record()
        await queue.enqueue(record)

        fetched = await queue.get(record.filing_id)
        assert fetched is not None
        assert fetched.filing_id == record.filing_id
        assert fetched.payload == b'{"records": []}'

    async def test_pending_lifecycle(self):
        queue = FilingQueue(_FakeRedis(), "test:filing")
        record = _sample_record()
        await queue.enqueue(record)

        assert len(await queue.list_pending()) == 1

        submitting = await queue.mark_submitting(record.filing_id)
        assert submitting.status == FilingStatus.SUBMITTING
        assert submitting.attempt_count == 1

        acked = await queue.mark_acknowledged(record.filing_id, FilingAcknowledgment(acknowledgment_reference="ACK-123"))
        assert acked.status == FilingStatus.ACKNOWLEDGED
        assert acked.acknowledgment.acknowledgment_reference == "ACK-123"
        assert await queue.list_pending() == []  # removed from pending once acknowledged

    async def test_retry_keeps_filing_pending(self):
        queue = FilingQueue(_FakeRedis(), "test:filing")
        record = _sample_record()
        await queue.enqueue(record)
        await queue.mark_submitting(record.filing_id)

        retried = await queue.mark_retry(record.filing_id, "connection refused")
        assert retried.status == FilingStatus.PENDING
        assert retried.last_error == "connection refused"
        assert len(await queue.list_pending()) == 1

    async def test_failed_removes_from_pending(self):
        queue = FilingQueue(_FakeRedis(), "test:filing")
        record = _sample_record()
        await queue.enqueue(record)
        await queue.mark_submitting(record.filing_id)

        failed = await queue.mark_failed(record.filing_id, "exhausted retries")
        assert failed.status == FilingStatus.FAILED
        assert await queue.list_pending() == []


# --------------------------------------------------------------------------
# SFTP submission -- against a REAL in-process paramiko SFTP server.
# --------------------------------------------------------------------------


def _make_stub_sftp_server_class():
    """paramiko.SFTPServerInterface must be the base class (it supplies
    session_started/session_ended and other lifecycle hooks paramiko's
    SFTPServer calls unconditionally) -- built lazily inside a factory
    function rather than at module import time so this test file still
    collects cleanly in an environment where paramiko isn't installed."""
    import paramiko

    class _StubSFTPServer(paramiko.SFTPServerInterface):
        """Minimal SFTPServerInterface backed by a real local directory
        -- virtual SFTP paths map 1:1 onto real files under `root_dir`.
        Only the operations SftpFilingSubmitter actually uses (open for
        write, open for read, stat) are implemented."""

        def __init__(self, server, *largs, root_dir: str, **kwargs):
            super().__init__(server, *largs, **kwargs)
            self.root_dir = root_dir

        def _realpath(self, path: str) -> str:
            return os.path.join(self.root_dir, path.lstrip("/"))

        def open(self, path, flags, attr):
            real_path = self._realpath(path)
            try:
                if flags & os.O_WRONLY or flags & os.O_CREAT:
                    mode = "ab" if flags & os.O_APPEND else "wb"
                    f = open(real_path, mode)
                else:
                    f = open(real_path, "rb")
            except OSError:
                return paramiko.SFTP_NO_SUCH_FILE

            handle = paramiko.SFTPHandle(flags)
            handle.readfile = f
            handle.writefile = f
            return handle

        def stat(self, path):
            real_path = self._realpath(path)
            try:
                return paramiko.SFTPAttributes.from_stat(os.stat(real_path))
            except OSError:
                return paramiko.SFTP_NO_SUCH_FILE

        lstat = stat

        def list_folder(self, path):
            real_path = self._realpath(path)
            try:
                out = []
                for fname in os.listdir(real_path):
                    attr = paramiko.SFTPAttributes.from_stat(os.stat(os.path.join(real_path, fname)))
                    attr.filename = fname
                    out.append(attr)
                return out
            except OSError:
                return paramiko.SFTP_NO_SUCH_FILE

    return _StubSFTPServer


@pytest.fixture
def sftp_server(tmp_path):
    """Starts a REAL paramiko SFTP server bound to 127.0.0.1 on an
    ephemeral port, serving `tmp_path` as its filesystem root. Yields
    (host, port, host_key). Torn down after the test."""
    import paramiko

    stub_sftp_server_class = _make_stub_sftp_server_class()
    host_key = paramiko.RSAKey.generate(2048)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    sock.listen(5)
    host, port = sock.getsockname()

    stop_event = threading.Event()

    def _serve_one_connection():
        sock.settimeout(1.0)
        while not stop_event.is_set():
            try:
                client_sock, _ = sock.accept()
            except socket.timeout:
                continue
            transport = paramiko.Transport(client_sock)
            transport.add_server_key(host_key)
            transport.set_subsystem_handler("sftp", paramiko.SFTPServer, sftp_si=stub_sftp_server_class, root_dir=str(tmp_path))

            class _ServerInterface(paramiko.ServerInterface):
                def check_auth_password(self, username, password):
                    return paramiko.AUTH_SUCCESSFUL

                def check_auth_publickey(self, username, key):
                    return paramiko.AUTH_SUCCESSFUL

                def get_allowed_auths(self, username):
                    return "password,publickey"

                def check_channel_request(self, kind, chanid):
                    return paramiko.OPEN_SUCCEEDED

                # check_channel_subsystem_request is intentionally NOT
                # overridden -- paramiko.ServerInterface's default
                # implementation already dispatches to whatever
                # Transport.set_subsystem_handler registered (the sftp
                # handler set up above), which is exactly what's needed.

            try:
                transport.start_server(server=_ServerInterface())
                channel = transport.accept(5)
                if channel is not None:
                    while transport.is_active() and not stop_event.is_set():
                        time.sleep(0.05)
            except Exception:
                pass

    thread = threading.Thread(target=_serve_one_connection, daemon=True)
    thread.start()

    yield host, port

    stop_event.set()
    sock.close()


def _simulate_intake_ack(root_dir: str, payload_filename: str, timeout: float = 10.0) -> None:
    """Runs in a background thread: waits for the uploaded payload file
    to appear, then drops a `.ack` receipt -- simulating a real MII's
    asynchronous SFTP intake process."""
    payload_path = os.path.join(root_dir, payload_filename)
    ack_path = payload_path + ".ack"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if os.path.exists(payload_path):
            with open(ack_path, "w") as f:
                f.write("ACK-SEBI-2026-0001")
            return
        time.sleep(0.1)


@pytest.mark.asyncio
class TestSftpFilingSubmitter:
    async def test_upload_and_acknowledgment_round_trip(self, sftp_server, tmp_path):
        host, port = sftp_server
        settings = Settings(
            regulatory_filing_sftp_host=host,
            regulatory_filing_sftp_port=port,
            regulatory_filing_sftp_username="regengine",
            regulatory_filing_sftp_password="anything",  # _StubServerInterface accepts any password
            regulatory_filing_sftp_remote_dir="/",
        )
        record = _sample_record(filename="compliance_log_test.json")

        intake_thread = threading.Thread(target=_simulate_intake_ack, args=(str(tmp_path), record.filename), daemon=True)
        intake_thread.start()

        submitter = SftpFilingSubmitter(settings)
        ack = await submitter.submit(record, ack_poll_timeout_seconds=8.0, ack_poll_interval_seconds=0.2)

        assert ack.acknowledgment_reference == "ACK-SEBI-2026-0001"
        assert (tmp_path / record.filename).read_bytes() == record.payload
        assert (tmp_path / f"{record.filename}.p7s").exists()

    async def test_timeout_when_no_acknowledgment_arrives(self, sftp_server, tmp_path):
        host, port = sftp_server
        settings = Settings(
            regulatory_filing_sftp_host=host,
            regulatory_filing_sftp_port=port,
            regulatory_filing_sftp_username="regengine",
            regulatory_filing_sftp_password="anything",
            regulatory_filing_sftp_remote_dir="/",
        )
        record = _sample_record(filename="compliance_log_never_acked.json")
        submitter = SftpFilingSubmitter(settings)

        with pytest.raises(SubmissionError, match="No acknowledgment"):
            await submitter.submit(record, ack_poll_timeout_seconds=1.0, ack_poll_interval_seconds=0.2)

        assert (tmp_path / record.filename).exists()  # the upload itself still happened


# --------------------------------------------------------------------------
# Portal API submission (httpx.MockTransport)
# --------------------------------------------------------------------------


@pytest.mark.asyncio
class TestPortalApiFilingSubmitter:
    async def test_successful_submission_parses_acknowledgment(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/v1/filings"
            return httpx.Response(200, json={"acknowledgment_reference": "SEBI-ACK-9001", "acknowledged_at": "2026-08-29T12:00:00+00:00"})

        _patch_httpx_transport(monkeypatch, handler)
        settings = Settings(regulatory_filing_portal_api_base_url="https://filings.sebi.example", regulatory_filing_portal_api_key="secret-key")
        record = _sample_record(channel=SubmissionChannel.PORTAL_API)

        submitter = PortalApiFilingSubmitter(settings)
        ack = await submitter.submit(record)
        assert ack.acknowledgment_reference == "SEBI-ACK-9001"

    async def test_rejection_raises_submission_error(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(422, text="malformed filing payload")

        _patch_httpx_transport(monkeypatch, handler)
        settings = Settings(regulatory_filing_portal_api_base_url="https://filings.sebi.example")
        record = _sample_record(channel=SubmissionChannel.PORTAL_API)

        submitter = PortalApiFilingSubmitter(settings)
        with pytest.raises(SubmissionError, match="422"):
            await submitter.submit(record)

    async def test_missing_base_url_raises(self):
        submitter = PortalApiFilingSubmitter(Settings())
        with pytest.raises(SubmissionError):
            await submitter.submit(_sample_record(channel=SubmissionChannel.PORTAL_API))


_RealAsyncClient = httpx.AsyncClient


def _patch_httpx_transport(monkeypatch: pytest.MonkeyPatch, handler) -> None:
    def _fake_async_client(**kwargs):
        kwargs.pop("transport", None)
        return _RealAsyncClient(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _fake_async_client)


# --------------------------------------------------------------------------
# Celery task wiring (redis faked; matches tests/test_resilience_tasks.py's convention)
# --------------------------------------------------------------------------


class TestSubmitFilingTask:
    def test_disabled_flag_is_a_no_op(self, monkeypatch):
        import app.regulatory_filing.tasks as mod

        monkeypatch.setattr(mod, "get_settings", lambda: Settings(regulatory_filing_enabled=False))
        called = []
        monkeypatch.setattr(mod, "_submit_one", lambda filing_id: called.append(filing_id))

        mod.submit_filing_task.apply(args=["f1"]).get()
        assert called == []

    def test_successful_submission_marks_acknowledged(self, monkeypatch):
        import app.regulatory_filing.tasks as mod
        import app.regulatory_filing.submission as submission_mod

        fake_redis = _FakeRedis()
        monkeypatch.setattr(mod, "get_settings", lambda: Settings(regulatory_filing_enabled=True))
        monkeypatch.setattr(mod.aioredis, "from_url", lambda *a, **kw: fake_redis)

        class _StubSubmitter:
            async def submit(self, record):
                return FilingAcknowledgment(acknowledgment_reference="ACK-1")

        monkeypatch.setattr(mod, "get_submitter", lambda record, settings: _StubSubmitter())

        breach_calls = []

        async def _fake_raise_breach_event(event, redis_client, settings):
            breach_calls.append(event)

        monkeypatch.setattr(mod, "raise_breach_event", _fake_raise_breach_event)

        async def _seed():
            queue = FilingQueue(fake_redis, Settings().regulatory_filing_key_prefix)
            record = _sample_record()
            await queue.enqueue(record)
            return record.filing_id

        import asyncio

        filing_id = asyncio.run(_seed())
        mod.submit_filing_task.apply(args=[filing_id]).get()

        async def _check():
            queue = FilingQueue(fake_redis, Settings().regulatory_filing_key_prefix)
            record = await queue.get(filing_id)
            assert record.status == FilingStatus.ACKNOWLEDGED
            assert record.acknowledgment.acknowledgment_reference == "ACK-1"

        asyncio.run(_check())
        assert breach_calls == []

    def test_exhausted_retries_raises_breach_event(self, monkeypatch):
        import app.regulatory_filing.tasks as mod

        fake_redis = _FakeRedis()
        monkeypatch.setattr(mod, "get_settings", lambda: Settings(regulatory_filing_enabled=True))
        monkeypatch.setattr(mod.aioredis, "from_url", lambda *a, **kw: fake_redis)

        class _FailingSubmitter:
            async def submit(self, record):
                raise SubmissionError("destination unreachable")

        monkeypatch.setattr(mod, "get_submitter", lambda record, settings: _FailingSubmitter())
        monkeypatch.setattr(mod, "is_transient", lambda exc: False)  # force immediate exhaustion regardless of attempt_count

        breach_calls = []

        async def _fake_raise_breach_event(event, redis_client, settings):
            breach_calls.append(event)

        monkeypatch.setattr(mod, "raise_breach_event", _fake_raise_breach_event)

        import asyncio

        async def _seed():
            queue = FilingQueue(fake_redis, Settings().regulatory_filing_key_prefix)
            record = _sample_record(max_retries=3)
            await queue.enqueue(record)
            return record.filing_id

        filing_id = asyncio.run(_seed())
        mod.submit_filing_task.apply(args=[filing_id]).get()

        assert len(breach_calls) == 1
        assert breach_calls[0].event_type == BreachEventType.FILING_SUBMISSION_FAILED

        async def _check():
            queue = FilingQueue(fake_redis, Settings().regulatory_filing_key_prefix)
            record = await queue.get(filing_id)
            assert record.status == FilingStatus.FAILED

        asyncio.run(_check())


class TestSubmitPendingFilingsTask:
    def test_dispatches_one_task_per_pending_filing(self, monkeypatch):
        import app.regulatory_filing.tasks as mod

        fake_redis = _FakeRedis()
        monkeypatch.setattr(mod, "get_settings", lambda: Settings(regulatory_filing_enabled=True))
        monkeypatch.setattr(mod.aioredis, "from_url", lambda *a, **kw: fake_redis)

        dispatched = []
        monkeypatch.setattr(mod.submit_filing_task, "delay", lambda filing_id: dispatched.append(filing_id))

        import asyncio

        async def _seed():
            queue = FilingQueue(fake_redis, Settings().regulatory_filing_key_prefix)
            r1 = _sample_record()
            r2 = _sample_record()
            await queue.enqueue(r1)
            await queue.enqueue(r2)
            return r1.filing_id, r2.filing_id

        ids = asyncio.run(_seed())
        count = mod.submit_pending_filings_task.apply().get()

        assert count == 2
        assert set(dispatched) == set(ids)

    def test_disabled_flag_dispatches_nothing(self, monkeypatch):
        import app.regulatory_filing.tasks as mod

        monkeypatch.setattr(mod, "get_settings", lambda: Settings(regulatory_filing_enabled=False))
        count = mod.submit_pending_filings_task.apply().get()
        assert count == 0
