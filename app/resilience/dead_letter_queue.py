"""Redis-backed Dead-Letter Queue: where an item lands once its originating
Celery task has either exhausted its retries on a transient failure, or hit
a failure `app.resilience.retry_policy.is_transient` says isn't worth
retrying at all (see app.resilience.exceptions for that distinction).

Storage shape (mirrors app.execution.hitl_queue's existing Redis pattern
in this codebase -- a per-item JSON blob plus sorted-set indexes for
listing, not a single giant list/hash):

    {prefix}:entry:{entry_id}        -> DLQEntry, JSON, one key per item
    {prefix}:all                     -> sorted set, score=last_failed_at epoch, all entry_ids
    {prefix}:category:{category}     -> sorted set, same score, entry_ids in that FailureCategory
    {prefix}:status:{status}         -> sorted set, same score, entry_ids in that DLQStatus

An item is a member of exactly one category set (fixed at creation) and
exactly one status set at a time (moved between sets as its status
changes) -- so `list(category=X)` or `list(status=Y)` is a single ZREVRANGE,
no full scan. Filtering on BOTH dimensions at once intersects in Python
after fetching the more selective single-dimension set; the DLQ is, by
design, a low-volume collection of failures a human looks at, not a
high-throughput data structure, so this is a deliberate simplicity/
efficiency tradeoff, not an oversight.
"""
from __future__ import annotations

import datetime as dt
import logging
import traceback as tb_module

import redis.asyncio as redis

from app.resilience.models import DLQEntry, DLQStats, DLQStatus, FailureCategory

logger = logging.getLogger(__name__)


class DLQEntryNotFoundError(KeyError):
    pass


class DLQInvalidTransitionError(ValueError):
    """Raised when an action is attempted on an entry whose current status
    doesn't allow it (e.g. re-requeueing something already RESOLVED)."""


class DeadLetterQueue:
    def __init__(self, redis_client: redis.Redis, key_prefix: str) -> None:
        self._redis = redis_client
        self._prefix = key_prefix

    def _entry_key(self, entry_id: str) -> str:
        return f"{self._prefix}:entry:{entry_id}"

    def _category_key(self, category: FailureCategory) -> str:
        return f"{self._prefix}:category:{category.value}"

    def _status_key(self, status: DLQStatus) -> str:
        return f"{self._prefix}:status:{status.value}"

    @property
    def _all_key(self) -> str:
        return f"{self._prefix}:all"

    @staticmethod
    def _score(moment: dt.datetime) -> float:
        return moment.timestamp()

    async def send(
        self,
        *,
        category: FailureCategory,
        task_name: str,
        payload: dict,
        exc: BaseException,
        original_task_id: str | None = None,
        attempt_count: int = 1,
    ) -> DLQEntry:
        """Parks one failed item. Called from a Celery task's exception
        handler once it has decided (via `app.resilience.retry_policy.is_transient`
        and/or its own `max_retries`) that this failure will not be retried
        again."""
        now = dt.datetime.now(dt.timezone.utc)
        entry = DLQEntry(
            category=category,
            task_name=task_name,
            original_task_id=original_task_id,
            payload=payload,
            error_type=type(exc).__name__,
            error_message=str(exc),
            traceback="".join(tb_module.format_exception(type(exc), exc, exc.__traceback__)),
            attempt_count=attempt_count,
            first_failed_at=now,
            last_failed_at=now,
            status=DLQStatus.PENDING,
        )
        await self._persist_new(entry)
        logger.error(
            "DLQ: routed '%s' (task=%s, category=%s, error=%s: %s)",
            entry.entry_id, task_name, category.value, entry.error_type, entry.error_message,
        )
        return entry

    async def _persist_new(self, entry: DLQEntry) -> None:
        score = self._score(entry.last_failed_at)
        async with self._redis.pipeline() as pipe:
            pipe.set(self._entry_key(entry.entry_id), entry.model_dump_json())
            pipe.zadd(self._all_key, {entry.entry_id: score})
            pipe.zadd(self._category_key(entry.category), {entry.entry_id: score})
            pipe.zadd(self._status_key(entry.status), {entry.entry_id: score})
            await pipe.execute()

    async def get(self, entry_id: str) -> DLQEntry:
        raw = await self._redis.get(self._entry_key(entry_id))
        if raw is None:
            raise DLQEntryNotFoundError(entry_id)
        return DLQEntry.model_validate_json(raw)

    async def list(
        self,
        *,
        category: FailureCategory | None = None,
        status: DLQStatus | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[DLQEntry]:
        if category is not None:
            index_key = self._category_key(category)
        elif status is not None:
            index_key = self._status_key(status)
        else:
            index_key = self._all_key

        # Newest-failed first: a compliance engineer triaging the queue
        # almost always wants to see what just broke, not what's been
        # sitting there longest.
        ids = await self._redis.zrevrange(index_key, 0, -1)

        entries: list[DLQEntry] = []
        for entry_id in ids:
            try:
                entry = await self.get(entry_id)
            except DLQEntryNotFoundError:
                continue  # index/entry drift (e.g. a key expired) -- skip rather than fail the whole listing
            if category is not None and status is not None and entry.status != status:
                continue  # secondary-dimension filter, see module docstring
            entries.append(entry)
            if len(entries) >= offset + limit:
                break

        return entries[offset : offset + limit]

    async def count(self, *, category: FailureCategory | None = None, status: DLQStatus | None = None) -> int:
        if category is not None:
            return await self._redis.zcard(self._category_key(category))
        if status is not None:
            return await self._redis.zcard(self._status_key(status))
        return await self._redis.zcard(self._all_key)

    async def stats(self) -> DLQStats:
        total = await self.count()
        by_category = {c.value: await self.count(category=c) for c in FailureCategory}
        by_status = {s.value: await self.count(status=s) for s in DLQStatus}
        return DLQStats(total=total, by_category=by_category, by_status=by_status)

    async def update_payload(self, entry_id: str, new_payload: dict) -> DLQEntry:
        """A compliance engineer edits the failing parameters (e.g. fixes
        a malformed URL, adjusts an extraction override) before requeueing.
        Does not itself requeue -- see `mark_requeued`, called separately
        once the actual Celery re-dispatch succeeds."""
        entry = await self.get(entry_id)
        if entry.status not in (DLQStatus.PENDING, DLQStatus.REQUEUED):
            raise DLQInvalidTransitionError(f"Cannot edit entry '{entry_id}' in terminal status '{entry.status.value}'.")
        entry = entry.model_copy(update={"payload": new_payload})
        await self._redis.set(self._entry_key(entry_id), entry.model_dump_json())
        return entry

    async def mark_requeued(self, entry_id: str, *, requeued_by: str, requeued_task_id: str) -> DLQEntry:
        entry = await self.get(entry_id)
        if entry.status not in (DLQStatus.PENDING, DLQStatus.REQUEUED):
            raise DLQInvalidTransitionError(f"Cannot requeue entry '{entry_id}' in terminal status '{entry.status.value}'.")
        updated = entry.model_copy(update={
            "status": DLQStatus.REQUEUED,
            "requeued_at": dt.datetime.now(dt.timezone.utc),
            "requeued_by": requeued_by,
            "requeued_task_id": requeued_task_id,
        })
        await self._move_status(entry.status, updated)
        return updated

    async def mark_resolved(self, entry_id: str, *, resolved_by: str, notes: str | None = None) -> DLQEntry:
        entry = await self.get(entry_id)
        updated = entry.model_copy(update={
            "status": DLQStatus.RESOLVED,
            "resolved_by": resolved_by,
            "resolution_notes": notes,
            "resolved_at": dt.datetime.now(dt.timezone.utc),
        })
        await self._move_status(entry.status, updated)
        return updated

    async def discard(self, entry_id: str, *, discarded_by: str, notes: str | None = None) -> DLQEntry:
        entry = await self.get(entry_id)
        if entry.status in (DLQStatus.RESOLVED, DLQStatus.DISCARDED):
            raise DLQInvalidTransitionError(f"Entry '{entry_id}' is already terminal ('{entry.status.value}').")
        updated = entry.model_copy(update={
            "status": DLQStatus.DISCARDED,
            "resolved_by": discarded_by,
            "resolution_notes": notes,
            "resolved_at": dt.datetime.now(dt.timezone.utc),
        })
        await self._move_status(entry.status, updated)
        return updated

    async def _move_status(self, old_status: DLQStatus, entry: DLQEntry) -> None:
        async with self._redis.pipeline() as pipe:
            pipe.set(self._entry_key(entry.entry_id), entry.model_dump_json())
            pipe.zrem(self._status_key(old_status), entry.entry_id)
            pipe.zadd(self._status_key(entry.status), {entry.entry_id: self._score(entry.last_failed_at)})
            await pipe.execute()
