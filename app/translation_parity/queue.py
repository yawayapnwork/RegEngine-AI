"""Requirement 3's compliance-officer review queue: Redis-backed,
mirroring app.execution.hitl_queue.HITLQueue's exact shape.

Redis-backed rather than a new Postgres table deliberately: this queue
holds ingestion-time, pre-compilation findings about a specific
circular upload, the same "transient item awaiting a human's next
action" nature as app.execution.models.HITLCase (an ambiguous
transaction) -- not the durable, FK-linked, workflow-audited nature of
app.db.models.HITLReview (a flagged COMPILED policy, tied to a
`clause_id`/`compiled_rule_id` that may not even exist yet for a
circular still failing its pre-compilation parity check). Reusing
HITLReview's table would additionally require extending its
`reason_code` CHECK constraint and relaxing its NOT NULL `clause_id` FK
via a migration for no benefit this queue doesn't already provide.
"""
from __future__ import annotations

import datetime as dt
import uuid

import redis.asyncio as redis

from app.translation_parity.models import DiscrepancyCase, DiscrepancyReviewStatus, TranslationParityReport


class TranslationDiscrepancyQueue:
    def __init__(self, redis_client: redis.Redis, key_prefix: str) -> None:
        self._redis = redis_client
        self._prefix = key_prefix

    def _case_key(self, case_id: str) -> str:
        return f"{self._prefix}:case:{case_id}"

    @property
    def pending_set_key(self) -> str:
        return f"{self._prefix}:pending"

    async def enqueue(self, report: TranslationParityReport, diff_html_by_clause_pair: dict[str, str]) -> DiscrepancyCase:
        case = DiscrepancyCase(case_id=str(uuid.uuid4()), report=report, diff_html_by_clause_pair=diff_html_by_clause_pair)
        await self._redis.set(self._case_key(case.case_id), case.model_dump_json())
        await self._redis.sadd(self.pending_set_key, case.case_id)
        return case

    async def get(self, case_id: str) -> DiscrepancyCase | None:
        raw = await self._redis.get(self._case_key(case_id))
        return DiscrepancyCase.model_validate_json(raw) if raw else None

    async def list_pending(self) -> list[DiscrepancyCase]:
        case_ids = await self._redis.smembers(self.pending_set_key)
        cases = []
        for case_id in case_ids:
            case = await self.get(case_id if isinstance(case_id, str) else case_id.decode())
            if case is not None:
                cases.append(case)
        return sorted(cases, key=lambda c: c.created_at)

    async def resolve(self, case_id: str, status: DiscrepancyReviewStatus, resolved_by: str, notes: str | None) -> DiscrepancyCase:
        case = await self.get(case_id)
        if case is None:
            raise KeyError(f"No translation discrepancy case with id '{case_id}'")
        if case.status != DiscrepancyReviewStatus.PENDING:
            raise ValueError(f"Discrepancy case '{case_id}' was already resolved as '{case.status.value}'")

        case.status = status
        case.resolved_by = resolved_by
        case.resolution_notes = notes
        case.resolved_at = dt.datetime.now(dt.timezone.utc)

        await self._redis.set(self._case_key(case_id), case.model_dump_json())
        await self._redis.srem(self.pending_set_key, case_id)
        return case
