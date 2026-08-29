"""Requirement 3's "security vault": a durable, queryable record of
every red-team attack scenario run against this pipeline and how the
defense middleware responded -- Redis-backed, mirroring
app.execution.hitl_queue.HITLQueue's established storage shape (a JSON
blob per record plus a sorted-set index), consistent with every other
security-relevant event log in this codebase (app.incident.store.BreachEventStore,
app.resilience.dead_letter_queue.DeadLetterQueue).
"""
from __future__ import annotations

import datetime as dt
import uuid
from enum import Enum

import redis.asyncio as redis
from pydantic import BaseModel, Field


class AttackOutcome(str, Enum):
    RESISTED = "resisted"          # defense middleware caught/neutralized the injection
    NOT_RESISTED = "not_resisted"  # the injection was NOT caught -- an escaped defect
    ERROR = "error"                # the scenario itself failed to run (not a defense verdict either way)


class RedTeamTelemetryRecord(BaseModel):
    record_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    scenario_name: str
    technique: str  # app.redteam.attack_generator.InjectionTechnique value
    outcome: AttackOutcome
    detected_patterns: list[str] = Field(default_factory=list)
    detail: str | None = None
    ran_at: dt.datetime = Field(default_factory=lambda: dt.datetime.now(dt.timezone.utc))


class RedTeamTelemetryVault:
    """Storage shape:

        {prefix}:record:{record_id}  -> RedTeamTelemetryRecord, JSON
        {prefix}:all                 -> sorted set, score=ran_at epoch, all record_ids
        {prefix}:technique:{name}    -> sorted set, same score, record_ids for that technique
        {prefix}:outcome:{outcome}   -> sorted set, same score, record_ids for that outcome
    """

    def __init__(self, redis_client: redis.Redis, key_prefix: str) -> None:
        self._redis = redis_client
        self._prefix = key_prefix

    def _record_key(self, record_id: str) -> str:
        return f"{self._prefix}:record:{record_id}"

    def _technique_key(self, technique: str) -> str:
        return f"{self._prefix}:technique:{technique}"

    def _outcome_key(self, outcome: AttackOutcome) -> str:
        return f"{self._prefix}:outcome:{outcome.value}"

    @property
    def _all_key(self) -> str:
        return f"{self._prefix}:all"

    async def record(self, record: RedTeamTelemetryRecord) -> None:
        score = record.ran_at.timestamp()
        async with self._redis.pipeline() as pipe:
            pipe.set(self._record_key(record.record_id), record.model_dump_json())
            pipe.zadd(self._all_key, {record.record_id: score})
            pipe.zadd(self._technique_key(record.technique), {record.record_id: score})
            pipe.zadd(self._outcome_key(record.outcome), {record.record_id: score})
            await pipe.execute()

    async def get(self, record_id: str) -> RedTeamTelemetryRecord | None:
        raw = await self._redis.get(self._record_key(record_id))
        return RedTeamTelemetryRecord.model_validate_json(raw) if raw else None

    async def list_all(self, *, technique: str | None = None, outcome: AttackOutcome | None = None) -> list[RedTeamTelemetryRecord]:
        if technique is not None:
            index_key = self._technique_key(technique)
        elif outcome is not None:
            index_key = self._outcome_key(outcome)
        else:
            index_key = self._all_key

        record_ids = await self._redis.zrevrange(index_key, 0, -1)
        records = []
        for record_id in record_ids:
            record = await self.get(record_id if isinstance(record_id, str) else record_id.decode())
            if record is not None:
                records.append(record)
        return records

    async def resistance_rate(self) -> float:
        """Requirement 3's headline metric: fraction of all logged
        scenarios where the defense middleware resisted the attack.
        Scenarios that errored out (AttackOutcome.ERROR) are excluded
        from the denominator -- a scenario that never actually ran is
        neither a resisted nor an unresisted attack, and folding it
        into either count would misrepresent the real resistance rate."""
        resisted = await self._redis.zcard(self._outcome_key(AttackOutcome.RESISTED))
        not_resisted = await self._redis.zcard(self._outcome_key(AttackOutcome.NOT_RESISTED))
        total = resisted + not_resisted
        return (resisted / total) if total else 1.0
