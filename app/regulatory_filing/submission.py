"""Requirement 3's submission channels and queue: a signed filing goes
out via SFTP or a regulatory portal API, with a Redis-backed queue
tracking status/attempts (mirrors app.execution.hitl_queue.HITLQueue's
established storage shape in this codebase) that
app.regulatory_filing.tasks' Celery worker drains on a schedule.
"""
from __future__ import annotations

import base64
import datetime as dt
import io
import logging
import uuid
from enum import Enum

import httpx
import redis.asyncio as redis
from pydantic import BaseModel, Field

from app.config import Settings
from app.regulatory_filing.schemas import FilingTarget, FilingType
from app.regulatory_filing.signing import SignedFiling

logger = logging.getLogger(__name__)


class SubmissionChannel(str, Enum):
    SFTP = "sftp"
    PORTAL_API = "portal_api"


class FilingStatus(str, Enum):
    PENDING = "pending"        # queued, not yet attempted (or awaiting retry)
    SUBMITTING = "submitting"  # a worker currently has this in flight
    ACKNOWLEDGED = "acknowledged"  # destination confirmed receipt
    FAILED = "failed"          # retry budget exhausted


class SubmissionError(RuntimeError):
    """A submission attempt failed. Distinguish transient (network/
    connectivity -- see app.resilience.retry_policy.is_transient, which
    already recognizes OSError/ConnectionError/TimeoutError/httpx
    transport errors, all of which paramiko and httpx raise naturally)
    from permanent (the destination rejected the filing itself, e.g. a
    malformed payload or an expired credential) by the underlying
    exception TYPE raised -- this class itself carries no separate
    transient/permanent flag; app.regulatory_filing.tasks classifies via
    `is_transient` exactly like every other Celery task in this
    codebase."""


class FilingAcknowledgment(BaseModel):
    acknowledgment_reference: str
    acknowledged_at: dt.datetime = Field(default_factory=lambda: dt.datetime.now(dt.timezone.utc))
    raw_detail: str | None = None


class FilingSubmissionRecord(BaseModel):
    """One filing's full lifecycle record -- the queue's stored unit.
    Carries the payload/signature INLINE (base64) rather than a
    separate blob store: a daily SEBI/MII filing is at most a few MB of
    JSON/XML, well within a single Redis value, and inlining avoids a
    second storage system just for this."""

    filing_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    filing_type: FilingType
    target: FilingTarget
    channel: SubmissionChannel
    filename: str

    payload_b64: str
    content_type: str  # "application/xml" | "application/json"
    signature: SignedFiling

    status: FilingStatus = FilingStatus.PENDING
    attempt_count: int = 0
    max_retries: int
    last_error: str | None = None
    submitted_at: dt.datetime | None = None
    acknowledgment: FilingAcknowledgment | None = None
    created_at: dt.datetime = Field(default_factory=lambda: dt.datetime.now(dt.timezone.utc))

    @property
    def payload(self) -> bytes:
        return base64.b64decode(self.payload_b64)


class FilingQueue:
    """Redis storage shape (mirrors app.execution.hitl_queue.HITLQueue):

        {prefix}:filing:{filing_id}  -> FilingSubmissionRecord, JSON
        {prefix}:pending             -> set of filing_ids awaiting submission/retry
    """

    def __init__(self, redis_client: redis.Redis, key_prefix: str) -> None:
        self._redis = redis_client
        self._prefix = key_prefix

    def _filing_key(self, filing_id: str) -> str:
        return f"{self._prefix}:filing:{filing_id}"

    @property
    def _pending_key(self) -> str:
        return f"{self._prefix}:pending"

    async def enqueue(self, record: FilingSubmissionRecord) -> None:
        await self._redis.set(self._filing_key(record.filing_id), record.model_dump_json())
        await self._redis.sadd(self._pending_key, record.filing_id)
        logger.info("Filing %s (%s -> %s via %s) enqueued for submission.", record.filing_id, record.filing_type.value, record.target.value, record.channel.value)

    async def get(self, filing_id: str) -> FilingSubmissionRecord | None:
        raw = await self._redis.get(self._filing_key(filing_id))
        return FilingSubmissionRecord.model_validate_json(raw) if raw else None

    async def list_pending(self) -> list[FilingSubmissionRecord]:
        filing_ids = await self._redis.smembers(self._pending_key)
        records = []
        for filing_id in filing_ids:
            record = await self.get(filing_id if isinstance(filing_id, str) else filing_id.decode())
            if record is not None:
                records.append(record)
        return sorted(records, key=lambda r: r.created_at)

    async def _save(self, record: FilingSubmissionRecord) -> None:
        await self._redis.set(self._filing_key(record.filing_id), record.model_dump_json())

    async def mark_submitting(self, filing_id: str) -> FilingSubmissionRecord:
        record = await self.get(filing_id)
        if record is None:
            raise KeyError(f"No filing '{filing_id}' in the queue.")
        updated = record.model_copy(update={"status": FilingStatus.SUBMITTING, "attempt_count": record.attempt_count + 1})
        await self._save(updated)
        return updated

    async def mark_acknowledged(self, filing_id: str, ack: FilingAcknowledgment) -> FilingSubmissionRecord:
        record = await self.get(filing_id)
        if record is None:
            raise KeyError(f"No filing '{filing_id}' in the queue.")
        updated = record.model_copy(update={
            "status": FilingStatus.ACKNOWLEDGED,
            "submitted_at": dt.datetime.now(dt.timezone.utc),
            "acknowledgment": ack,
            "last_error": None,
        })
        await self._save(updated)
        await self._redis.srem(self._pending_key, filing_id)
        return updated

    async def mark_retry(self, filing_id: str, error: str) -> FilingSubmissionRecord:
        """Attempt failed but the retry budget isn't exhausted -- stays
        in the pending set so the next sweep picks it up again."""
        record = await self.get(filing_id)
        if record is None:
            raise KeyError(f"No filing '{filing_id}' in the queue.")
        updated = record.model_copy(update={"status": FilingStatus.PENDING, "last_error": error})
        await self._save(updated)
        return updated

    async def mark_failed(self, filing_id: str, error: str) -> FilingSubmissionRecord:
        record = await self.get(filing_id)
        if record is None:
            raise KeyError(f"No filing '{filing_id}' in the queue.")
        updated = record.model_copy(update={"status": FilingStatus.FAILED, "last_error": error})
        await self._save(updated)
        await self._redis.srem(self._pending_key, filing_id)
        return updated


class SftpFilingSubmitter:
    """Uploads a filing's payload + detached PKCS#7 signature to a
    configured SFTP destination (each MII/SEBI publishes its own SFTP
    host/path convention). Genuinely tested against a real, in-process
    paramiko SFTP SERVER (tests/test_regulatory_filing.py) -- not a
    mocked transport -- so the actual SSH/SFTP protocol handshake,
    authentication, and file upload are exercised for real.

    Acknowledgment model: many MII SFTP intake gateways have no
    synchronous ack protocol -- they pick up the dropped file
    asynchronously and, on successful intake, drop a receipt file back
    (conventionally `<filename>.ack`) in the same directory. This
    submitter uploads the payload + signature, then polls for that
    receipt file up to `ack_poll_timeout_seconds`, matching that real
    pattern rather than inventing a synchronous protocol the SFTP
    transport itself doesn't provide.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def _connect(self):
        import paramiko  # deferred: only needed when the SFTP channel is actually used

        settings = self._settings
        if not settings.regulatory_filing_sftp_host or not settings.regulatory_filing_sftp_username:
            raise SubmissionError("regulatory_filing_sftp_host/username are not configured.")

        client = paramiko.SSHClient()
        if settings.regulatory_filing_sftp_known_host_key:
            # Parse one OpenSSH known_hosts-format line ("<hostname> <key-type> <base64-key>")
            # and register it explicitly, THEN reject anything that
            # doesn't match -- accepting an unrecognized host key for a
            # regulatory filing destination is a submission-integrity
            # risk (this is a targeted MITM/DNS-spoofing defense, not
            # boilerplate), so this path never silently degrades to
            # AutoAddPolicy once a host key has been configured.
            parts = settings.regulatory_filing_sftp_known_host_key.split()
            if len(parts) < 3:
                raise SubmissionError("regulatory_filing_sftp_known_host_key must be a full 'hostname key-type base64-key' known_hosts line.")
            hostname, key_type, key_b64 = parts[0], parts[1], parts[2]
            key_class = {
                "ssh-rsa": paramiko.RSAKey,
                "ssh-ed25519": paramiko.Ed25519Key,
                "ecdsa-sha2-nistp256": paramiko.ECDSAKey,
            }.get(key_type)
            if key_class is None:
                raise SubmissionError(f"Unsupported host key type in regulatory_filing_sftp_known_host_key: {key_type!r}.")
            host_key = key_class(data=base64.b64decode(key_b64))
            client.get_host_keys().add(hostname, key_type, host_key)
            client.set_missing_host_key_policy(paramiko.RejectPolicy())
        else:
            logger.warning("regulatory_filing_sftp_known_host_key is not configured; using paramiko's AutoAddPolicy for this SFTP connection -- NOT safe for a production regulatory submission.")
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        connect_kwargs: dict = {"hostname": settings.regulatory_filing_sftp_host, "port": settings.regulatory_filing_sftp_port, "username": settings.regulatory_filing_sftp_username}
        if settings.regulatory_filing_sftp_private_key_pem:
            import paramiko as _p

            pkey = _p.RSAKey.from_private_key(io.StringIO(settings.regulatory_filing_sftp_private_key_pem))
            connect_kwargs["pkey"] = pkey
        elif settings.regulatory_filing_sftp_password:
            connect_kwargs["password"] = settings.regulatory_filing_sftp_password
        else:
            raise SubmissionError("Neither regulatory_filing_sftp_private_key_pem nor regulatory_filing_sftp_password is configured.")

        client.connect(**connect_kwargs)
        return client

    def _submit_sync(self, record: FilingSubmissionRecord, ack_poll_timeout_seconds: float, ack_poll_interval_seconds: float) -> FilingAcknowledgment:
        import time

        client = self._connect()
        try:
            sftp = client.open_sftp()
            remote_dir = self._settings.regulatory_filing_sftp_remote_dir.rstrip("/")
            payload_remote_path = f"{remote_dir}/{record.filename}"
            signature_remote_path = f"{payload_remote_path}.p7s"
            ack_remote_path = f"{payload_remote_path}.ack"

            sftp.putfo(io.BytesIO(record.payload), payload_remote_path)
            sftp.putfo(io.BytesIO(base64.b64decode(record.signature.signature_der_b64)), signature_remote_path)
            logger.info("SFTP: uploaded %s (%d bytes) + detached signature to %s:%s.", record.filename, len(record.payload), self._settings.regulatory_filing_sftp_host, remote_dir)

            deadline = time.monotonic() + ack_poll_timeout_seconds
            while time.monotonic() < deadline:
                try:
                    sftp.stat(ack_remote_path)
                    with sftp.open(ack_remote_path, "r") as f:
                        ack_content = f.read().decode("utf-8", errors="replace").strip()
                    return FilingAcknowledgment(acknowledgment_reference=ack_content or ack_remote_path, raw_detail=f"receipt file {ack_remote_path}")
                except IOError:
                    time.sleep(ack_poll_interval_seconds)

            raise SubmissionError(f"No acknowledgment receipt ({ack_remote_path}) appeared within {ack_poll_timeout_seconds}s of upload.")
        finally:
            client.close()

    async def submit(self, record: FilingSubmissionRecord, *, ack_poll_timeout_seconds: float = 30.0, ack_poll_interval_seconds: float = 1.0) -> FilingAcknowledgment:
        import asyncio

        return await asyncio.to_thread(self._submit_sync, record, ack_poll_timeout_seconds, ack_poll_interval_seconds)


class PortalApiFilingSubmitter:
    """Submits via a SEBI/MII regulatory portal REST API instead of
    SFTP, for destinations that publish one. Tested against
    `httpx.MockTransport` (this codebase's established convention --
    see tests/test_opa_execution.py, tests/test_healing.py) rather than
    a live portal, since no such sandboxed SEBI portal is reachable
    from here."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def submit(self, record: FilingSubmissionRecord) -> FilingAcknowledgment:
        settings = self._settings
        if not settings.regulatory_filing_portal_api_base_url:
            raise SubmissionError("regulatory_filing_portal_api_base_url is not configured.")

        url = f"{settings.regulatory_filing_portal_api_base_url.rstrip('/')}/v1/filings"
        files = {
            "payload": (record.filename, record.payload, record.content_type),
            "signature": (f"{record.filename}.p7s", base64.b64decode(record.signature.signature_der_b64), "application/pkcs7-signature"),
        }
        data = {
            "filing_id": record.filing_id,
            "filing_type": record.filing_type.value,
            "target": record.target.value,
            "signer_certificate_pem": record.signature.signer_certificate_pem,
        }
        headers = {"Authorization": f"Bearer {settings.regulatory_filing_portal_api_key}"} if settings.regulatory_filing_portal_api_key else {}

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(url, data=data, files=files, headers=headers)
        except httpx.HTTPError as exc:
            raise SubmissionError(f"Portal API request failed: {exc}") from exc

        if response.status_code >= 300:
            raise SubmissionError(f"Portal API rejected filing {record.filing_id}: {response.status_code} {response.text}")

        body = response.json()
        return FilingAcknowledgment(
            acknowledgment_reference=body["acknowledgment_reference"],
            acknowledged_at=dt.datetime.fromisoformat(body["acknowledged_at"]) if "acknowledged_at" in body else dt.datetime.now(dt.timezone.utc),
            raw_detail=response.text,
        )


def get_submitter(record: FilingSubmissionRecord, settings: Settings):
    if record.channel == SubmissionChannel.SFTP:
        return SftpFilingSubmitter(settings)
    if record.channel == SubmissionChannel.PORTAL_API:
        return PortalApiFilingSubmitter(settings)
    raise ValueError(f"Unknown submission channel: {record.channel!r}")
