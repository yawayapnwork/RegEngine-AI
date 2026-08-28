"""The subscriber half of `app.execution.policy_events`: listens on
`POLICY_EVENTS_CHANNEL` and, for each event, hot-reloads OPA, updates the
Redis L2 `PolicyRegistry`, and evicts the affected entries from this
process's local L1 `PolicyCache` -- the mechanism that makes "a compliance
officer approves a policy" become "every live evaluation sees it" with no
restart and no in-flight request ever seeing a torn/partial state.

Runs as a background asyncio task INSIDE every FastAPI worker process
(started from `app.main`'s lifespan), not as one centralized standalone
service. This is deliberate, not an oversight: the thing each subscriber
instance is responsible for keeping coherent is THAT PROCESS's own
`PolicyCache` -- a single external reloader could update OPA and Redis
just fine, but could never reach into another process's memory to evict
its cache. Every FastAPI replica therefore runs its own subscriber.

Celery workers do NOT need one: their evaluation path (app.execution.tasks)
builds a fresh `PolicyRegistry` per task with no L1 cache in front of it,
so it always reads current Redis L2 state already -- there is nothing
there for a subscriber to invalidate. They still benefit from the FastAPI
subscribers' OPA hot-reload, since OPA is one shared server every process
queries.

Publishing to OPA from every FastAPI subscriber instance is deliberately
redundant, not a bug: `OPAEngine.publish_policy` PUTs to OPA's Policy API,
which is idempotent (see that module's docstring -- "compiles and
hot-swaps atomically"), so N processes independently re-publishing the
same Rego on the same event is harmless, and far more resilient than
electing a single publisher every other replica would then depend on.
"""
from __future__ import annotations

import asyncio
import logging

import redis.asyncio as redis

from app.compiler.models import CompiledRego
from app.execution.opa_engine import OPAEngine, OPAEngineError
from app.execution.policy_cache import PolicyCache
from app.execution.policy_events import POLICY_EVENTS_CHANNEL, PolicyEvent, PolicyEventType
from app.execution.policy_registry import PolicyRegistry

logger = logging.getLogger(__name__)

# Capped exponential backoff between reconnect attempts after a dropped
# Redis pub/sub connection. Capped (not unbounded) so a prolonged Redis
# outage still retries every 30s rather than backing off into minutes --
# this subscriber being dark for a long stretch directly widens the
# window PolicyCache's TTL safety net has to cover alone.
_RECONNECT_BACKOFF_SECONDS = (1, 2, 5, 10, 30)


class PolicyHotReloadSubscriber:
    def __init__(
        self,
        redis_client: redis.Redis,
        opa_engine: OPAEngine,
        policy_registry: PolicyRegistry,
        policy_cache: PolicyCache,
    ) -> None:
        self._redis = redis_client
        self._opa = opa_engine
        self._registry = policy_registry
        self._cache = policy_cache
        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        """Runs until `stop()` is called (normal shutdown, from
        app.main's lifespan). Any Redis connection failure is caught and
        retried with backoff, forever -- a dropped connection must never
        permanently silence hot-reloads for the rest of this process's
        lifetime; that would quietly turn every subsequent policy change
        into "stale until this worker is restarted"."""
        attempt = 0
        while not self._stop.is_set():
            try:
                await self._subscribe_and_listen()
                attempt = 0  # a full listen cycle completed cleanly (i.e. stop() was called)
            except (redis.ConnectionError, redis.TimeoutError) as exc:
                delay = _RECONNECT_BACKOFF_SECONDS[min(attempt, len(_RECONNECT_BACKOFF_SECONDS) - 1)]
                logger.warning("Policy event subscriber lost its Redis connection (%s); reconnecting in %ds.", exc, delay)
                attempt += 1
                await self._sleep_or_stop(delay)
            except Exception:  # noqa: BLE001 - this loop must never crash-exit; that would be silent, permanent staleness
                logger.exception("Unexpected error in policy event subscriber; restarting listen loop.")
                await self._sleep_or_stop(_RECONNECT_BACKOFF_SECONDS[0])

    async def _sleep_or_stop(self, delay: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=delay)
        except asyncio.TimeoutError:
            pass  # normal case: delay elapsed without stop() being called

    async def _subscribe_and_listen(self) -> None:
        pubsub = self._redis.pubsub()
        await pubsub.subscribe(POLICY_EVENTS_CHANNEL)
        logger.info("Policy event subscriber connected; listening on '%s'.", POLICY_EVENTS_CHANNEL)
        try:
            while not self._stop.is_set():
                message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                if message is None:
                    continue
                await self._handle_raw_message(message)
        finally:
            await pubsub.unsubscribe(POLICY_EVENTS_CHANNEL)
            await pubsub.aclose()

    async def _handle_raw_message(self, message: dict) -> None:
        try:
            event = PolicyEvent.model_validate_json(message["data"])
        except Exception as exc:  # noqa: BLE001 - a malformed event must not kill the subscriber
            logger.error("Discarding malformed policy event: %s", exc)
            return

        try:
            await self.apply(event)
        except Exception:  # noqa: BLE001 - one bad event must not stop later events being processed
            logger.exception("Failed to apply policy event for rule_id=%s (event_type=%s)", event.rule_id, event.event_type.value)

    async def apply(self, event: PolicyEvent) -> None:
        """The actual reload logic, factored out of the listen loop so
        tests can drive it directly with a hand-built PolicyEvent instead
        of a raw Redis pub/sub message."""
        if event.event_type in (PolicyEventType.APPROVED, PolicyEventType.AMENDED):
            await self._apply_active(event)
        elif event.event_type == PolicyEventType.REVOKED:
            await self._apply_revoked(event)
        else:  # pragma: no cover - exhaustive over PolicyEventType today; guards a future enum addition
            logger.error("Unhandled policy event type: %s", event.event_type)
            return

        for entity_type in event.entity_types:
            self._cache.invalidate(entity_type)
        logger.debug("Invalidated local PolicyCache entries for entity_types=%s.", event.entity_types)

    async def _apply_active(self, event: PolicyEvent) -> None:
        if not event.rego_code:
            logger.error("Policy event '%s' for rule_id=%s is missing rego_code; cannot publish to OPA.", event.event_type.value, event.rule_id)
            return

        # thresholds_compiled is cosmetic metadata (used only in
        # CompiledRego's own logging/audit fields) that this event doesn't
        # carry and OPA's Policy API never reads -- 0 here has no
        # functional effect on the hot-reload.
        compiled = CompiledRego(rule_id=event.rule_id, package=event.package, rego_code=event.rego_code, thresholds_compiled=0)
        try:
            await self._opa.publish_policy(compiled)
        except OPAEngineError:
            logger.exception(
                "Failed to hot-reload OPA policy for rule_id=%s; will retry on the next event for this rule "
                "(or the officer/operator can re-trigger). Local caches are NOT invalidated for this event.",
                event.rule_id,
            )
            raise

        await self._registry.register(compiled, event.entity_types)
        logger.info(
            "Hot-reloaded OPA policy '%s' v%d (package=%s) for entity_types=%s.",
            event.rule_id, event.rule_version, event.package, event.entity_types,
        )

    async def _apply_revoked(self, event: PolicyEvent) -> None:
        try:
            await self._opa.remove_policy(event.rule_id)
        except OPAEngineError:
            logger.exception("Failed to remove revoked OPA policy for rule_id=%s.", event.rule_id)
            raise
        await self._registry.unregister(event.rule_id)
        logger.info("Revoked OPA policy '%s'.", event.rule_id)
