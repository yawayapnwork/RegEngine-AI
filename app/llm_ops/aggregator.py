"""Aggregation queries backing the LLM cost-analytics dashboard.

Mirrors `app.analytics.aggregator.ComplianceAggregator`'s shape (a
stateless, per-request aggregator over a single table) but reads
`llm_usage_events` instead of the audit ledger -- kept in its own module
rather than folded into `ComplianceAggregator` since the two answer
unrelated questions (compliance outcomes vs. infrastructure spend) for a
different audience (compliance officers vs. platform/finance ops).
"""
from __future__ import annotations

import datetime as dt

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from app.llm_ops.models import CostSummary, ModelTier, TenantCostBreakdown, llm_usage_events
from app.llm_ops.pricing import estimate_cost_usd


class CostAggregator:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def build_summary(
        self,
        start: dt.datetime,
        end: dt.datetime,
        tenant_id: str | None = None,
    ) -> CostSummary:
        async with self._engine.connect() as conn:
            base_filter = [llm_usage_events.c.created_at >= start, llm_usage_events.c.created_at <= end]
            if tenant_id:
                base_filter.append(llm_usage_events.c.tenant_id == tenant_id)

            totals_row = (
                await conn.execute(
                    select(
                        func.count().label("total_requests"),
                        func.sum(case((llm_usage_events.c.model_tier == ModelTier.CACHE_HIT.value, 1), else_=0)).label("cache_hits"),
                        func.coalesce(func.sum(llm_usage_events.c.cost_usd), 0).label("total_cost_usd"),
                    ).where(*base_filter)
                )
            ).one()

            tier_rows = (
                await conn.execute(
                    select(llm_usage_events.c.model_tier, func.count())
                    .where(*base_filter)
                    .group_by(llm_usage_events.c.model_tier)
                )
            ).all()
            tier_distribution = {tier: count for tier, count in tier_rows}

            tenant_breakdown = await self._tenant_breakdown(conn, base_filter)

        total_requests = totals_row.total_requests or 0
        cache_hits = totals_row.cache_hits or 0
        cache_hit_ratio_pct = (cache_hits / total_requests * 100.0) if total_requests else 0.0
        total_cost = float(totals_row.total_cost_usd or 0.0)
        estimated_savings = sum(t.estimated_savings_usd for t in tenant_breakdown)

        return CostSummary(
            period_start=start,
            period_end=end,
            total_requests=total_requests,
            total_cache_hits=cache_hits,
            cache_hit_ratio_pct=round(cache_hit_ratio_pct, 2),
            total_cost_usd=round(total_cost, 6),
            estimated_savings_usd=round(estimated_savings, 6),
            tier_distribution=tier_distribution,
            tenant_breakdown=tenant_breakdown,
        )

    async def _tenant_breakdown(self, conn, base_filter: list) -> list[TenantCostBreakdown]:
        rows = (
            await conn.execute(
                select(
                    llm_usage_events.c.tenant_id,
                    func.count().label("total_requests"),
                    func.sum(case((llm_usage_events.c.model_tier == ModelTier.CACHE_HIT.value, 1), else_=0)).label("cache_hits"),
                    func.sum(case((llm_usage_events.c.model_tier == ModelTier.CHEAP_LOCAL.value, 1), else_=0)).label("cheap_tier_requests"),
                    func.sum(case((llm_usage_events.c.model_tier == ModelTier.FRONTIER.value, 1), else_=0)).label("frontier_tier_requests"),
                    func.sum(case((llm_usage_events.c.escalated_from_cheap.is_(True), 1), else_=0)).label("escalations"),
                    func.coalesce(func.sum(llm_usage_events.c.input_tokens), 0).label("total_input_tokens"),
                    func.coalesce(func.sum(llm_usage_events.c.output_tokens), 0).label("total_output_tokens"),
                    func.coalesce(func.sum(llm_usage_events.c.cost_usd), 0).label("total_cost_usd"),
                )
                .where(*base_filter)
                .group_by(llm_usage_events.c.tenant_id)
                .order_by(func.coalesce(func.sum(llm_usage_events.c.cost_usd), 0).desc())
            )
        ).all()

        breakdown: list[TenantCostBreakdown] = []
        for row in rows:
            total_requests = row.total_requests or 0
            cache_hits = row.cache_hits or 0
            cache_hit_ratio_pct = (cache_hits / total_requests * 100.0) if total_requests else 0.0

            # "What would this tenant have cost with no cache at all?": every
            # cache hit is re-priced as if it had instead been a fresh
            # frontier-tier call on roughly-comparable clause-sized input.
            # This is deliberately an estimate (the actual token count of a
            # cache hit's *would-have-been* prompt is unknowable after the
            # fact) rather than a claim of precise counterfactual cost.
            avg_input = (row.total_input_tokens / max(1, total_requests - cache_hits)) if total_requests > cache_hits else 500
            avg_output = (row.total_output_tokens / max(1, total_requests - cache_hits)) if total_requests > cache_hits else 300
            hypothetical_cache_hit_cost = estimate_cost_usd("huggingface/Qwen/Qwen2.5-72B-Instruct", int(avg_input), int(avg_output))
            estimated_without_cache = float(row.total_cost_usd or 0.0) + cache_hits * hypothetical_cache_hit_cost
            estimated_savings = estimated_without_cache - float(row.total_cost_usd or 0.0)

            breakdown.append(
                TenantCostBreakdown(
                    tenant_id=row.tenant_id or "unknown",
                    total_requests=total_requests,
                    cache_hits=cache_hits,
                    cache_hit_ratio_pct=round(cache_hit_ratio_pct, 2),
                    cheap_tier_requests=row.cheap_tier_requests or 0,
                    frontier_tier_requests=row.frontier_tier_requests or 0,
                    escalations=row.escalations or 0,
                    total_input_tokens=row.total_input_tokens or 0,
                    total_output_tokens=row.total_output_tokens or 0,
                    total_cost_usd=round(float(row.total_cost_usd or 0.0), 6),
                    estimated_cost_without_cache_usd=round(estimated_without_cache, 6),
                    estimated_savings_usd=round(estimated_savings, 6),
                )
            )
        return breakdown
