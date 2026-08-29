"""FastAPI dependency wiring for the execution service.

A single process-wide Redis connection pool is reused across requests
(redis.asyncio.Redis is safe for concurrent use); OPAEngine and the
higher-level collaborators are cheap to construct per-request since they
hold no state beyond their configuration. `PolicyCache` is the deliberate
exception -- see its module docstring -- it MUST be a process-wide
singleton (`@lru_cache`), not constructed per-request, since its entire
purpose is to hold state across requests.
"""
from __future__ import annotations

from functools import lru_cache

import redis.asyncio as redis
from fastapi import Depends

from app.config import Settings, get_settings
from app.execution.evaluator import Evaluator
from app.execution.hitl_queue import HITLQueue
from app.execution.opa_engine import OPAEngine
from app.execution.policy_cache import PolicyCache
from app.execution.policy_events import PolicyEventPublisher
from app.execution.policy_publisher import PolicyPublisher
from app.execution.policy_registry import PolicyRegistry
from app.governance.kill_switch import KillSwitchStore


@lru_cache(maxsize=1)
def get_redis_pool() -> redis.Redis:
    return redis.from_url(get_settings().redis_url, decode_responses=True)


def get_opa_engine(settings: Settings = Depends(get_settings)) -> OPAEngine:
    return OPAEngine(base_url=settings.opa_server_url, timeout_seconds=settings.opa_request_timeout_seconds)


def get_policy_registry(settings: Settings = Depends(get_settings)) -> PolicyRegistry:
    return PolicyRegistry(redis_client=get_redis_pool(), registry_key=settings.policy_registry_key)


@lru_cache(maxsize=1)
def get_policy_cache() -> PolicyCache:
    """Process-wide singleton -- see this module's and PolicyCache's
    docstrings. Built directly off `get_settings()` (not FastAPI's
    `Depends`) so it can also be constructed from `app.main`'s lifespan,
    outside request handling, the same way `app.ledger.db.get_ledger_engine`
    already is."""
    settings = get_settings()
    registry = PolicyRegistry(redis_client=get_redis_pool(), registry_key=settings.policy_registry_key)
    return PolicyCache(registry, ttl_seconds=settings.policy_cache_ttl_seconds)


def get_hitl_queue(settings: Settings = Depends(get_settings)) -> HITLQueue:
    return HITLQueue(redis_client=get_redis_pool(), key_prefix=settings.hitl_key_prefix)


def get_kill_switch_store(settings: Settings = Depends(get_settings)) -> KillSwitchStore:
    return KillSwitchStore(redis_client=get_redis_pool(), key_prefix=settings.governance_key_prefix)


def get_policy_event_publisher() -> PolicyEventPublisher:
    return PolicyEventPublisher(redis_client=get_redis_pool())


def get_policy_publisher(
    event_publisher: PolicyEventPublisher = Depends(get_policy_event_publisher),
) -> PolicyPublisher:
    return PolicyPublisher(event_publisher)


def get_evaluator(
    opa_engine: OPAEngine = Depends(get_opa_engine),
    policy_cache: PolicyCache = Depends(get_policy_cache),
    hitl_queue: HITLQueue = Depends(get_hitl_queue),
    kill_switch_store: KillSwitchStore = Depends(get_kill_switch_store),
) -> Evaluator:
    return Evaluator(opa_engine=opa_engine, policy_registry=policy_cache, hitl_queue=hitl_queue, kill_switch_store=kill_switch_store)
