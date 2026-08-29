"""Cost-tracking middleware for every LLM invocation (and cache hit) in
the compliance pipeline.

`track_llm_call` is the single choke point every call site -- the
semantic cache's own hit path, the cheap-tier call, the frontier-tier
call, and an escalation retry -- goes through, mirroring the "one
instrumentation point, not scattered call sites" principle already used
for the four required Prometheus metrics (see app.observability.metrics's
module docstring). It:

  1. Records a durable row in `llm_usage_events` (Postgres) -- the source
     of truth `app.llm_ops.aggregator.CostAggregator` queries for the
     per-tenant dashboard.
  2. Increments the platform-wide Prometheus counters (cache hit ratio,
     routing distribution, cost, tokens) for alerting/Grafana.

A cache hit is recorded as a genuine event with model_tier=CACHE_HIT and
zero tokens/cost -- not skipped -- so "total requests" and "cache hit
ratio" can both be computed as simple aggregates over one table.
"""
from __future__ import annotations

import logging
import time
import uuid
from contextlib import asynccontextmanager
from typing import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncEngine

from app.llm_ops.models import CacheLayer, LLMUsageEvent, ModelTier, llm_usage_events
from app.llm_ops.pricing import estimate_cost_usd
from app.observability.metrics import LLM_CACHE_LOOKUP_TOTAL, LLM_COST_USD_TOTAL, LLM_ROUTING_DECISION_TOTAL, LLM_TOKENS_TOTAL

logger = logging.getLogger(__name__)


class LLMCallTracker:
    """Mutable accumulator handed to the caller inside `track_llm_call`'s
    `async with` block, so the caller can fill in tokens/model details
    after the call completes (they aren't known before it runs)."""

    def __init__(self, tenant_id: str | None, task_type: str, clause_sha256: str | None) -> None:
        self.tenant_id = tenant_id
        self.task_type = task_type
        self.clause_sha256 = clause_sha256
        self.model_tier: ModelTier = ModelTier.CACHE_HIT
        self.model_name: str | None = None
        self.cache_layer: CacheLayer = CacheLayer.NONE
        self.complexity = None
        self.input_tokens = 0
        self.output_tokens = 0
        self.escalated_from_cheap = False
        self.details: dict = {}


class CostTracker:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def record(self, event: LLMUsageEvent) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(
                llm_usage_events.insert().values(
                    event_id=event.event_id,
                    tenant_id=event.tenant_id,
                    task_type=event.task_type,
                    model_tier=event.model_tier.value,
                    model_name=event.model_name,
                    cache_layer=event.cache_layer.value,
                    complexity=event.complexity.value if event.complexity else None,
                    input_tokens=event.input_tokens,
                    output_tokens=event.output_tokens,
                    cost_usd=event.cost_usd,
                    latency_ms=event.latency_ms,
                    escalated_from_cheap=event.escalated_from_cheap,
                    clause_sha256=event.clause_sha256,
                    details=event.details,
                    created_at=event.created_at,
                )
            )

        LLM_CACHE_LOOKUP_TOTAL.labels(
            layer=event.cache_layer.value, hit="true" if event.model_tier == ModelTier.CACHE_HIT else "false"
        ).inc()
        LLM_ROUTING_DECISION_TOTAL.labels(
            tier=event.model_tier.value,
            complexity=event.complexity.value if event.complexity else "n/a",
            escalated="true" if event.escalated_from_cheap else "false",
        ).inc()
        LLM_COST_USD_TOTAL.labels(model_tier=event.model_tier.value).inc(event.cost_usd)
        LLM_TOKENS_TOTAL.labels(model_tier=event.model_tier.value, direction="input").inc(event.input_tokens)
        LLM_TOKENS_TOTAL.labels(model_tier=event.model_tier.value, direction="output").inc(event.output_tokens)


@asynccontextmanager
async def track_llm_call(
    tracker: CostTracker,
    *,
    tenant_id: str | None,
    task_type: str,
    clause_sha256: str | None = None,
) -> AsyncIterator[LLMCallTracker]:
    """Usage:

        async with track_llm_call(tracker, tenant_id=..., task_type="extraction") as call:
            call.model_tier = ModelTier.CHEAP_LOCAL
            call.model_name = settings.llm_router_cheap_model
            call.complexity = decision.complexity
            response = await invoke_model(...)
            call.input_tokens = response.usage.input_tokens
            call.output_tokens = response.usage.output_tokens

    Cost is computed automatically from `call.model_name` +
    `call.input_tokens`/`output_tokens` on exit; a cache hit (model_tier
    left at its CACHE_HIT default, model_name left None) is recorded with
    zero cost regardless of what tokens fields hold, since no model was
    actually invoked."""
    started = time.perf_counter()
    call = LLMCallTracker(tenant_id=tenant_id, task_type=task_type, clause_sha256=clause_sha256)
    try:
        yield call
    finally:
        latency_ms = int((time.perf_counter() - started) * 1000)
        cost_usd = 0.0
        if call.model_tier != ModelTier.CACHE_HIT and call.model_name:
            cost_usd = estimate_cost_usd(call.model_name, call.input_tokens, call.output_tokens)

        event = LLMUsageEvent(
            event_id=str(uuid.uuid4()),
            tenant_id=call.tenant_id,
            task_type=call.task_type,
            model_tier=call.model_tier,
            model_name=call.model_name,
            cache_layer=call.cache_layer,
            complexity=call.complexity,
            input_tokens=call.input_tokens,
            output_tokens=call.output_tokens,
            cost_usd=cost_usd,
            latency_ms=latency_ms,
            escalated_from_cheap=call.escalated_from_cheap,
            clause_sha256=call.clause_sha256,
            details=call.details,
        )
        try:
            await tracker.record(event)
        except Exception:
            # Cost tracking must never fail the actual compliance
            # extraction it's observing -- log and move on.
            logger.exception("Failed to record LLM usage event %s (tenant=%s, task=%s)", event.event_id, call.tenant_id, call.task_type)
