"""L1 in-process cache in front of `PolicyRegistry` (Redis, L2), giving
`Evaluator` sub-millisecond `entity_type -> applicable policies` lookups on
the synchronous, high-frequency-trading evaluation hot path
(`POST /v1/execution/transactions/evaluate`) -- one Python dict lookup, no
network round-trip -- instead of a Redis `HGET` per transaction.

Correctness of a distributed L1 cache -- one independent copy per FastAPI
worker process -- rests on TWO deliberately independent mechanisms:

  1. Event-driven invalidation. `PolicyHotReloadSubscriber`
     (app.execution.policy_hot_reload) evicts the affected entity_type's
     entry the instant a compliance officer approves a policy (or any
     other lifecycle event fires), via Redis pub/sub. This is the fast
     path and, under normal operation, the only one that ever fires.
  2. A short TTL safety net. Redis pub/sub is at-most-once delivery -- a
     subscriber that is disconnected for any reason (a rolling deploy, a
     network blip) simply never sees a message published during that
     window, with no redelivery. An entry older than `ttl_seconds` is
     transparently refetched from Redis on next use regardless, which
     bounds the worst-case staleness from a dropped event to
     `ttl_seconds` -- never permanent inconsistency.

`ttl_seconds` is therefore a safety-net tuning knob, not the primary
invalidation mechanism -- keep it short (seconds, not minutes) precisely
because it is only meant to matter when something else already went wrong.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Protocol

from app.observability.metrics import POLICY_CACHE_LOOKUP_TOTAL


class PolicyLookup(Protocol):
    """The interface `Evaluator` actually depends on. Both `PolicyCache`
    and the underlying `PolicyRegistry` satisfy it, so `Evaluator` can be
    wired to either -- production always uses `PolicyCache`; a test can
    hand it a bare `PolicyRegistry` (or a fake) to bypass caching."""

    async def policies_for(self, entity_type: str) -> list[dict[str, str]]: ...


@dataclass
class _CacheEntry:
    policies: list[dict[str, str]]
    cached_at: float


class PolicyCache:
    def __init__(self, registry: PolicyLookup, ttl_seconds: float = 30.0) -> None:
        self._registry = registry
        self._ttl = ttl_seconds
        self._entries: dict[str, _CacheEntry] = {}
        self.hits = 0
        self.misses = 0

    async def policies_for(self, entity_type: str) -> list[dict[str, str]]:
        entry = self._entries.get(entity_type)
        now = time.monotonic()
        if entry is not None and (now - entry.cached_at) < self._ttl:
            self.hits += 1
            POLICY_CACHE_LOOKUP_TOTAL.labels(outcome="hit").inc()
            return entry.policies

        self.misses += 1
        POLICY_CACHE_LOOKUP_TOTAL.labels(outcome="miss").inc()
        policies = await self._registry.policies_for(entity_type)
        self._entries[entity_type] = _CacheEntry(policies=policies, cached_at=now)
        return policies

    def invalidate(self, entity_type: str) -> None:
        """Evicts one entity_type's entry -- called by
        PolicyHotReloadSubscriber for every entity_type a policy event
        affects. Evicting rather than updating in place is deliberate:
        the next `policies_for()` call refetches the authoritative list
        from Redis (L2) instead of this process trying to locally patch a
        list it doesn't own the source of truth for."""
        self._entries.pop(entity_type, None)

    def invalidate_all(self) -> None:
        self._entries.clear()

    def stats(self) -> dict[str, int]:
        return {"cached_entity_types": len(self._entries), "hits": self.hits, "misses": self.misses}
