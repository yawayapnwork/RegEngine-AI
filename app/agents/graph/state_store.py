"""Persistent execution-state recording (Requirement 2) -- mirrors
app.execution.hitl_queue.HITLQueue / app.incident.store.BreachEventStore's
shape (a hash-per-run plus an append-only list of per-node records), the
established pattern in this codebase for "durable, cross-process state a
Redis-backed worker needs," since a LangGraph run here executes inside a
Celery-driven asyncio.to_thread call, not a long-lived in-process object
another request could read state from directly.

Two Redis keys per run:
  <prefix>:run:<run_id>        A hash of the run's current top-level state
                                (route_taken, fallback_count, final
                                confidence, cumulative token_usage) --
                                cheap to read for a status check.
  <prefix>:nodes:<run_id>      An ordered list of one JSON record per node
                                execution (node_name, started_at,
                                duration_ms, confidence_score,
                                token_usage, error) -- the full execution
                                trace, for debugging why a given clause
                                took the route it did.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
from typing import Any

import redis.asyncio as redis

logger = logging.getLogger(__name__)


class GraphExecutionStateStore:
    def __init__(self, redis_client: redis.Redis, key_prefix: str, ttl_seconds: int) -> None:
        self._redis = redis_client
        self._prefix = key_prefix
        self._ttl = ttl_seconds

    def _run_key(self, run_id: str) -> str:
        return f"{self._prefix}:run:{run_id}"

    def _nodes_key(self, run_id: str) -> str:
        return f"{self._prefix}:nodes:{run_id}"

    async def record_node_execution(
        self,
        run_id: str,
        node_name: str,
        *,
        route_taken: str | None = None,
        confidence_score: float | None = None,
        token_usage: dict[str, int] | None = None,
        duration_ms: float | None = None,
        error: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        """Called at the end of EVERY node function (app.agents.graph.nodes)
        -- the single choke point every node's execution passes through,
        so a node can never run without its state being durably recorded,
        mirroring the "one instrumentation point, not scattered call
        sites" principle app.observability.metrics's module docstring
        establishes for Prometheus metrics."""
        record = {
            "node_name": node_name,
            "recorded_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "route_taken": route_taken,
            "confidence_score": confidence_score,
            "token_usage": token_usage or {},
            "duration_ms": duration_ms,
            "error": error,
            "extra": extra or {},
        }
        try:
            pipe = self._redis.pipeline()
            pipe.rpush(self._nodes_key(run_id), json.dumps(record))
            pipe.expire(self._nodes_key(run_id), self._ttl)

            run_updates: dict[str, str] = {"last_node": node_name, "updated_at": record["recorded_at"]}
            if route_taken is not None:
                run_updates["route_taken"] = route_taken
            if confidence_score is not None:
                run_updates["last_confidence_score"] = str(confidence_score)
            pipe.hset(self._run_key(run_id), mapping=run_updates)
            pipe.expire(self._run_key(run_id), self._ttl)
            await pipe.execute()
        except Exception:  # noqa: BLE001 - a state-recording failure must never abort the actual extraction/audit work in progress
            logger.exception("Failed to record graph node execution: run_id=%s node=%s", run_id, node_name)

    async def get_run_summary(self, run_id: str) -> dict[str, str] | None:
        raw = await self._redis.hgetall(self._run_key(run_id))
        return raw or None

    async def get_node_history(self, run_id: str) -> list[dict[str, Any]]:
        raw_records = await self._redis.lrange(self._nodes_key(run_id), 0, -1)
        return [json.loads(r) for r in raw_records]
