"""Redis-backed canary state -- mirrors app.execution.hitl_queue.HITLQueue's
shape (one JSON value per item, plus a set of active ids), the
established convention for transient, ops-workflow state in this
codebase (see also app.negotiation.telemetry, app.translation_parity.queue).

A canary run is operational/transient state, not a permanent compliance
record -- like a live transaction's HITL case, not like a compiled
policy's audit trail -- so this deliberately does not add a new
Postgres table (app.db.models.CompiledRule already records which
version is live; this store only tracks the in-flight A/B comparison
that precedes that decision).
"""
from __future__ import annotations

import datetime as dt

import redis.asyncio as redis

from app.canary.models import CanaryRun, CanaryStatus, ComparisonResult


class CanaryStore:
    def __init__(self, redis_client: redis.Redis, key_prefix: str) -> None:
        self._redis = redis_client
        self._prefix = key_prefix

    def _run_key(self, canary_id: str) -> str:
        return f"{self._prefix}:run:{canary_id}"

    @property
    def active_set_key(self) -> str:
        return f"{self._prefix}:active"

    async def create(self, run: CanaryRun) -> CanaryRun:
        await self._redis.set(self._run_key(run.canary_id), run.model_dump_json())
        await self._redis.sadd(self.active_set_key, run.canary_id)
        return run

    async def get(self, canary_id: str) -> CanaryRun | None:
        raw = await self._redis.get(self._run_key(canary_id))
        return CanaryRun.model_validate_json(raw) if raw else None

    async def list_active(self) -> list[CanaryRun]:
        canary_ids = await self._redis.smembers(self.active_set_key)
        runs = []
        for canary_id in canary_ids:
            run = await self.get(canary_id if isinstance(canary_id, str) else canary_id.decode())
            if run is not None and run.status == CanaryStatus.RUNNING:
                runs.append(run)
        return sorted(runs, key=lambda r: r.started_at)

    async def record_comparison(self, canary_id: str, comparison: ComparisonResult) -> CanaryRun:
        """The real-time parity-analysis write path -- called once per
        shadowed transaction (see app.canary.parity.ParityAnalyzer).
        Read-modify-write on the single JSON value is safe here despite
        not being a Redis transaction: every comparison for a given
        canary_id is recorded from within the SAME fire-and-forget
        asyncio task chain the mirroring engine spawns per transaction,
        and Redis itself serializes each individual command, so the
        actual risk is two truly concurrent writers on the same
        canary_id under heavy parallelism -- acceptable for a
        statistics counter used to make a coarse promote/rollback
        judgment over thousands of samples, not a place where losing an
        occasional increment changes the outcome, unlike, say, the
        audit ledger's hash chain."""
        run = await self.get(canary_id)
        if run is None:
            raise KeyError(f"No canary run with id '{canary_id}'")

        stats = run.stats
        stats.total_compared += 1
        if comparison.diverged:
            stats.diverged += 1
        else:
            stats.matched += 1
        breakdown_key = f"{comparison.production_decision.value}|{comparison.candidate_decision.value}"
        stats.decision_breakdown[breakdown_key] = stats.decision_breakdown.get(breakdown_key, 0) + 1
        stats.production_latency_ms_sum += comparison.production_latency_ms
        stats.candidate_latency_ms_sum += comparison.candidate_latency_ms

        await self._redis.set(self._run_key(canary_id), run.model_dump_json())
        return run

    async def mark_promoted(self, canary_id: str, reason: str) -> CanaryRun:
        return await self._resolve(canary_id, CanaryStatus.PROMOTED, reason)

    async def mark_rolled_back(self, canary_id: str, reason: str) -> CanaryRun:
        return await self._resolve(canary_id, CanaryStatus.ROLLED_BACK, reason)

    async def _resolve(self, canary_id: str, status: CanaryStatus, reason: str) -> CanaryRun:
        run = await self.get(canary_id)
        if run is None:
            raise KeyError(f"No canary run with id '{canary_id}'")
        if run.status != CanaryStatus.RUNNING:
            raise ValueError(f"Canary run '{canary_id}' was already resolved as '{run.status.value}'")

        run.status = status
        run.resolved_at = dt.datetime.now(dt.timezone.utc)
        run.resolution_reason = reason

        await self._redis.set(self._run_key(canary_id), run.model_dump_json())
        await self._redis.srem(self.active_set_key, canary_id)
        return run
