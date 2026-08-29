"""Requirement 3's "retry tracking logic": a Redis-backed record of every
repair attempt made against a given rule_id's current failure, so an
in-progress (or already-concluded) healing loop can be inspected,
resumed, or audited independently of whichever Celery worker process
ran it.

Storage shape (mirrors app.incident.store.BreachEventStore's
lpush/ltrim pattern -- a capped, newest-first list per key -- rather
than app.ledger's hash-chain or app.resilience.dead_letter_queue's
sorted-set-index approach, since a healing history is read almost
always by its single rule_id, never listed/filtered globally the way
DLQ entries are):

    {prefix}:attempts:{rule_id}  -> Redis list of RepairAttempt JSON,
                                     newest first, capped at max_history
    {prefix}:count:{rule_id}     -> Redis string, the current attempt
                                     counter (separate from the capped
                                     list so retry-count enforcement is
                                     correct even if history is trimmed)
"""
from __future__ import annotations

import redis.asyncio as redis

from app.healing.models import RepairAttempt


class HealingAttemptTracker:
    def __init__(self, redis_client: redis.Redis, key_prefix: str, max_history: int = 20) -> None:
        self._redis = redis_client
        self._prefix = key_prefix
        self._max_history = max_history

    def _attempts_key(self, rule_id: str) -> str:
        return f"{self._prefix}:attempts:{rule_id}"

    def _count_key(self, rule_id: str) -> str:
        return f"{self._prefix}:count:{rule_id}"

    async def get_attempt_count(self, rule_id: str) -> int:
        raw = await self._redis.get(self._count_key(rule_id))
        return int(raw) if raw is not None else 0

    async def record_attempt(self, rule_id: str, attempt: RepairAttempt) -> int:
        """Persists `attempt` and increments this rule_id's counter.
        Returns the new count so the caller can compare against
        `settings.policy_self_healing_max_retries` without a second
        round-trip."""
        new_count = await self._redis.incr(self._count_key(rule_id))
        await self._redis.lpush(self._attempts_key(rule_id), attempt.model_dump_json())
        await self._redis.ltrim(self._attempts_key(rule_id), 0, self._max_history - 1)
        return new_count

    async def get_history(self, rule_id: str) -> list[RepairAttempt]:
        """Newest-first, matching `lpush`'s insertion order -- oldest
        attempt is `history[-1]`, matching how a reviewer wants to read
        it ("what did it try most recently")."""
        raw_entries = await self._redis.lrange(self._attempts_key(rule_id), 0, -1)
        return [RepairAttempt.model_validate_json(raw) for raw in raw_entries]

    async def reset(self, rule_id: str) -> None:
        """Called once a rule_id's loop concludes (healed or escalated)
        so a LATER, unrelated failure on the same rule_id (e.g. after a
        human edits and re-submits it) starts its own retry budget from
        zero rather than inheriting a stale counter."""
        await self._redis.delete(self._count_key(rule_id))
        await self._redis.delete(self._attempts_key(rule_id))
