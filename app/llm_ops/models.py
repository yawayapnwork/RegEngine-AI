"""Data contracts and persistence schema for the LLM cost-optimization
layer: semantic caching, model-tier routing, and per-tenant spend tracking.

`llm_usage_events` uses SQLAlchemy Core (like `app.ledger.models`), bound
to the shared `Base.metadata` so Alembic autogenerate and `create_all` see
it alongside the rest of the schema -- see
`migrations/versions/0004_llm_cost_tracking.py` for the corresponding
migration.
"""
from __future__ import annotations

import datetime as dt
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Index, Numeric, String, Table, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.types import JSON, BigInteger, Integer

from app.db.base import Base
from app.db import models as _db_models  # noqa: F401 - registers `tenants` on Base.metadata for the FK below

_JSON_TYPE = JSON().with_variant(JSONB, "postgresql")
_ID_TYPE = BigInteger().with_variant(Integer, "sqlite")

metadata = Base.metadata


class ModelTier(str, Enum):
    CACHE_HIT = "cache_hit"        # no model invoked at all
    CHEAP_LOCAL = "cheap_local"    # e.g. the QLoRA-fine-tuned model via vLLM/Ollama (llm_finetune/)
    FRONTIER = "frontier"          # e.g. Claude 3.5 Sonnet


class TaskComplexity(str, Enum):
    SIMPLE = "simple"          # single unambiguous numeric threshold, no qualitative language
    MODERATE = "moderate"      # multiple thresholds or conditions, still deterministic
    COMPLEX = "complex"        # qualitative directives, ambiguous phrasing, cross-references, long text


class CacheLayer(str, Enum):
    NONE = "none"
    EXACT = "exact"        # Redis, SHA-256 of normalized text
    SEMANTIC = "semantic"  # Qdrant, cosine similarity above threshold


# ---------------------------------------------------------------------------
# llm_usage_events -- one row per LLM invocation attempt (cache hits included,
# at zero cost/tokens, so cache-hit-ratio and $-saved can be computed from
# the same table without a separate counter that can drift out of sync).
# ---------------------------------------------------------------------------

llm_usage_events = Table(
    "llm_usage_events",
    metadata,
    Column("id", _ID_TYPE, primary_key=True, autoincrement=True),
    Column("event_id", String(64), nullable=False, unique=True),
    Column("tenant_id", Text, ForeignKey("tenants.tenant_id", ondelete="SET NULL"), nullable=True),
    Column("task_type", String(32), nullable=False),  # "extraction" | "rego_compile" | "audit"
    Column("model_tier", String(16), nullable=False),  # ModelTier
    Column("model_name", String(128), nullable=True),  # None when model_tier == cache_hit
    Column("cache_layer", String(16), nullable=False, server_default="none"),  # CacheLayer
    Column("complexity", String(16), nullable=True),  # TaskComplexity, null for cache hits (never scored)
    Column("input_tokens", Integer, nullable=False, server_default="0"),
    Column("output_tokens", Integer, nullable=False, server_default="0"),
    Column("cost_usd", Numeric(12, 6), nullable=False, server_default="0"),
    Column("latency_ms", Integer, nullable=False, server_default="0"),
    Column("escalated_from_cheap", Boolean, nullable=False, server_default="false"),
    Column("clause_sha256", String(64), nullable=True),
    Column("details", _JSON_TYPE, nullable=False, server_default="{}"),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Index("ix_llm_usage_events_tenant_created", "tenant_id", "created_at"),
    Index("ix_llm_usage_events_model_tier", "model_tier"),
    Index("ix_llm_usage_events_created_at", "created_at"),
)


# ---------------------------------------------------------------------------
# Pydantic contracts
# ---------------------------------------------------------------------------


class CacheLookupResult(BaseModel):
    hit: bool
    layer: CacheLayer = CacheLayer.NONE
    similarity: float | None = Field(None, description="Cosine similarity for CacheLayer.SEMANTIC hits; 1.0 implied for EXACT.")
    cached_response: dict[str, Any] | None = None
    cache_key: str | None = None


class RoutingDecision(BaseModel):
    tier: ModelTier
    model_name: str
    complexity: TaskComplexity
    reasons: list[str] = Field(default_factory=list)


class LLMUsageEvent(BaseModel):
    event_id: str
    tenant_id: str | None = None
    task_type: str
    model_tier: ModelTier
    model_name: str | None = None
    cache_layer: CacheLayer = CacheLayer.NONE
    complexity: TaskComplexity | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    latency_ms: int = 0
    escalated_from_cheap: bool = False
    clause_sha256: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)
    created_at: dt.datetime = Field(default_factory=lambda: dt.datetime.now(dt.timezone.utc))


# ---------------------------------------------------------------------------
# Dashboard response models
# ---------------------------------------------------------------------------


class TenantCostBreakdown(BaseModel):
    tenant_id: str
    total_requests: int = 0
    cache_hits: int = 0
    cache_hit_ratio_pct: float = 0.0
    cheap_tier_requests: int = 0
    frontier_tier_requests: int = 0
    escalations: int = 0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cost_usd: float = 0.0
    estimated_cost_without_cache_usd: float = Field(
        0.0, description="What total_cost_usd would have been had every cache hit instead been a fresh frontier-tier call -- the headline 'savings' number."
    )
    estimated_savings_usd: float = 0.0


class CostSummary(BaseModel):
    period_start: dt.datetime
    period_end: dt.datetime
    total_requests: int = 0
    total_cache_hits: int = 0
    cache_hit_ratio_pct: float = 0.0
    total_cost_usd: float = 0.0
    estimated_savings_usd: float = 0.0
    tier_distribution: dict[str, int] = Field(default_factory=dict)
    tenant_breakdown: list[TenantCostBreakdown] = Field(default_factory=list)
