"""Requirement 3's tracking store: a `GrievanceRecord`'s full lifecycle
-- drafted, submitted, and (Requirement 3) polled/updated status --
Redis-backed, mirroring `app.regulatory_filing.submission.FilingQueue`'s
established shape in this codebase (itself mirroring
`app.execution.hitl_queue.HITLQueue`) for "external-submission tracking
state," rather than a new Postgres table.
"""
from __future__ import annotations

import datetime as dt
import uuid
from enum import Enum

import redis.asyncio as redis
from pydantic import BaseModel, Field

from app.grievance_escalation.schemas import GrievanceRespondent, GrievanceSubmissionRequest, ScoresGrievanceStatus


class GrievanceStatus(str, Enum):
    DRAFTED = "drafted"              # evidence assembled, held for compliance-officer confirmation (settings.grievance_escalation_auto_submit_enabled == False)
    PENDING_SUBMISSION = "pending_submission"  # confirmed (or auto-submit enabled) -- queued for the next submit attempt
    SUBMITTING = "submitting"
    SUBMITTED = "submitted"          # SCORES acknowledged receipt; awaiting resolution
    RESOLVED = "resolved"
    REJECTED = "rejected"
    SUBMISSION_FAILED = "submission_failed"  # retry budget exhausted


class GrievanceRecord(BaseModel):
    grievance_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    request: GrievanceSubmissionRequest
    status: GrievanceStatus = GrievanceStatus.DRAFTED
    attempt_count: int = 0
    max_retries: int
    last_error: str | None = None

    scores_reference_number: str | None = None
    scores_status: ScoresGrievanceStatus | None = None
    resolution_summary: str | None = None

    filed_at: dt.datetime = Field(default_factory=lambda: dt.datetime.now(dt.timezone.utc))
    response_due_at: dt.datetime
    submitted_at: dt.datetime | None = None
    last_polled_at: dt.datetime | None = None
    resolved_at: dt.datetime | None = None

    @property
    def respondent(self) -> GrievanceRespondent:
        return self.request.respondent

    @property
    def is_open(self) -> bool:
        return self.status in (GrievanceStatus.SUBMITTED,)

    @property
    def is_overdue(self) -> bool:
        return self.is_open and dt.datetime.now(dt.timezone.utc) > self.response_due_at


class GrievanceQueue:
    """Redis storage shape (mirrors FilingQueue exactly):
        {prefix}:grievance:{grievance_id} -> GrievanceRecord, JSON
        {prefix}:pending_submission       -> set of grievance_ids awaiting submission/retry
        {prefix}:open                     -> set of grievance_ids SUBMITTED but not yet RESOLVED/REJECTED (Requirement 3's polling target)
    """

    def __init__(self, redis_client: redis.Redis, key_prefix: str) -> None:
        self._redis = redis_client
        self._prefix = key_prefix

    def _key(self, grievance_id: str) -> str:
        return f"{self._prefix}:grievance:{grievance_id}"

    @property
    def _pending_submission_key(self) -> str:
        return f"{self._prefix}:pending_submission"

    @property
    def _open_key(self) -> str:
        return f"{self._prefix}:open"

    async def create_draft(self, record: GrievanceRecord) -> GrievanceRecord:
        await self._save(record)
        return record

    async def get(self, grievance_id: str) -> GrievanceRecord | None:
        raw = await self._redis.get(self._key(grievance_id))
        return GrievanceRecord.model_validate_json(raw) if raw else None

    async def list_pending_submission(self) -> list[GrievanceRecord]:
        return await self._list_from_set(self._pending_submission_key)

    async def list_open(self) -> list[GrievanceRecord]:
        return await self._list_from_set(self._open_key)

    async def _list_from_set(self, set_key: str) -> list[GrievanceRecord]:
        ids = await self._redis.smembers(set_key)
        records = []
        for grievance_id in ids:
            record = await self.get(grievance_id if isinstance(grievance_id, str) else grievance_id.decode())
            if record is not None:
                records.append(record)
        return sorted(records, key=lambda r: r.filed_at)

    async def _save(self, record: GrievanceRecord) -> None:
        await self._redis.set(self._key(record.grievance_id), record.model_dump_json())

    async def confirm_for_submission(self, grievance_id: str) -> GrievanceRecord:
        """Compliance officer confirms a DRAFTED grievance (or the
        auto-submit path calls this immediately after drafting) -- moves
        it into the submission queue for the next Celery sweep."""
        record = await self._require(grievance_id)
        if record.status != GrievanceStatus.DRAFTED:
            raise ValueError(f"Grievance '{grievance_id}' is not DRAFTED (status={record.status.value}); cannot confirm.")
        updated = record.model_copy(update={"status": GrievanceStatus.PENDING_SUBMISSION})
        await self._save(updated)
        await self._redis.sadd(self._pending_submission_key, grievance_id)
        return updated

    async def mark_submitting(self, grievance_id: str) -> GrievanceRecord:
        record = await self._require(grievance_id)
        updated = record.model_copy(update={"status": GrievanceStatus.SUBMITTING, "attempt_count": record.attempt_count + 1})
        await self._save(updated)
        return updated

    async def mark_submitted(self, grievance_id: str, scores_reference_number: str) -> GrievanceRecord:
        record = await self._require(grievance_id)
        updated = record.model_copy(update={
            "status": GrievanceStatus.SUBMITTED, "scores_reference_number": scores_reference_number,
            "scores_status": ScoresGrievanceStatus.SUBMITTED, "submitted_at": dt.datetime.now(dt.timezone.utc), "last_error": None,
        })
        await self._save(updated)
        await self._redis.srem(self._pending_submission_key, grievance_id)
        await self._redis.sadd(self._open_key, grievance_id)
        return updated

    async def mark_retry(self, grievance_id: str, error: str) -> GrievanceRecord:
        record = await self._require(grievance_id)
        updated = record.model_copy(update={"status": GrievanceStatus.PENDING_SUBMISSION, "last_error": error})
        await self._save(updated)
        return updated

    async def mark_submission_failed(self, grievance_id: str, error: str) -> GrievanceRecord:
        record = await self._require(grievance_id)
        updated = record.model_copy(update={"status": GrievanceStatus.SUBMISSION_FAILED, "last_error": error})
        await self._save(updated)
        await self._redis.srem(self._pending_submission_key, grievance_id)
        return updated

    async def update_status(self, grievance_id: str, scores_status: ScoresGrievanceStatus, resolution_summary: str | None) -> GrievanceRecord:
        """Requirement 3: applies a polled SCORES status update. Only
        RESOLVED/REJECTED remove the grievance from the open-for-polling
        set -- every other status keeps it open for the next poll."""
        record = await self._require(grievance_id)
        updates: dict = {"scores_status": scores_status, "resolution_summary": resolution_summary, "last_polled_at": dt.datetime.now(dt.timezone.utc)}
        if scores_status == ScoresGrievanceStatus.RESOLVED:
            updates["status"] = GrievanceStatus.RESOLVED
            updates["resolved_at"] = dt.datetime.now(dt.timezone.utc)
        elif scores_status == ScoresGrievanceStatus.REJECTED:
            updates["status"] = GrievanceStatus.REJECTED
            updates["resolved_at"] = dt.datetime.now(dt.timezone.utc)
        updated = record.model_copy(update=updates)
        await self._save(updated)
        if updated.status in (GrievanceStatus.RESOLVED, GrievanceStatus.REJECTED):
            await self._redis.srem(self._open_key, grievance_id)
        return updated

    async def _require(self, grievance_id: str) -> GrievanceRecord:
        record = await self.get(grievance_id)
        if record is None:
            raise KeyError(f"No grievance '{grievance_id}' in the queue.")
        return record
