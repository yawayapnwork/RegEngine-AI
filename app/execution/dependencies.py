"""FastAPI dependency wiring for the execution service.

A single process-wide Redis connection pool is reused across requests
(redis.asyncio.Redis is safe for concurrent use); OPAEngine and the
higher-level collaborators are cheap to construct per-request since they
hold no state beyond their configuration.
"""
from __future__ import annotations

from functools import lru_cache

import redis.asyncio as redis
from fastapi import Depends

from app.config import Settings, get_settings
from app.execution.evaluator import Evaluator
from app.execution.hitl_queue import HITLQueue
from app.execution.opa_engine import OPAEngine
from app.execution.policy_registry import PolicyRegistry


@lru_cache(maxsize=1)
def get_redis_pool() -> redis.Redis:
    return redis.from_url(get_settings().redis_url, decode_responses=True)


def get_opa_engine(settings: Settings = Depends(get_settings)) -> OPAEngine:
    return OPAEngine(base_url=settings.opa_server_url, timeout_seconds=settings.opa_request_timeout_seconds)


def get_policy_registry(settings: Settings = Depends(get_settings)) -> PolicyRegistry:
    return PolicyRegistry(redis_client=get_redis_pool(), registry_key=settings.policy_registry_key)


def get_hitl_queue(settings: Settings = Depends(get_settings)) -> HITLQueue:
    return HITLQueue(redis_client=get_redis_pool(), key_prefix=settings.hitl_key_prefix)


def get_evaluator(
    opa_engine: OPAEngine = Depends(get_opa_engine),
    policy_registry: PolicyRegistry = Depends(get_policy_registry),
    hitl_queue: HITLQueue = Depends(get_hitl_queue),
) -> Evaluator:
    return Evaluator(opa_engine=opa_engine, policy_registry=policy_registry, hitl_queue=hitl_queue)
