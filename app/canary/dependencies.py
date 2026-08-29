"""DI wiring, mirroring app.execution.dependencies's shape."""
from __future__ import annotations

import redis.asyncio as redis

from app.canary.opa_publisher import CanaryOPAPublisher
from app.canary.orchestrator import CanaryOrchestrator
from app.canary.parity import ParityAnalyzer
from app.canary.store import CanaryStore
from app.config import Settings
from app.execution.opa_engine import OPAEngine


def get_canary_store(redis_client: redis.Redis, settings: Settings) -> CanaryStore:
    return CanaryStore(redis_client=redis_client, key_prefix=settings.canary_store_key_prefix)


def get_canary_opa_publisher(settings: Settings) -> CanaryOPAPublisher:
    return CanaryOPAPublisher(OPAEngine(base_url=settings.opa_server_url, timeout_seconds=settings.opa_request_timeout_seconds))


def get_canary_orchestrator(redis_client: redis.Redis, settings: Settings) -> CanaryOrchestrator:
    return CanaryOrchestrator(
        store=get_canary_store(redis_client, settings),
        opa_publisher=get_canary_opa_publisher(settings),
        promotion_max_divergence_pct=settings.canary_promotion_max_divergence_pct,
        rollback_divergence_pct=settings.canary_rollback_divergence_pct,
        rollback_min_sample_size=settings.canary_rollback_min_sample_size,
        evaluation_window_seconds=settings.canary_evaluation_window_seconds,
    )


def get_parity_analyzer(redis_client: redis.Redis, settings: Settings) -> ParityAnalyzer:
    return ParityAnalyzer(
        store=get_canary_store(redis_client, settings),
        rollback_divergence_pct=settings.canary_rollback_divergence_pct,
        rollback_min_sample_size=settings.canary_rollback_min_sample_size,
    )
