"""Tests for the shadow-execution/canary-release service (app.canary).

Follows tests/test_opa_execution.py's established two-layer convention:
`httpx.MockTransport` for the real OPA HTTP contract (no live OPA
process), hand-rolled fakes for Redis/Postgres collaborators (matching
tests/test_agent_graph.py's `_FakeRedis` idiom used throughout this
session's new subsystems).
"""
from __future__ import annotations

import httpx
import pytest

from app.canary.mirroring import ShadowTrafficMirror, spawn_shadow_evaluation
from app.canary.models import CanaryDecision, CanaryRun, CanaryStatus
from app.canary.opa_publisher import CanaryOPAPublisher, canary_opa_rule_id, canary_package
from app.canary.orchestrator import CanaryOrchestrator
from app.canary.parity import ParityAnalyzer, decision_from_opa_result
from app.canary.store import CanaryStore
from app.compiler.models import CompiledRego
from app.db.models import CompiledRule
from app.execution.models import Decision
from app.execution.opa_engine import OPAEngine
from app.execution.policy_publisher import PolicyPublisher

_RealAsyncClient = httpx.AsyncClient


def _patch_transport(monkeypatch: pytest.MonkeyPatch, handler) -> None:
    def _fake_async_client(**kwargs):
        kwargs.pop("transport", None)
        return _RealAsyncClient(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _fake_async_client)


class _FakeRedis:
    def __init__(self) -> None:
        self.strings: dict[str, str] = {}
        self.sets: dict[str, set[str]] = {}

    async def set(self, key: str, value: str) -> None:
        self.strings[key] = value

    async def get(self, key: str) -> str | None:
        return self.strings.get(key)

    async def sadd(self, key: str, member: str) -> None:
        self.sets.setdefault(key, set()).add(member)

    async def srem(self, key: str, member: str) -> None:
        self.sets.get(key, set()).discard(member)

    async def smembers(self, key: str) -> set[str]:
        return set(self.sets.get(key, set()))


def _compiled_rego(rule_id: str = "a" * 64 + ":4.2.b", package: str = "sebi.broking.circulars.x.clause_1") -> CompiledRego:
    return CompiledRego(rule_id=rule_id, package=package, rego_code=f"package {package}\n\ndefault allow = false\n", thresholds_compiled=1)


class TestOpaPublisherNamespacing:
    def test_canary_rule_id_and_package_are_namespaced(self) -> None:
        assert canary_opa_rule_id("c1", "rule-1") == "canary_c1_rule-1"
        assert canary_package("c1", "sebi.broking.x") == "canary.c1.sebi.broking.x"

    def test_hyphenated_canary_id_is_sanitized_in_package(self) -> None:
        # Rego package segments can't contain "-" -- canary_id is a uuid4,
        # which is all hyphens by construction.
        assert "-" not in canary_package("11111111-2222-3333-4444-555555555555", "sebi.x")

    @pytest.mark.asyncio
    async def test_publish_candidate_rewrites_package_line_and_puts_namespaced_id(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["path"] = request.url.path
            seen["body"] = request.content.decode()
            return httpx.Response(200)

        _patch_transport(monkeypatch, handler)
        opa = OPAEngine(base_url="http://opa.local:8181", timeout_seconds=2.0)
        publisher = CanaryOPAPublisher(opa)

        namespaced_rule_id, namespaced_package = await publisher.publish_candidate("c1", _compiled_rego())

        assert namespaced_rule_id == f"canary_c1_{'a' * 64}:4.2.b"
        assert seen["path"] == f"/v1/policies/{namespaced_rule_id}"
        assert f"package {namespaced_package}" in seen["body"]
        assert "package sebi.broking.circulars.x.clause_1" not in seen["body"]


class TestParityAnalyzer:
    def test_decision_from_opa_result(self) -> None:
        assert decision_from_opa_result(None) == Decision.FLAGGED
        assert decision_from_opa_result({"violations": ["x"]}) == Decision.DENY
        assert decision_from_opa_result({"violations": [], "allow": True}) == Decision.ALLOW

    @pytest.mark.asyncio
    async def test_compare_and_record_updates_running_stats(self) -> None:
        store = CanaryStore(_FakeRedis(), key_prefix="regengine:canary")
        run = CanaryRun(
            canary_id="c1", rule_id="rule-1", tenant_id="t1",
            production_package="sebi.x", candidate_package="canary.c1.sebi.x",
            candidate_opa_rule_id="canary_c1_rule-1", candidate_compiled_rule_id=1,
        )
        await store.create(run)
        analyzer = ParityAnalyzer(store, rollback_divergence_pct=0.5, rollback_min_sample_size=2)

        await analyzer.compare_and_record(
            "c1", "txn-1",
            production_result={"violations": []}, production_latency_ms=1.0, production_error=None,
            candidate_result={"violations": []}, candidate_latency_ms=2.0, candidate_error=None,
        )
        updated = await store.get("c1")
        assert updated.stats.total_compared == 1
        assert updated.stats.matched == 1
        assert updated.stats.diverged == 0

        await analyzer.compare_and_record(
            "c1", "txn-2",
            production_result={"violations": []}, production_latency_ms=1.0, production_error=None,
            candidate_result={"violations": ["shortfall"]}, candidate_latency_ms=2.0, candidate_error=None,
        )
        updated = await store.get("c1")
        assert updated.stats.total_compared == 2
        assert updated.stats.diverged == 1
        assert updated.stats.divergence_pct == pytest.approx(0.5)

    @pytest.mark.asyncio
    async def test_rollback_spike_ignored_below_min_sample_size(self) -> None:
        store = CanaryStore(_FakeRedis(), key_prefix="regengine:canary")
        run = CanaryRun(canary_id="c1", rule_id="r", tenant_id=None, production_package="p", candidate_package="c", candidate_opa_rule_id="cid", candidate_compiled_rule_id=1)
        await store.create(run)
        analyzer = ParityAnalyzer(store, rollback_divergence_pct=0.1, rollback_min_sample_size=10)

        await analyzer.compare_and_record("c1", "t1", production_result={"violations": []}, production_latency_ms=1, production_error=None, candidate_result={"violations": ["x"]}, candidate_latency_ms=1, candidate_error=None)
        assert await analyzer.check_rollback_spike("c1") == CanaryDecision.CONTINUE

    @pytest.mark.asyncio
    async def test_rollback_spike_detected_once_min_sample_size_and_threshold_cleared(self) -> None:
        store = CanaryStore(_FakeRedis(), key_prefix="regengine:canary")
        run = CanaryRun(canary_id="c1", rule_id="r", tenant_id=None, production_package="p", candidate_package="c", candidate_opa_rule_id="cid", candidate_compiled_rule_id=1)
        await store.create(run)
        analyzer = ParityAnalyzer(store, rollback_divergence_pct=0.4, rollback_min_sample_size=2)

        await analyzer.compare_and_record("c1", "t1", production_result={"violations": []}, production_latency_ms=1, production_error=None, candidate_result={"violations": ["x"]}, candidate_latency_ms=1, candidate_error=None)
        await analyzer.compare_and_record("c1", "t2", production_result={"violations": []}, production_latency_ms=1, production_error=None, candidate_result={"violations": []}, candidate_latency_ms=1, candidate_error=None)
        # 1/2 = 50% divergence, above the 40% bar, sample size (2) meets the minimum.
        assert await analyzer.check_rollback_spike("c1") == CanaryDecision.ROLLBACK


@pytest.mark.asyncio
class TestCanaryStore:
    async def test_create_get_list_active_round_trip(self) -> None:
        store = CanaryStore(_FakeRedis(), key_prefix="regengine:canary")
        run = CanaryRun(canary_id="c1", rule_id="r", tenant_id=None, production_package="p", candidate_package="c", candidate_opa_rule_id="cid", candidate_compiled_rule_id=1)
        await store.create(run)

        assert (await store.get("c1")).canary_id == "c1"
        assert [r.canary_id for r in await store.list_active()] == ["c1"]

    async def test_mark_promoted_removes_from_active_set(self) -> None:
        store = CanaryStore(_FakeRedis(), key_prefix="regengine:canary")
        await store.create(CanaryRun(canary_id="c1", rule_id="r", tenant_id=None, production_package="p", candidate_package="c", candidate_opa_rule_id="cid", candidate_compiled_rule_id=1))

        resolved = await store.mark_promoted("c1", "all good")
        assert resolved.status == CanaryStatus.PROMOTED
        assert await store.list_active() == []

    async def test_resolving_twice_raises(self) -> None:
        store = CanaryStore(_FakeRedis(), key_prefix="regengine:canary")
        await store.create(CanaryRun(canary_id="c1", rule_id="r", tenant_id=None, production_package="p", candidate_package="c", candidate_opa_rule_id="cid", candidate_compiled_rule_id=1))
        await store.mark_rolled_back("c1", "bad")
        with pytest.raises(ValueError):
            await store.mark_promoted("c1", "too late")


class _FakeOPAPublisher:
    def __init__(self) -> None:
        self.published: list[str] = []
        self.removed: list[str] = []

    async def publish_candidate(self, canary_id, candidate):
        rule_id = f"canary_{canary_id}_{candidate.rule_id}"
        package = f"canary.{canary_id}.{candidate.package}"
        self.published.append(rule_id)
        return rule_id, package

    async def remove_candidate(self, namespaced_rule_id: str) -> None:
        self.removed.append(namespaced_rule_id)


class _FakeSession:
    def __init__(self, compiled_rule: CompiledRule | None) -> None:
        self._rule = compiled_rule

    async def get(self, model, pk):
        return self._rule if self._rule and self._rule.id == pk else None


class _FakePolicyPublisher:
    def __init__(self) -> None:
        self.published: list[CompiledRule] = []

    async def publish_approved(self, compiled_rule, *, approved_by, entity_types=None):
        self.published.append(compiled_rule)


def _orchestrator(fake_publisher: _FakeOPAPublisher, redis=None, **overrides) -> CanaryOrchestrator:
    store = CanaryStore(redis or _FakeRedis(), key_prefix="regengine:canary")
    defaults = dict(promotion_max_divergence_pct=0.02, rollback_divergence_pct=0.10, rollback_min_sample_size=5, evaluation_window_seconds=86400)
    defaults.update(overrides)
    return CanaryOrchestrator(store, fake_publisher, **defaults)


@pytest.mark.asyncio
class TestCanaryOrchestrator:
    async def test_start_canary_publishes_and_creates_run(self) -> None:
        fake_publisher = _FakeOPAPublisher()
        orchestrator = _orchestrator(fake_publisher)
        production_rule = CompiledRule(id=1, clause_id=1, tenant_id="t1", rule_id="rule-1", rule_version=1, opa_package_name="sebi.broking.x", is_compiled=True, is_active=True)

        run = await orchestrator.start_canary(production_rule, _compiled_rego(), candidate_compiled_rule_id=2)

        assert run.status == CanaryStatus.RUNNING
        assert run.production_package == "sebi.broking.x"
        assert fake_publisher.published == [run.candidate_opa_rule_id]

    async def test_start_canary_rejects_unpublished_production_rule(self) -> None:
        orchestrator = _orchestrator(_FakeOPAPublisher())
        production_rule = CompiledRule(id=1, clause_id=1, tenant_id="t1", rule_id="rule-1", rule_version=1, opa_package_name=None)
        with pytest.raises(ValueError):
            await orchestrator.start_canary(production_rule, _compiled_rego(), candidate_compiled_rule_id=2)

    async def test_evaluate_window_continues_before_window_elapsed(self) -> None:
        redis = _FakeRedis()
        orchestrator = _orchestrator(_FakeOPAPublisher(), redis=redis, evaluation_window_seconds=86400)
        store = CanaryStore(redis, key_prefix="regengine:canary")
        await store.create(CanaryRun(canary_id="c1", rule_id="r", tenant_id=None, production_package="p", candidate_package="c", candidate_opa_rule_id="cid", candidate_compiled_rule_id=1))

        assert await orchestrator.evaluate_window("c1") == CanaryDecision.CONTINUE

    async def test_evaluate_window_promotes_after_window_elapsed_within_bar(self) -> None:
        import datetime as dt

        redis = _FakeRedis()
        orchestrator = _orchestrator(_FakeOPAPublisher(), redis=redis, evaluation_window_seconds=1, promotion_max_divergence_pct=0.5)
        store = CanaryStore(redis, key_prefix="regengine:canary")
        run = CanaryRun(canary_id="c1", rule_id="r", tenant_id=None, production_package="p", candidate_package="c", candidate_opa_rule_id="cid", candidate_compiled_rule_id=1, started_at=dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=10))
        await store.create(run)

        assert await orchestrator.evaluate_window("c1") == CanaryDecision.PROMOTE

    async def test_evaluate_window_ambiguous_divergence_after_window_continues(self) -> None:
        import datetime as dt

        redis = _FakeRedis()
        # 10% divergence: above the 2% promotion bar, but the rollback
        # check never even fires (min_sample_size=100 > 10 comparisons
        # made) -- isolates the "ambiguous, neither promote nor
        # rollback" outcome from a real rollback-spike decision.
        orchestrator = _orchestrator(_FakeOPAPublisher(), redis=redis, evaluation_window_seconds=1, promotion_max_divergence_pct=0.02, rollback_divergence_pct=0.5, rollback_min_sample_size=100)
        store = CanaryStore(redis, key_prefix="regengine:canary")
        run = CanaryRun(canary_id="c1", rule_id="r", tenant_id=None, production_package="p", candidate_package="c", candidate_opa_rule_id="cid", candidate_compiled_rule_id=1, started_at=dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=10))
        await store.create(run)
        analyzer = ParityAnalyzer(store, rollback_divergence_pct=0.5, rollback_min_sample_size=100)
        await analyzer.compare_and_record("c1", "t1", production_result={"violations": []}, production_latency_ms=1, production_error=None, candidate_result={"violations": ["x"]}, candidate_latency_ms=1, candidate_error=None)
        for i in range(9):
            await analyzer.compare_and_record("c1", f"t{i}", production_result={"violations": []}, production_latency_ms=1, production_error=None, candidate_result={"violations": []}, candidate_latency_ms=1, candidate_error=None)

        assert await orchestrator.evaluate_window("c1") == CanaryDecision.CONTINUE

    async def test_evaluate_window_rolls_back_on_spike_regardless_of_window(self) -> None:
        redis = _FakeRedis()
        orchestrator = _orchestrator(_FakeOPAPublisher(), redis=redis, evaluation_window_seconds=86400, rollback_divergence_pct=0.3, rollback_min_sample_size=1)
        store = CanaryStore(redis, key_prefix="regengine:canary")
        await store.create(CanaryRun(canary_id="c1", rule_id="r", tenant_id=None, production_package="p", candidate_package="c", candidate_opa_rule_id="cid", candidate_compiled_rule_id=1))
        analyzer = ParityAnalyzer(store, rollback_divergence_pct=0.3, rollback_min_sample_size=1)
        await analyzer.compare_and_record("c1", "t1", production_result={"violations": []}, production_latency_ms=1, production_error=None, candidate_result={"violations": ["x"]}, candidate_latency_ms=1, candidate_error=None)

        assert await orchestrator.evaluate_window("c1") == CanaryDecision.ROLLBACK

    async def test_rollback_removes_candidate_from_opa_and_marks_resolved(self) -> None:
        redis = _FakeRedis()
        fake_publisher = _FakeOPAPublisher()
        orchestrator = _orchestrator(fake_publisher, redis=redis)
        store = CanaryStore(redis, key_prefix="regengine:canary")
        await store.create(CanaryRun(canary_id="c1", rule_id="r", tenant_id=None, production_package="p", candidate_package="c", candidate_opa_rule_id="cid-1", candidate_compiled_rule_id=1))

        resolved = await orchestrator.rollback("c1", "spike")
        assert resolved.status == CanaryStatus.ROLLED_BACK
        assert fake_publisher.removed == ["cid-1"]

    async def test_rollback_is_idempotent_after_already_resolved(self) -> None:
        redis = _FakeRedis()
        fake_publisher = _FakeOPAPublisher()
        orchestrator = _orchestrator(fake_publisher, redis=redis)
        store = CanaryStore(redis, key_prefix="regengine:canary")
        await store.create(CanaryRun(canary_id="c1", rule_id="r", tenant_id=None, production_package="p", candidate_package="c", candidate_opa_rule_id="cid-1", candidate_compiled_rule_id=1))
        await orchestrator.rollback("c1", "first")

        resolved_again = await orchestrator.rollback("c1", "second")
        assert resolved_again.status == CanaryStatus.ROLLED_BACK
        assert fake_publisher.removed == ["cid-1"]  # not called twice

    async def test_promote_publishes_via_policy_publisher_and_removes_canary_copy(self) -> None:
        redis = _FakeRedis()
        fake_publisher = _FakeOPAPublisher()
        orchestrator = _orchestrator(fake_publisher, redis=redis)
        store = CanaryStore(redis, key_prefix="regengine:canary")
        await store.create(CanaryRun(canary_id="c1", rule_id="r", tenant_id=None, production_package="p", candidate_package="c", candidate_opa_rule_id="cid-1", candidate_compiled_rule_id=42))

        candidate_rule = CompiledRule(id=42, clause_id=1, tenant_id="t1", rule_id="rule-1", rule_version=2, opa_package_name="sebi.broking.x", rego_policy="package sebi.broking.x\n", is_compiled=True)
        session = _FakeSession(candidate_rule)
        policy_publisher = _FakePolicyPublisher()

        resolved = await orchestrator.promote("c1", session, policy_publisher)

        assert resolved.status == CanaryStatus.PROMOTED
        assert policy_publisher.published == [candidate_rule]
        assert fake_publisher.removed == ["cid-1"]

    async def test_promote_raises_when_candidate_rule_missing(self) -> None:
        redis = _FakeRedis()
        orchestrator = _orchestrator(_FakeOPAPublisher(), redis=redis)
        store = CanaryStore(redis, key_prefix="regengine:canary")
        await store.create(CanaryRun(canary_id="c1", rule_id="r", tenant_id=None, production_package="p", candidate_package="c", candidate_opa_rule_id="cid-1", candidate_compiled_rule_id=42))

        with pytest.raises(ValueError):
            await orchestrator.promote("c1", _FakeSession(None), _FakePolicyPublisher())


class _FakeOrchestrator:
    def __init__(self) -> None:
        self.rollback_calls: list[str] = []

    async def rollback(self, canary_id: str, reason: str) -> None:
        self.rollback_calls.append(canary_id)


@pytest.mark.asyncio
class TestShadowTrafficMirror:
    async def test_shadow_evaluate_records_comparison_and_never_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if "production" in request.url.path:
                return httpx.Response(200, json={"result": {"allow": True, "violations": []}})
            return httpx.Response(200, json={"result": {"allow": False, "violations": ["shortfall"]}})

        _patch_transport(monkeypatch, handler)
        opa = OPAEngine(base_url="http://opa.local:8181", timeout_seconds=2.0)
        redis = _FakeRedis()
        store = CanaryStore(redis, key_prefix="regengine:canary")
        run = CanaryRun(canary_id="c1", rule_id="r", tenant_id=None, production_package="production.pkg", candidate_package="candidate.pkg", candidate_opa_rule_id="cid-1", candidate_compiled_rule_id=1)
        await store.create(run)

        analyzer = ParityAnalyzer(store, rollback_divergence_pct=0.5, rollback_min_sample_size=1)
        fake_orchestrator = _FakeOrchestrator()
        mirror = ShadowTrafficMirror(opa, analyzer, fake_orchestrator)

        await mirror.shadow_evaluate(run, "txn-1", {"entity_type": "Stockbroker", "facts": {}})

        updated = await store.get("c1")
        assert updated.stats.total_compared == 1
        assert updated.stats.diverged == 1
        # divergence 100% >= 50% rollback bar, sample size 1 >= min 1 -> rollback triggered.
        assert fake_orchestrator.rollback_calls == ["c1"]

    async def test_shadow_evaluate_swallows_opa_errors(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, text="boom")

        _patch_transport(monkeypatch, handler)
        opa = OPAEngine(base_url="http://opa.local:8181", timeout_seconds=2.0)
        redis = _FakeRedis()
        store = CanaryStore(redis, key_prefix="regengine:canary")
        run = CanaryRun(canary_id="c1", rule_id="r", tenant_id=None, production_package="p", candidate_package="c", candidate_opa_rule_id="cid-1", candidate_compiled_rule_id=1)
        await store.create(run)
        analyzer = ParityAnalyzer(store, rollback_divergence_pct=0.99, rollback_min_sample_size=100)
        mirror = ShadowTrafficMirror(opa, analyzer, _FakeOrchestrator())

        await mirror.shadow_evaluate(run, "txn-1", {"entity_type": "Stockbroker", "facts": {}})  # must not raise

        updated = await store.get("c1")
        assert updated.stats.total_compared == 1
        assert updated.stats.matched == 1  # both sides errored -> both FLAGGED -> not diverged

    async def test_spawn_shadow_evaluation_schedules_a_task_per_run(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"result": {"allow": True, "violations": []}})

        _patch_transport(monkeypatch, handler)
        opa = OPAEngine(base_url="http://opa.local:8181", timeout_seconds=2.0)
        redis = _FakeRedis()
        store = CanaryStore(redis, key_prefix="regengine:canary")
        run = CanaryRun(canary_id="c1", rule_id="r", tenant_id=None, production_package="p", candidate_package="c", candidate_opa_rule_id="cid-1", candidate_compiled_rule_id=1)
        await store.create(run)
        analyzer = ParityAnalyzer(store, rollback_divergence_pct=0.5, rollback_min_sample_size=5)
        mirror = ShadowTrafficMirror(opa, analyzer, _FakeOrchestrator())

        import asyncio

        spawn_shadow_evaluation(mirror, [run], "txn-1", {"entity_type": "Stockbroker", "facts": {}})
        await asyncio.sleep(0.05)  # let the fire-and-forget task run

        updated = await store.get("c1")
        assert updated.stats.total_compared == 1
