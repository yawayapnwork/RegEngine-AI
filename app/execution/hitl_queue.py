"""Execution-time HITL fallback: Redis-backed queue of ambiguous transaction
decisions awaiting human sign-off.

This is deliberately non-blocking. The synchronous `/evaluate` endpoint
must still "return instantly" per the requirement even when a decision is
ambiguous — so an ambiguous transaction gets an immediate `FLAGGED`
response plus a `hitl_case_id`, and the human's eventual decision is
delivered later via `app.execution.tasks.dispatch_hitl_resolution_webhook`
to `TransactionPayload.callback_url`. The broker/OMS is expected to either
poll `GET /v1/execution/hitl/cases/{id}` or receive that webhook.
"""
from __future__ import annotations

import datetime as dt
import uuid

import redis.asyncio as redis

from app.execution.models import HITLCase, HITLStatus, PolicyOutcome, TransactionPayload


class HITLQueue:
    def __init__(self, redis_client: redis.Redis, key_prefix: str) -> None:
        self._redis = redis_client
        self._prefix = key_prefix

    def _case_key(self, case_id: str) -> str:
        return f"{self._prefix}:case:{case_id}"

    @property
    def _pending_set_key(self) -> str:
        return f"{self._prefix}:pending"

    async def enqueue(
        self,
        transaction: TransactionPayload,
        reason: str,
        matched_policies: list[PolicyOutcome],
    ) -> HITLCase:
        case = HITLCase(
            case_id=str(uuid.uuid4()),
            transaction=transaction,
            reason=reason,
            matched_policies=matched_policies,
        )
        await self._redis.set(self._case_key(case.case_id), case.model_dump_json())
        await self._redis.sadd(self._pending_set_key, case.case_id)
        return case

    async def get(self, case_id: str) -> HITLCase | None:
        raw = await self._redis.get(self._case_key(case_id))
        return HITLCase.model_validate_json(raw) if raw else None

    async def list_pending(self) -> list[HITLCase]:
        case_ids = await self._redis.smembers(self._pending_set_key)
        cases = []
        for case_id in case_ids:
            case = await self.get(case_id if isinstance(case_id, str) else case_id.decode())
            if case is not None:
                cases.append(case)
        return sorted(cases, key=lambda c: c.created_at)

    async def resolve(self, case_id: str, status: HITLStatus, resolved_by: str, notes: str | None) -> HITLCase:
        case = await self.get(case_id)
        if case is None:
            raise KeyError(f"No HITL case with id '{case_id}'")
        if case.status != HITLStatus.PENDING:
            raise ValueError(f"HITL case '{case_id}' was already resolved as '{case.status.value}'")

        case.status = status
        case.resolved_by = resolved_by
        case.resolution_notes = notes
        case.resolved_at = dt.datetime.utcnow()

        await self._redis.set(self._case_key(case_id), case.model_dump_json())
        await self._redis.srem(self._pending_set_key, case_id)
        return case
