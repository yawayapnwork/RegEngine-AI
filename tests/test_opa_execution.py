"""OPA Policy Execution test suite.

Two layers, both fully mocked -- no `opa` binary or server process is
started by these tests:

  1. `OPAEngine` against `httpx.MockTransport` -- verifies the HTTP
     contract (URLs, methods, status handling, undefined-vs-error
     distinction) without a live OPA server.
  2. `Evaluator` against hand-rolled `OPAEngine`/`PolicyRegistry`/`HITLQueue`
     doubles, driven by decision payloads shaped exactly like what a real
     compiled Rego module's `decision` object produces (see
     app.compiler.rego_compiler's module docstring) for a margin-check
     policy -- verifying the allow/deny/flagged reduction rules against
     mocked broker transaction payloads.
"""
from __future__ import annotations

import httpx
import pytest

from app.compiler.naming import metric_field_name, rego_package_name
from app.execution.evaluator import Evaluator
from app.execution.models import Decision, HITLCase, PolicyOutcome, SourceChannel, TransactionPayload
from app.execution.opa_engine import OPAEngine, OPAEngineError
from app.regulatory.taxonomy import Regulator

PACKAGE = rego_package_name(Regulator.SEBI, "broking", "SEBI/HO/MRD/2024/1", "2.1.b")
RULE_ID = "a" * 64 + ":2.1.b"
MARGIN_FIELD = metric_field_name("Upfront Margin", "%")  # "upfront_margin_pct"

_RealAsyncClient = httpx.AsyncClient  # captured before any monkeypatching below


def _patch_transport(monkeypatch: pytest.MonkeyPatch, handler) -> None:
    """Routes every httpx.AsyncClient OPAEngine constructs through a
    MockTransport, so `evaluate`/`publish_policy`/`remove_policy` exercise
    their real request-building/response-parsing code with no real socket.
    Must build from `_RealAsyncClient`, not `httpx.AsyncClient` (which this
    itself patches) -- constructing via the patched name would recurse."""
    def _fake_async_client(**kwargs):
        kwargs.pop("transport", None)
        return _RealAsyncClient(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _fake_async_client)


def _decision(*, allow: bool, margin_pct: float) -> dict:
    """The exact shape app.compiler.rego_compiler's generated `decision`
    object has for a "Upfront Margin >= 20%" rule, computed here in plain
    Python rather than by invoking a real OPA binary."""
    violated = margin_pct < 20
    return {
        "allow": allow,
        "violations": (
            [f"Upfront Margin {margin_pct}% violates required >= 20%"] if violated else []
        ),
        "rule_id": RULE_ID,
        "clause_number": "2.1.b",
        "circular_number": "SEBI/HO/MRD/2024/1",
        "obligation_type": "mandatory",
    }


def _transaction(**overrides) -> TransactionPayload:
    base = dict(
        transaction_id="TXN-001",
        entity_type="Stockbroker",
        facts={MARGIN_FIELD: 25.0},
        broker_id="BRK-001",
    )
    base.update(overrides)
    return TransactionPayload(**base)


# --------------------------------------------------------------------------
# OPAEngine: HTTP contract, mocked transport
# --------------------------------------------------------------------------


class TestOPAEngineEvaluate:
    @pytest.mark.asyncio
    async def test_evaluate_returns_parsed_result_on_200(self, monkeypatch):
        expected_result = _decision(allow=True, margin_pct=25.0)

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == f"/v1/data/{PACKAGE.replace('.', '/')}/decision"
            assert request.method == "POST"
            return httpx.Response(200, json={"result": expected_result})

        engine = OPAEngine("http://opa.local:8181", timeout_seconds=2.0)
        _patch_transport(monkeypatch, handler)

        result = await engine.evaluate(PACKAGE, {"entity_type": "Stockbroker", "facts": {MARGIN_FIELD: 25.0}})

        assert result == expected_result

    @pytest.mark.asyncio
    async def test_evaluate_returns_none_when_opa_reports_undefined(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={})  # no "result" key == undefined in OPA's data API

        engine = OPAEngine("http://opa.local:8181", timeout_seconds=2.0)
        _patch_transport(monkeypatch, handler)

        result = await engine.evaluate(PACKAGE, {"entity_type": "Stockbroker", "facts": {}})

        assert result is None

    @pytest.mark.asyncio
    async def test_evaluate_raises_on_non_200(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, text="internal error")

        engine = OPAEngine("http://opa.local:8181", timeout_seconds=2.0)
        _patch_transport(monkeypatch, handler)

        with pytest.raises(OPAEngineError):
            await engine.evaluate(PACKAGE, {"entity_type": "Stockbroker", "facts": {}})

    @pytest.mark.asyncio
    async def test_evaluate_raises_on_transport_failure(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused", request=request)

        engine = OPAEngine("http://opa.local:8181", timeout_seconds=2.0)
        _patch_transport(monkeypatch, handler)

        with pytest.raises(OPAEngineError):
            await engine.evaluate(PACKAGE, {"entity_type": "Stockbroker", "facts": {}})


class TestOPAEnginePublishRemove:
    @pytest.mark.asyncio
    async def test_publish_policy_puts_rego_source(self, monkeypatch):
        from app.compiler.models import CompiledRego

        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["method"] = request.method
            captured["path"] = request.url.path
            captured["body"] = request.content
            return httpx.Response(200)

        engine = OPAEngine("http://opa.local:8181", timeout_seconds=2.0)
        _patch_transport(monkeypatch, handler)

        compiled = CompiledRego(rule_id=RULE_ID, package=PACKAGE, rego_code="package x\n", thresholds_compiled=1)
        await engine.publish_policy(compiled)

        assert captured["method"] == "PUT"
        assert captured["path"] == f"/v1/policies/{RULE_ID}"
        assert captured["body"] == b"package x\n"

    @pytest.mark.asyncio
    async def test_publish_policy_raises_on_rejection(self, monkeypatch):
        from app.compiler.models import CompiledRego

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(400, text="rego_parse_error")

        engine = OPAEngine("http://opa.local:8181", timeout_seconds=2.0)
        _patch_transport(monkeypatch, handler)

        compiled = CompiledRego(rule_id=RULE_ID, package=PACKAGE, rego_code="not valid rego", thresholds_compiled=1)
        with pytest.raises(OPAEngineError):
            await engine.publish_policy(compiled)

    @pytest.mark.asyncio
    async def test_remove_policy_accepts_404_as_success(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.method == "DELETE"
            return httpx.Response(404)  # already gone -- not an error for removal

        engine = OPAEngine("http://opa.local:8181", timeout_seconds=2.0)
        _patch_transport(monkeypatch, handler)

        await engine.remove_policy(RULE_ID)  # must not raise


# --------------------------------------------------------------------------
# Evaluator: allow / deny / flagged reduction over mocked broker transactions
# --------------------------------------------------------------------------


class _FakeOPAEngine:
    def __init__(self, responses: dict[str, dict | None | Exception]) -> None:
        """`responses` maps package -> a canned decision dict, None
        (undefined), or an Exception instance to raise."""
        self._responses = responses
        self.calls: list[tuple[str, dict]] = []

    async def evaluate(self, package: str, input_doc: dict) -> dict | None:
        self.calls.append((package, input_doc))
        outcome = self._responses[package]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class _FakePolicyRegistry:
    def __init__(self, policies: dict[str, list[dict]]) -> None:
        """`policies` maps entity_type -> list of {"rule_id", "package"}."""
        self._policies = policies

    async def policies_for(self, entity_type: str) -> list[dict]:
        return self._policies.get(entity_type, [])


class _FakeHITLQueue:
    def __init__(self) -> None:
        self.enqueued: list[dict] = []

    async def enqueue(self, transaction: TransactionPayload, reason: str, matched_policies: list[PolicyOutcome]) -> HITLCase:
        case = HITLCase(case_id=f"case-{len(self.enqueued) + 1}", transaction=transaction, reason=reason, matched_policies=matched_policies)
        self.enqueued.append({"transaction_id": transaction.transaction_id, "reason": reason})
        return case


@pytest.fixture
def registry_single_policy() -> _FakePolicyRegistry:
    return _FakePolicyRegistry({"Stockbroker": [{"rule_id": RULE_ID, "package": PACKAGE}]})


class TestEvaluatorReduction:
    @pytest.mark.asyncio
    async def test_compliant_transaction_is_allowed(self, registry_single_policy):
        opa = _FakeOPAEngine({PACKAGE: _decision(allow=True, margin_pct=25.0)})
        hitl = _FakeHITLQueue()
        evaluator = Evaluator(opa_engine=opa, policy_registry=registry_single_policy, hitl_queue=hitl)

        result = await evaluator.evaluate_transaction(_transaction(facts={MARGIN_FIELD: 25.0}))

        assert result.decision == Decision.ALLOW
        assert result.hitl_case_id is None
        assert result.reasons == []
        assert hitl.enqueued == []
        assert result.latency_ms is not None and result.latency_ms >= 0

    @pytest.mark.asyncio
    async def test_violating_transaction_is_denied_with_reason(self, registry_single_policy):
        opa = _FakeOPAEngine({PACKAGE: _decision(allow=False, margin_pct=15.0)})
        hitl = _FakeHITLQueue()
        evaluator = Evaluator(opa_engine=opa, policy_registry=registry_single_policy, hitl_queue=hitl)

        result = await evaluator.evaluate_transaction(_transaction(facts={MARGIN_FIELD: 15.0}))

        assert result.decision == Decision.DENY
        assert any("15.0%" in r for r in result.reasons)
        assert hitl.enqueued == []  # a confirmed violation never needs a human, per the reduction rules

    @pytest.mark.asyncio
    async def test_missing_facts_key_is_flagged_and_enqueued_for_hitl(self, registry_single_policy):
        """OPA reports the decision as undefined (allow == None) when a
        required `facts` key is absent -- never an implicit allow or deny."""
        opa = _FakeOPAEngine({PACKAGE: None})
        hitl = _FakeHITLQueue()
        evaluator = Evaluator(opa_engine=opa, policy_registry=registry_single_policy, hitl_queue=hitl)

        result = await evaluator.evaluate_transaction(_transaction(facts={}))  # upfront_margin_pct missing

        assert result.decision == Decision.FLAGGED
        assert result.hitl_case_id == "case-1"
        assert len(hitl.enqueued) == 1
        assert hitl.enqueued[0]["transaction_id"] == "TXN-001"

    @pytest.mark.asyncio
    async def test_opa_unreachable_is_treated_as_flagged_not_a_hard_failure(self, registry_single_policy):
        """An OPA outage must degrade to a HITL-flagged decision, never an
        unhandled exception that would take down the evaluate endpoint."""
        opa = _FakeOPAEngine({PACKAGE: OPAEngineError("connection refused")})
        hitl = _FakeHITLQueue()
        evaluator = Evaluator(opa_engine=opa, policy_registry=registry_single_policy, hitl_queue=hitl)

        result = await evaluator.evaluate_transaction(_transaction())

        assert result.decision == Decision.FLAGGED
        assert result.hitl_case_id is not None

    @pytest.mark.asyncio
    async def test_no_applicable_policy_allows_without_hitl(self):
        registry = _FakePolicyRegistry({})  # nothing registered for any entity_type
        opa = _FakeOPAEngine({})
        hitl = _FakeHITLQueue()
        evaluator = Evaluator(opa_engine=opa, policy_registry=registry, hitl_queue=hitl)

        result = await evaluator.evaluate_transaction(_transaction(entity_type="AssetManager"))

        assert result.decision == Decision.ALLOW
        assert result.matched_policies == []
        assert hitl.enqueued == []
        assert opa.calls == []

    @pytest.mark.asyncio
    async def test_violation_wins_over_undefined_from_another_policy(self):
        """Most-restrictive-wins: one policy denies, another is undefined ->
        the confirmed DENY must not be diluted into a FLAGGED."""
        other_package = "sebi.circulars.sebi_ho_mrd_2024_1.clause_2_2"
        registry = _FakePolicyRegistry({
            "Stockbroker": [
                {"rule_id": RULE_ID, "package": PACKAGE},
                {"rule_id": "b" * 64 + ":2.2", "package": other_package},
            ]
        })
        opa = _FakeOPAEngine({
            PACKAGE: _decision(allow=False, margin_pct=10.0),
            other_package: None,
        })
        hitl = _FakeHITLQueue()
        evaluator = Evaluator(opa_engine=opa, policy_registry=registry, hitl_queue=hitl)

        result = await evaluator.evaluate_transaction(_transaction(facts={MARGIN_FIELD: 10.0}))

        assert result.decision == Decision.DENY
        assert hitl.enqueued == []

    @pytest.mark.asyncio
    async def test_evaluator_passes_entity_type_and_facts_as_opa_input(self, registry_single_policy):
        opa = _FakeOPAEngine({PACKAGE: _decision(allow=True, margin_pct=25.0)})
        hitl = _FakeHITLQueue()
        evaluator = Evaluator(opa_engine=opa, policy_registry=registry_single_policy, hitl_queue=hitl)

        txn = _transaction(facts={MARGIN_FIELD: 25.0}, source_channel=SourceChannel.SFTP_BATCH)
        await evaluator.evaluate_transaction(txn)

        assert opa.calls == [(PACKAGE, {"entity_type": "Stockbroker", "facts": {MARGIN_FIELD: 25.0}})]
