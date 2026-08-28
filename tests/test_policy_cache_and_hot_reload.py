"""Tests for the L1 PolicyCache, the pub/sub PolicyEvent contract, the
PolicyHotReloadSubscriber that applies events, and PolicyPublisher (the
persistence-side bridge used by the HITL approval endpoint).

Redis and OPA are faked throughout -- these test the actual reload/
invalidation LOGIC (what gets published, what gets evicted, what happens
on failure), not network plumbing already covered by test_opa_execution.py.
"""
from __future__ import annotations

import asyncio

import pytest

import redis.asyncio as aioredis

from app.compiler.models import CompiledRego
from app.db.models import CompiledRule
from app.execution.opa_engine import OPAEngineError
from app.execution.policy_cache import PolicyCache
from app.execution.policy_events import POLICY_EVENTS_CHANNEL, PolicyEvent, PolicyEventPublisher, PolicyEventType
from app.execution.policy_hot_reload import PolicyHotReloadSubscriber
from app.execution.policy_publisher import PolicyPublisher

RULE_ID = "a" * 64 + ":2.1.b"
PACKAGE = "sebi.circulars.sebi_ho_mrd_2024_1.clause_2_1_b"


# --------------------------------------------------------------------------
# PolicyCache
# --------------------------------------------------------------------------


class _FakeRegistry:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.data: dict[str, list[dict[str, str]]] = {}

    async def policies_for(self, entity_type: str) -> list[dict[str, str]]:
        self.calls.append(entity_type)
        return self.data.get(entity_type, [])


@pytest.mark.asyncio
class TestPolicyCache:
    async def test_first_call_is_a_miss_second_is_a_hit(self):
        registry = _FakeRegistry()
        registry.data["Stockbroker"] = [{"rule_id": RULE_ID, "package": PACKAGE}]
        cache = PolicyCache(registry, ttl_seconds=60)

        first = await cache.policies_for("Stockbroker")
        second = await cache.policies_for("Stockbroker")

        assert first == second == [{"rule_id": RULE_ID, "package": PACKAGE}]
        assert registry.calls == ["Stockbroker"]  # only one real fetch
        assert cache.stats() == {"cached_entity_types": 1, "hits": 1, "misses": 1}

    async def test_invalidate_forces_a_refetch(self):
        registry = _FakeRegistry()
        registry.data["Stockbroker"] = [{"rule_id": RULE_ID, "package": PACKAGE}]
        cache = PolicyCache(registry, ttl_seconds=60)

        await cache.policies_for("Stockbroker")
        cache.invalidate("Stockbroker")
        await cache.policies_for("Stockbroker")

        assert registry.calls == ["Stockbroker", "Stockbroker"]

    async def test_ttl_expiry_forces_a_refetch_even_without_invalidate(self):
        registry = _FakeRegistry()
        cache = PolicyCache(registry, ttl_seconds=0.01)

        await cache.policies_for("Stockbroker")
        await asyncio.sleep(0.02)
        await cache.policies_for("Stockbroker")

        assert registry.calls == ["Stockbroker", "Stockbroker"]

    async def test_different_entity_types_are_cached_independently(self):
        registry = _FakeRegistry()
        cache = PolicyCache(registry, ttl_seconds=60)

        await cache.policies_for("Stockbroker")
        await cache.policies_for("AssetManager")
        cache.invalidate("Stockbroker")
        await cache.policies_for("Stockbroker")
        await cache.policies_for("AssetManager")  # still cached

        assert registry.calls == ["Stockbroker", "AssetManager", "Stockbroker"]

    async def test_invalidate_all_clears_every_entry(self):
        registry = _FakeRegistry()
        cache = PolicyCache(registry, ttl_seconds=60)
        await cache.policies_for("Stockbroker")
        await cache.policies_for("AssetManager")

        cache.invalidate_all()

        assert cache.stats()["cached_entity_types"] == 0


# --------------------------------------------------------------------------
# PolicyEventPublisher
# --------------------------------------------------------------------------


class _FakePubSubRedis:
    def __init__(self) -> None:
        self.published: list[tuple[str, str]] = []

    async def publish(self, channel: str, message: str) -> int:
        self.published.append((channel, message))
        return 1  # pretend one subscriber received it


@pytest.mark.asyncio
class TestPolicyEventPublisher:
    async def test_publish_sends_to_the_policy_events_channel(self):
        redis_client = _FakePubSubRedis()
        publisher = PolicyEventPublisher(redis_client)
        event = PolicyEvent(
            event_type=PolicyEventType.APPROVED, rule_id=RULE_ID, rule_version=1, package=PACKAGE,
            rego_code="package x\n", compiled_rule_id=42, approved_by="officer-1",
        )

        result = await publisher.publish(event)

        assert result == 1
        channel, payload = redis_client.published[0]
        assert channel == POLICY_EVENTS_CHANNEL
        replayed = PolicyEvent.model_validate_json(payload)
        assert replayed.rule_id == RULE_ID
        assert replayed.approved_by == "officer-1"


# --------------------------------------------------------------------------
# PolicyHotReloadSubscriber.apply() -- the reload/invalidation logic
# --------------------------------------------------------------------------


class _FakeOPAEngine:
    def __init__(self, *, fail_publish: bool = False, fail_remove: bool = False) -> None:
        self.published: list[CompiledRego] = []
        self.removed: list[str] = []
        self.fail_publish = fail_publish
        self.fail_remove = fail_remove

    async def publish_policy(self, compiled: CompiledRego) -> None:
        if self.fail_publish:
            raise OPAEngineError("simulated OPA outage")
        self.published.append(compiled)

    async def remove_policy(self, rule_id: str) -> None:
        if self.fail_remove:
            raise OPAEngineError("simulated OPA outage")
        self.removed.append(rule_id)


class _FakePolicyRegistry:
    def __init__(self) -> None:
        self.registered: list[tuple[CompiledRego, list[str]]] = []
        self.unregistered: list[str] = []

    async def register(self, compiled: CompiledRego, entity_types: list[str]) -> None:
        self.registered.append((compiled, entity_types))

    async def unregister(self, rule_id: str) -> None:
        self.unregistered.append(rule_id)

    async def policies_for(self, entity_type: str) -> list[dict[str, str]]:
        return []


def _approved_event(**overrides) -> PolicyEvent:
    base = dict(
        event_type=PolicyEventType.APPROVED, rule_id=RULE_ID, rule_version=2, package=PACKAGE,
        rego_code="package x\ndefault allow := false\n", entity_types=["Stockbroker"], compiled_rule_id=7,
        approved_by="officer-1",
    )
    base.update(overrides)
    return PolicyEvent(**base)


@pytest.mark.asyncio
class TestPolicyHotReloadSubscriberApply:
    async def test_approved_event_publishes_registers_and_invalidates(self):
        opa = _FakeOPAEngine()
        registry = _FakePolicyRegistry()
        cache = PolicyCache(_FakeRegistry(), ttl_seconds=60)
        await cache.policies_for("Stockbroker")  # warm the cache so we can prove it gets evicted
        subscriber = PolicyHotReloadSubscriber(redis_client=None, opa_engine=opa, policy_registry=registry, policy_cache=cache)

        await subscriber.apply(_approved_event())

        assert len(opa.published) == 1
        assert opa.published[0].rule_id == RULE_ID
        assert opa.published[0].package == PACKAGE
        assert registry.registered == [(opa.published[0], ["Stockbroker"])]
        assert "Stockbroker" not in cache._entries  # evicted

    async def test_amended_event_behaves_like_approved(self):
        opa = _FakeOPAEngine()
        registry = _FakePolicyRegistry()
        cache = PolicyCache(_FakeRegistry(), ttl_seconds=60)
        subscriber = PolicyHotReloadSubscriber(redis_client=None, opa_engine=opa, policy_registry=registry, policy_cache=cache)

        await subscriber.apply(_approved_event(event_type=PolicyEventType.AMENDED, approved_by=None))

        assert len(opa.published) == 1
        assert len(registry.registered) == 1

    async def test_revoked_event_removes_from_opa_and_unregisters(self):
        opa = _FakeOPAEngine()
        registry = _FakePolicyRegistry()
        cache = PolicyCache(_FakeRegistry(), ttl_seconds=60)
        subscriber = PolicyHotReloadSubscriber(redis_client=None, opa_engine=opa, policy_registry=registry, policy_cache=cache)

        event = PolicyEvent(
            event_type=PolicyEventType.REVOKED, rule_id=RULE_ID, rule_version=2, package=PACKAGE,
            entity_types=["Stockbroker"], compiled_rule_id=7,
        )
        await subscriber.apply(event)

        assert opa.removed == [RULE_ID]
        assert registry.unregistered == [RULE_ID]

    async def test_missing_rego_code_is_not_published_and_raises_nothing(self):
        opa = _FakeOPAEngine()
        registry = _FakePolicyRegistry()
        cache = PolicyCache(_FakeRegistry(), ttl_seconds=60)
        subscriber = PolicyHotReloadSubscriber(redis_client=None, opa_engine=opa, policy_registry=registry, policy_cache=cache)

        await subscriber.apply(_approved_event(rego_code=None))

        assert opa.published == []
        assert registry.registered == []

    async def test_opa_publish_failure_leaves_registry_and_cache_untouched(self):
        """Fail-static: if OPA rejects the policy, neither the Redis
        registry nor the local cache should change -- every layer must
        keep agreeing on the OLD state, not partially adopt the new one."""
        opa = _FakeOPAEngine(fail_publish=True)
        registry = _FakePolicyRegistry()
        cache = PolicyCache(_FakeRegistry(), ttl_seconds=60)
        await cache.policies_for("Stockbroker")
        subscriber = PolicyHotReloadSubscriber(redis_client=None, opa_engine=opa, policy_registry=registry, policy_cache=cache)

        with pytest.raises(OPAEngineError):
            await subscriber.apply(_approved_event())

        assert registry.registered == []
        assert "Stockbroker" in cache._entries  # NOT evicted -- OPA never actually changed

    async def test_opa_remove_failure_leaves_registry_untouched(self):
        opa = _FakeOPAEngine(fail_remove=True)
        registry = _FakePolicyRegistry()
        cache = PolicyCache(_FakeRegistry(), ttl_seconds=60)
        subscriber = PolicyHotReloadSubscriber(redis_client=None, opa_engine=opa, policy_registry=registry, policy_cache=cache)

        event = PolicyEvent(
            event_type=PolicyEventType.REVOKED, rule_id=RULE_ID, rule_version=2, package=PACKAGE,
            entity_types=["Stockbroker"], compiled_rule_id=7,
        )
        with pytest.raises(OPAEngineError):
            await subscriber.apply(event)

        assert registry.unregistered == []

    async def test_multiple_entity_types_all_get_invalidated(self):
        opa = _FakeOPAEngine()
        registry = _FakePolicyRegistry()
        cache = PolicyCache(_FakeRegistry(), ttl_seconds=60)
        await cache.policies_for("Stockbroker")
        await cache.policies_for("AssetManager")
        subscriber = PolicyHotReloadSubscriber(redis_client=None, opa_engine=opa, policy_registry=registry, policy_cache=cache)

        await subscriber.apply(_approved_event(entity_types=["Stockbroker", "AssetManager"]))

        assert "Stockbroker" not in cache._entries
        assert "AssetManager" not in cache._entries


# --------------------------------------------------------------------------
# PolicyHotReloadSubscriber -- malformed-message resilience
# (drives _handle_raw_message directly, the layer above apply())
# --------------------------------------------------------------------------


@pytest.mark.asyncio
class TestMalformedMessageHandling:
    async def test_malformed_json_is_discarded_without_raising(self):
        opa = _FakeOPAEngine()
        registry = _FakePolicyRegistry()
        cache = PolicyCache(_FakeRegistry(), ttl_seconds=60)
        subscriber = PolicyHotReloadSubscriber(redis_client=None, opa_engine=opa, policy_registry=registry, policy_cache=cache)

        await subscriber._handle_raw_message({"data": "not valid json at all"})

        assert opa.published == []  # discarded cleanly, no exception propagated

    async def test_apply_failure_in_handle_raw_message_does_not_propagate(self):
        opa = _FakeOPAEngine(fail_publish=True)
        registry = _FakePolicyRegistry()
        cache = PolicyCache(_FakeRegistry(), ttl_seconds=60)
        subscriber = PolicyHotReloadSubscriber(redis_client=None, opa_engine=opa, policy_registry=registry, policy_cache=cache)

        message = {"data": _approved_event().model_dump_json()}
        await subscriber._handle_raw_message(message)  # must not raise, despite OPA failing internally


# --------------------------------------------------------------------------
# PolicyPublisher
# --------------------------------------------------------------------------


def _compiled_rule(**overrides) -> CompiledRule:
    rule = CompiledRule(
        id=7, clause_id=1, rule_id=RULE_ID, rule_version=2,
        rego_policy="package x\n", opa_package_name=PACKAGE, is_compiled=True, is_active=True, hitl_status="RESOLVED",
    )
    for k, v in overrides.items():
        setattr(rule, k, v)
    return rule


@pytest.mark.asyncio
class TestPolicyPublisher:
    async def test_publish_approved_builds_correct_event(self):
        redis_client = _FakePubSubRedis()
        publisher = PolicyPublisher(PolicyEventPublisher(redis_client))

        await publisher.publish_approved(_compiled_rule(), approved_by="officer-1")

        _, payload = redis_client.published[0]
        event = PolicyEvent.model_validate_json(payload)
        assert event.event_type == PolicyEventType.APPROVED
        assert event.rule_id == RULE_ID
        assert event.rego_code == "package x\n"
        assert event.approved_by == "officer-1"
        assert event.entity_types == ["*"]  # documented default -- see PolicyPublisher's docstring

    async def test_publish_approved_respects_explicit_entity_types(self):
        redis_client = _FakePubSubRedis()
        publisher = PolicyPublisher(PolicyEventPublisher(redis_client))

        await publisher.publish_approved(_compiled_rule(), approved_by="officer-1", entity_types=["Stockbroker"])

        event = PolicyEvent.model_validate_json(redis_client.published[0][1])
        assert event.entity_types == ["Stockbroker"]

    async def test_publish_revoked_has_no_rego_code(self):
        redis_client = _FakePubSubRedis()
        publisher = PolicyPublisher(PolicyEventPublisher(redis_client))

        await publisher.publish_revoked(_compiled_rule())

        event = PolicyEvent.model_validate_json(redis_client.published[0][1])
        assert event.event_type == PolicyEventType.REVOKED
        assert event.rego_code is None

    async def test_publish_approved_without_compiled_rego_raises(self):
        publisher = PolicyPublisher(PolicyEventPublisher(_FakePubSubRedis()))
        incomplete_rule = _compiled_rule(rego_policy=None)

        with pytest.raises(ValueError):
            await publisher.publish_approved(incomplete_rule, approved_by="officer-1")


# --------------------------------------------------------------------------
# PolicyHotReloadSubscriber.run() -- reconnect-with-backoff resilience
# --------------------------------------------------------------------------


class _FlakyPubSub:
    """Fails subscribe() on its first use, then behaves as an empty,
    message-free channel until the subscriber is stopped."""

    def __init__(self, fail_first_n: int) -> None:
        self._remaining_failures = fail_first_n

    async def subscribe(self, _channel: str) -> None:
        if self._remaining_failures > 0:
            self._remaining_failures -= 1
            raise aioredis.ConnectionError("simulated connection drop")

    async def get_message(self, ignore_subscribe_messages: bool = True, timeout: float = 1.0):
        await asyncio.sleep(0)  # yield control so stop() can interleave
        return None

    async def unsubscribe(self, _channel: str) -> None:
        pass

    async def aclose(self) -> None:
        pass


class _FlakyRedisClient:
    def __init__(self, fail_first_n: int) -> None:
        self._pubsub = _FlakyPubSub(fail_first_n)

    def pubsub(self):
        return self._pubsub


@pytest.mark.asyncio
class TestSubscriberReconnect:
    async def test_run_reconnects_after_a_dropped_connection_and_stops_cleanly(self, monkeypatch):
        import app.execution.policy_hot_reload as hot_reload_module

        # Real backoff delays (1s minimum) would make this test slow for
        # no additional signal -- the RETRY behavior is what's under test,
        # not the specific timing.
        monkeypatch.setattr(hot_reload_module, "_RECONNECT_BACKOFF_SECONDS", (0.01, 0.01))

        redis_client = _FlakyRedisClient(fail_first_n=1)
        opa = _FakeOPAEngine()
        registry = _FakePolicyRegistry()
        cache = PolicyCache(_FakeRegistry(), ttl_seconds=60)
        subscriber = PolicyHotReloadSubscriber(redis_client=redis_client, opa_engine=opa, policy_registry=registry, policy_cache=cache)

        run_task = asyncio.create_task(subscriber.run())
        await asyncio.sleep(0.1)  # let it fail once, back off, and reconnect successfully
        subscriber.stop()
        await asyncio.wait_for(run_task, timeout=2.0)  # must exit cleanly, not hang or raise

        assert redis_client._pubsub._remaining_failures == 0  # confirms the retry actually happened
