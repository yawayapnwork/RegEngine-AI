"""Redis-backed persistence for breach events -- mirrors
app.execution.hitl_queue.HITLQueue's shape (a hash-per-event plus a
pending-set index) for the same reason: low-latency, operationally
simple state shared across every FastAPI worker and Celery worker
without a Postgres round-trip on the hot path.
"""
from __future__ import annotations

import datetime as dt

import redis.asyncio as redis

from app.incident.models import AckStatus, BreachEvent


class BreachEventStore:
    def __init__(self, redis_client: redis.Redis, key_prefix: str) -> None:
        self._redis = redis_client
        self._prefix = key_prefix

    def _event_key(self, event_id: str) -> str:
        return f"{self._prefix}:event:{event_id}"

    @property
    def pending_ack_set_key(self) -> str:
        """Sorted set: member=event_id, score=creation unix timestamp --
        lets the safety-net sweep (app.incident.tasks.sweep_overdue_escalations_task)
        cheaply scan only unacknowledged events instead of every event
        ever created."""
        return f"{self._prefix}:pending_ack"

    @property
    def recent_events_list_key(self) -> str:
        """Capped list of every event (any severity) in reverse-chron
        order -- backs the dashboard's initial-load REST endpoint
        (GET /v1/incidents) so a client connecting to the WebSocket after
        the fact still sees recent history, not just events from that
        point forward."""
        return f"{self._prefix}:recent"

    async def save(self, event: BreachEvent, recent_cap: int = 500) -> None:
        await self._redis.set(self._event_key(event.event_id), event.model_dump_json())
        await self._redis.lpush(self.recent_events_list_key, event.event_id)
        await self._redis.ltrim(self.recent_events_list_key, 0, recent_cap - 1)
        if event.requires_acknowledgment and event.ack_status == AckStatus.PENDING:
            await self._redis.zadd(self.pending_ack_set_key, {event.event_id: event.created_at.timestamp()})

    async def get(self, event_id: str) -> BreachEvent | None:
        raw = await self._redis.get(self._event_key(event_id))
        return BreachEvent.model_validate_json(raw) if raw else None

    async def list_recent(self, limit: int = 100) -> list[BreachEvent]:
        event_ids = await self._redis.lrange(self.recent_events_list_key, 0, limit - 1)
        events = []
        for raw_id in event_ids:
            event_id = raw_id if isinstance(raw_id, str) else raw_id.decode()
            event = await self.get(event_id)
            if event is not None:
                events.append(event)
        return events

    async def list_pending_ack(self, older_than_seconds: int | None = None) -> list[BreachEvent]:
        """Returns unacknowledged events, optionally filtered to ones
        created more than `older_than_seconds` ago -- used by the sweep
        task to find events whose scheduled escalation may have been
        lost (a worker restart mid-countdown, a Celery broker outage)."""
        if older_than_seconds is not None:
            cutoff = dt.datetime.now(dt.timezone.utc).timestamp() - older_than_seconds
            event_ids = await self._redis.zrangebyscore(self.pending_ack_set_key, min="-inf", max=cutoff)
        else:
            event_ids = await self._redis.zrange(self.pending_ack_set_key, 0, -1)

        events = []
        for raw_id in event_ids:
            event_id = raw_id if isinstance(raw_id, str) else raw_id.decode()
            event = await self.get(event_id)
            if event is not None and event.ack_status == AckStatus.PENDING:
                events.append(event)
        return events

    async def acknowledge(self, event_id: str, acknowledged_by: str) -> BreachEvent | None:
        event = await self.get(event_id)
        if event is None:
            return None
        event.ack_status = AckStatus.ACKNOWLEDGED
        event.acknowledged_by = acknowledged_by
        event.acknowledged_at = dt.datetime.now(dt.timezone.utc)
        await self._redis.set(self._event_key(event.event_id), event.model_dump_json())
        await self._redis.zrem(self.pending_ack_set_key, event_id)
        return event

    async def update_escalation_stage(self, event_id: str, stage_index: int) -> None:
        event = await self.get(event_id)
        if event is None:
            return
        event.escalation_stage = stage_index
        await self._redis.set(self._event_key(event.event_id), event.model_dump_json())
