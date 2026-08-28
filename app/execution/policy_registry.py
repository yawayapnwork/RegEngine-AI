"""Redis-backed index of which compiled policies apply to which entity type.

Publishing a policy to OPA (see `opa_engine.publish_policy`) makes it
evaluable, but the evaluator still needs to know *which* packages to query
for a given transaction without asking OPA to enumerate its whole policy
set on every request. This registry is the shared answer, stored in Redis
(not process memory) so every FastAPI worker and Celery worker sees the
same policy set the moment `app.compiler` publishes a new rule.
"""
from __future__ import annotations

import json

import redis.asyncio as redis

from app.compiler.models import CompiledRego


class PolicyRegistry:
    def __init__(self, redis_client: redis.Redis, registry_key: str) -> None:
        self._redis = redis_client
        self._key = registry_key  # Redis hash: entity_type -> JSON list[{rule_id, package}]

    async def register(self, compiled: CompiledRego, entity_types: list[str]) -> None:
        """entity_types comes from the caller (the compiler pipeline knows
        ExtractedComplianceRule.target_entities); an empty list means the
        policy has no entity guard and applies to every transaction, so it
        is stored under the sentinel key "*"."""
        entry = json.dumps({"rule_id": compiled.rule_id, "package": compiled.package})
        for entity_type in entity_types or ["*"]:
            existing = await self._redis.hget(self._key, entity_type)
            entries: list[str] = json.loads(existing) if existing else []
            if entry not in entries:
                entries.append(entry)
            await self._redis.hset(self._key, entity_type, json.dumps(entries))

    async def unregister(self, rule_id: str) -> None:
        all_entries = await self._redis.hgetall(self._key)
        for entity_type, raw in all_entries.items():
            entries = json.loads(raw)
            filtered = [e for e in entries if json.loads(e)["rule_id"] != rule_id]
            if filtered:
                await self._redis.hset(self._key, entity_type, json.dumps(filtered))
            else:
                await self._redis.hdel(self._key, entity_type)

    async def policies_for(self, entity_type: str) -> list[dict[str, str]]:
        """Applicable policies = those scoped to this entity_type plus every
        policy with no entity guard ("*")."""
        results: list[dict[str, str]] = []
        for key in (entity_type, "*"):
            raw = await self._redis.hget(self._key, key)
            if raw:
                results.extend(json.loads(e) for e in json.loads(raw))
        # de-dupe in case a rule was registered under both keys
        seen: set[str] = set()
        deduped = []
        for r in results:
            if r["rule_id"] not in seen:
                seen.add(r["rule_id"])
                deduped.append(r)
        return deduped
