"""Hot-reloads the FIX gateway's in-memory loaded-policy set from the
SAME `regengine:policy_events` pub/sub channel
`app.execution.policy_hot_reload.PolicyHotReloadSubscriber` already
listens on -- reusing that infrastructure rather than inventing a
parallel notification path.

One real gap this subscriber has to work around:
`app.execution.policy_events.PolicyEvent` is deliberately self-contained
with the compiled RESULT it exists to propagate cheaply (`rego_code`),
but it does not carry `jsonlogic_ast` -- that field was never part of
its "cheap enough to avoid a Postgres round-trip on the approval
critical path" design goal (see that module's docstring), since nothing
consumed jsonlogic_ast on any hot path before this package existed. So
unlike `PolicyHotReloadSubscriber`, this subscriber DOES pay one
Postgres round-trip per event (fetching the `CompiledRule` row by
`compiled_rule_id`, with `clause`/`clause.circular` eager-loaded for
the SEBI citation) -- acceptable here because policy publish/hot-reload
events are rare (human-paced, at most a few per minute even in a busy
compliance team), never on the per-order path this package's actual
latency budget is about.
"""
from __future__ import annotations

import asyncio
import logging

import redis.asyncio as redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from app.db.models import Clause, CompiledRule
from app.execution.policy_events import POLICY_EVENTS_CHANNEL, PolicyEvent, PolicyEventType
from app.fix_gateway.policy_manifest import LoadedFixPolicy, UnsupportedForFixGatewayError, build_loaded_policy

logger = logging.getLogger(__name__)

_RECONNECT_BACKOFF_SECONDS = (1, 2, 5, 10, 30)


class FixPolicyStore:
    """The FIX gateway's in-memory loaded-policy set for one process --
    analogous in spirit to `app.execution.policy_cache.PolicyCache`
    (a process-local, hot-reloaded L1 view), except this one holds fully
    LOADED native policy objects, not just registry entries, since
    loading a policy (packaging + regengine_native.CompiledPolicy
    construction) is itself cheap enough to do at hot-reload time but
    not something the per-order path should redo."""

    def __init__(self) -> None:
        self._by_rule_id: dict[str, LoadedFixPolicy] = {}

    def put(self, policy: LoadedFixPolicy) -> None:
        self._by_rule_id[policy.rule_id] = policy

    def remove(self, rule_id: str) -> None:
        self._by_rule_id.pop(rule_id, None)

    def current_policies(self) -> list[LoadedFixPolicy]:
        return list(self._by_rule_id.values())


class FixGatewayHotReloadSubscriber:
    def __init__(self, redis_client: redis.Redis, session_factory: async_sessionmaker[AsyncSession], store: FixPolicyStore) -> None:
        self._redis = redis_client
        self._session_factory = session_factory
        self._store = store
        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        backoff_index = 0
        while not self._stop.is_set():
            try:
                pubsub = self._redis.pubsub()
                await pubsub.subscribe(POLICY_EVENTS_CHANNEL)
                backoff_index = 0
                logger.info("FixGatewayHotReloadSubscriber subscribed to %s", POLICY_EVENTS_CHANNEL)
                async for message in pubsub.listen():
                    if self._stop.is_set():
                        break
                    if message["type"] != "message":
                        continue
                    await self._handle_raw_event(message["data"])
            except Exception:  # noqa: BLE001 - a subscriber crash must never take down the FastAPI worker; reconnect instead
                delay = _RECONNECT_BACKOFF_SECONDS[min(backoff_index, len(_RECONNECT_BACKOFF_SECONDS) - 1)]
                logger.exception("FixGatewayHotReloadSubscriber lost its Redis connection; reconnecting in %ds.", delay)
                backoff_index += 1
                await asyncio.sleep(delay)

    async def _handle_raw_event(self, raw: bytes | str) -> None:
        try:
            event = PolicyEvent.model_validate_json(raw)
        except Exception:  # noqa: BLE001
            logger.exception("FixGatewayHotReloadSubscriber received an unparseable policy event; ignoring it.")
            return

        if event.event_type == PolicyEventType.REVOKED:
            self._store.remove(event.rule_id)
            logger.info("FIX gateway: removed revoked policy rule_id=%s from the loaded set.", event.rule_id)
            return

        try:
            async with self._session_factory() as session:
                compiled_rule = await session.scalar(
                    select(CompiledRule)
                    .options(selectinload(CompiledRule.clause).selectinload(Clause.circular))
                    .where(CompiledRule.id == event.compiled_rule_id)
                )
                if compiled_rule is None:
                    logger.warning("FIX gateway: policy event referenced compiled_rule_id=%s, which no longer exists; skipping.", event.compiled_rule_id)
                    return
                loaded = build_loaded_policy(compiled_rule)
        except UnsupportedForFixGatewayError as exc:
            # Not every compiled policy is packageable for this fast path
            # (see policy_manifest.py) -- this is an expected, common
            # outcome (most rules will keep going through the OPA path
            # only), not an error worth crashing or retrying over.
            logger.info("FIX gateway: rule_id=%s is not packageable for the native fast path (%s); it will only be enforced via the OPA path.", event.rule_id, exc)
            return
        except Exception:  # noqa: BLE001 - one bad policy must never stop the subscriber from processing the next event
            logger.exception("FIX gateway: failed to load policy for rule_id=%s from event; leaving the previous loaded version (if any) in place.", event.rule_id)
            return

        self._store.put(loaded)
        logger.info("FIX gateway: (re)loaded rule_id=%s (%d check(s)) into the native policy set.", loaded.rule_id, loaded.compiled_policy.num_checks)


__all__ = ["FixPolicyStore", "FixGatewayHotReloadSubscriber"]
