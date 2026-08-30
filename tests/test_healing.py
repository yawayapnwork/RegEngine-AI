"""Tests for app.healing: the self-healing policy repair loop.

`OPAEngine` is exercised against a real `httpx.MockTransport` (mirrors
tests/test_opa_execution.py's `_patch_transport` exactly), so
error-interception is tested against OPAEngine's real request/response
code, not a hand-rolled fake. Redis is faked throughout (mirrors this
repo's established `_FakeRedis` pattern -- see tests/test_incident.py).
No HUGGINGFACEHUB_API_TOKEN / crewai is used anywhere here -- every test drives
the deterministic fast path or the orchestrator's retry/tracking
mechanics directly, matching regengine-cli.py's `--offline-agents`
precedent for testing this pipeline without a live LLM.
"""
from __future__ import annotations

import httpx
import pytest

from app.compiler.models import CompiledRego
from app.config import Settings
from app.execution.opa_engine import OPAEngine, OPAEngineError
from app.healing import orchestrator as orchestrator_module
from app.healing.detectors import (
    attempt_publish_and_intercept,
    check_rego_structure,
    classify_opa_error_message,
    run_isolated_test_cases,
)
from app.healing.models import HealingOutcome, PolicyErrorType, PolicyFailure, RepairStrategy
from app.healing.orchestrator import SelfHealingLoop
from app.healing.repair_agent import RepairSuggestion, apply_deterministic_fixes, repair_policy
from app.healing.tracking import HealingAttemptTracker

_RealAsyncClient = httpx.AsyncClient


def _patch_transport(monkeypatch: pytest.MonkeyPatch, handler) -> None:
    def _fake_async_client(**kwargs):
        kwargs.pop("transport", None)
        return _RealAsyncClient(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _fake_async_client)


_GOOD_REGO = """package sebi.broking.circulars.sebi_ho_mirsd_2026_01.clause_3_2_1

import rego.v1

default allow := false

entity_matches if { input.entity_type == "Stockbroker" }

cond_0 if { input.facts.upfront_margin_pct >= 20 }

allow if {
    entity_matches
    cond_0
}
"""

_MISSING_DEFAULT_REGO = _GOOD_REGO.replace("default allow := false\n\n", "")
_UNBALANCED_REGO = _GOOD_REGO.replace("cond_0 if { input.facts.upfront_margin_pct >= 20 }", "cond_0 if { input.facts.upfront_margin_pct >= 20")

_GOOD_JSON_LOGIC = {"and": [{"==": [{"var": "entity_type"}, "Stockbroker"]}, {">=": [{"var": "facts.upfront_margin_pct"}, 20]}]}
_STRING_TYPED_JSON_LOGIC = {"and": [{"==": [{"var": "entity_type"}, "Stockbroker"]}, {">=": [{"var": "facts.upfront_margin_pct"}, "20"]}]}

_TEST_FIXTURES = [
    {"description": "21% margin is compliant", "input": {"entity_type": "Stockbroker", "facts": {"upfront_margin_pct": 21}}, "expect_allow": True},
    {"description": "15% margin is non-compliant", "input": {"entity_type": "Stockbroker", "facts": {"upfront_margin_pct": 15}}, "expect_allow": False},
]


def _base_failure(**overrides) -> PolicyFailure:
    defaults = dict(
        rule_id="a" * 64 + ":3.2.1",
        circular_number="SEBI/HO/MIRSD/2026/01",
        clause_number="3.2.1",
        source_clause_text="Every stockbroker shall maintain upfront margin of not less than 20% of the transaction value.",
        rego_code=_GOOD_REGO,
        json_logic=_GOOD_JSON_LOGIC,
        package="sebi.broking.circulars.sebi_ho_mirsd_2026_01.clause_3_2_1",
        data_schema={"entity_type": "string", "facts.upfront_margin_pct": "number"},
        violation_message_template="Upfront Margin is {facts.upfront_margin_pct}%, required to be >= 20%",
        thresholds_compiled=1,
        error_type=PolicyErrorType.COMPILE_ERROR,
        error_message="synthetic failure",
    )
    defaults.update(overrides)
    return PolicyFailure(**defaults)


class _FakeRedis:
    """Implements just the subset of redis.asyncio.Redis's API
    HealingAttemptTracker calls."""

    def __init__(self) -> None:
        self.strings: dict[str, str] = {}
        self.lists: dict[str, list[str]] = {}

    async def get(self, key: str) -> str | None:
        return self.strings.get(key)

    async def incr(self, key: str) -> int:
        self.strings[key] = str(int(self.strings.get(key, "0")) + 1)
        return int(self.strings[key])

    async def lpush(self, key: str, value: str) -> None:
        self.lists.setdefault(key, []).insert(0, value)

    async def ltrim(self, key: str, start: int, end: int) -> None:
        lst = self.lists.get(key, [])
        self.lists[key] = lst[start : end + 1]

    async def lrange(self, key: str, start: int, end: int) -> list[str]:
        lst = self.lists.get(key, [])
        return lst[start : end + 1 if end != -1 else None]

    async def delete(self, key: str) -> None:
        self.strings.pop(key, None)
        self.lists.pop(key, None)


# --------------------------------------------------------------------------
# detectors
# --------------------------------------------------------------------------


class TestClassifyOpaErrorMessage:
    def test_parse_error_is_syntax(self):
        assert classify_opa_error_message("1 error occurred: policy.rego:4: rego_parse_error: unexpected assign token") == PolicyErrorType.SYNTAX_ERROR

    def test_type_error_is_compile_error(self):
        assert classify_opa_error_message("rego_type_error: match error") == PolicyErrorType.COMPILE_ERROR

    def test_unsafe_var_is_compile_error(self):
        assert classify_opa_error_message("rego_unsafe_var_error: var x is unsafe") == PolicyErrorType.COMPILE_ERROR

    def test_unrecognized_message_falls_back_to_compile_error(self):
        assert classify_opa_error_message("something OPA has never said before") == PolicyErrorType.COMPILE_ERROR


class TestCheckRegoStructure:
    def test_well_formed_rego_has_no_issues(self):
        assert check_rego_structure(_GOOD_REGO) == []

    def test_missing_default_allow_is_flagged(self):
        issues = check_rego_structure(_MISSING_DEFAULT_REGO)
        assert any("default allow" in i for i in issues)

    def test_unbalanced_braces_are_flagged(self):
        issues = check_rego_structure(_UNBALANCED_REGO)
        assert any("Unclosed" in i or "Unbalanced" in i for i in issues)

    def test_missing_package_is_flagged(self):
        issues = check_rego_structure(_GOOD_REGO.replace("package sebi.broking.circulars.sebi_ho_mirsd_2026_01.clause_3_2_1\n\n", ""))
        assert any("package" in i for i in issues)


class TestRunIsolatedTestCases:
    def test_valid_ast_passes_correct_fixtures(self):
        results = run_isolated_test_cases(_GOOD_JSON_LOGIC, _TEST_FIXTURES)
        assert all(r.passed for r in results)

    def test_malformed_ast_fails_every_fixture(self):
        results = run_isolated_test_cases({"bad": "shape", "two": "keys"}, _TEST_FIXTURES)
        assert len(results) == len(_TEST_FIXTURES)
        assert all(not r.passed for r in results)
        assert all("structural validation" in (r.detail or "") for r in results)

    def test_unsupported_operator_is_a_runtime_crash(self):
        results = run_isolated_test_cases({"or": [{"==": [1, 1]}]}, _TEST_FIXTURES)
        assert all(not r.passed for r in results)
        assert all("Runtime evaluation crash" in (r.detail or "") for r in results)

    def test_wrong_expected_outcome_fails_with_detail(self):
        results = run_isolated_test_cases(_GOOD_JSON_LOGIC, [{"description": "wrong expectation", "input": {"entity_type": "Stockbroker", "facts": {"upfront_margin_pct": 21}}, "expect_allow": False}])
        assert results[0].passed is False
        assert "Expected allow=False" in results[0].detail


@pytest.mark.asyncio
class TestAttemptPublishAndIntercept:
    async def test_clean_publish_returns_none(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200)

        _patch_transport(monkeypatch, handler)
        opa = OPAEngine(base_url="http://opa.local:8181", timeout_seconds=2.0)
        compiled = CompiledRego(rule_id="r1", package="sebi.broking.x", rego_code=_GOOD_REGO, thresholds_compiled=1)

        failure = await attempt_publish_and_intercept(opa, compiled, circular_number="C", clause_number="1", source_clause_text="text")
        assert failure is None

    async def test_rejection_builds_classified_policy_failure(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(400, text="1 error occurred: policy.rego:4: rego_parse_error: unexpected assign token")

        _patch_transport(monkeypatch, handler)
        opa = OPAEngine(base_url="http://opa.local:8181", timeout_seconds=2.0)
        compiled = CompiledRego(rule_id="r1", package="sebi.broking.x", rego_code=_MISSING_DEFAULT_REGO, thresholds_compiled=1)

        failure = await attempt_publish_and_intercept(opa, compiled, circular_number="C", clause_number="1", source_clause_text="the legal text")
        assert failure is not None
        assert failure.error_type == PolicyErrorType.SYNTAX_ERROR
        assert "rego_parse_error" in failure.error_message
        assert failure.source_clause_text == "the legal text"
        assert failure.stack_trace  # a real formatted traceback, not empty


# --------------------------------------------------------------------------
# repair_agent (deterministic fast path)
# --------------------------------------------------------------------------


class TestApplyDeterministicFixes:
    def test_injects_missing_default_allow(self):
        failure = _base_failure(error_type=PolicyErrorType.SYNTAX_ERROR, rego_code=_MISSING_DEFAULT_REGO)
        suggestion = apply_deterministic_fixes(failure)
        assert suggestion.can_repair is True
        assert "default allow := false" in suggestion.repaired_rego
        assert check_rego_structure(suggestion.repaired_rego) == []

    def test_coerces_numeric_string_in_json_logic(self):
        failure = _base_failure(error_type=PolicyErrorType.INVALID_JSON_LOGIC_TYPE, json_logic=_STRING_TYPED_JSON_LOGIC)
        suggestion = apply_deterministic_fixes(failure)
        assert suggestion.can_repair is True
        assert suggestion.repaired_json_logic["and"][1][">="][1] == 20
        assert isinstance(suggestion.repaired_json_logic["and"][1][">="][1], int)

    def test_declines_when_no_known_fix_applies(self):
        failure = _base_failure(error_type=PolicyErrorType.RUNTIME_CRASH, error_message="something novel")
        suggestion = apply_deterministic_fixes(failure)
        assert suggestion.can_repair is False
        assert suggestion.repair_notes

    def test_declines_unbalanced_braces_rather_than_guessing(self):
        failure = _base_failure(error_type=PolicyErrorType.SYNTAX_ERROR, rego_code=_UNBALANCED_REGO)
        suggestion = apply_deterministic_fixes(failure)
        assert suggestion.can_repair is False


class TestRepairPolicyRouting:
    def test_uses_deterministic_fix_without_touching_llm(self):
        failure = _base_failure(error_type=PolicyErrorType.SYNTAX_ERROR, rego_code=_MISSING_DEFAULT_REGO)
        suggestion, strategy = repair_policy(failure, Settings(hf_api_token=None))
        assert strategy == RepairStrategy.DETERMINISTIC_FIX
        assert suggestion.can_repair is True

    def test_unfixable_without_api_key_when_no_deterministic_fix_applies(self):
        failure = _base_failure(error_type=PolicyErrorType.RUNTIME_CRASH, error_message="novel failure")
        suggestion, strategy = repair_policy(failure, Settings(hf_api_token=None))
        assert strategy == RepairStrategy.UNFIXABLE
        assert suggestion.can_repair is False
        assert "HUGGINGFACEHUB_API_TOKEN" in suggestion.repair_notes


# --------------------------------------------------------------------------
# tracking
# --------------------------------------------------------------------------


@pytest.mark.asyncio
class TestHealingAttemptTracker:
    async def test_record_attempt_increments_count_and_stores_history(self):
        from app.healing.models import RepairAttempt

        tracker = HealingAttemptTracker(_FakeRedis(), "test:healing")
        attempt = RepairAttempt(attempt_number=1, strategy=RepairStrategy.DETERMINISTIC_FIX, repair_notes="fixed it", passed_tests=True)

        count = await tracker.record_attempt("rule-1", attempt)
        assert count == 1
        assert await tracker.get_attempt_count("rule-1") == 1

        history = await tracker.get_history("rule-1")
        assert len(history) == 1
        assert history[0].repair_notes == "fixed it"

    async def test_reset_clears_count_and_history(self):
        from app.healing.models import RepairAttempt

        tracker = HealingAttemptTracker(_FakeRedis(), "test:healing")
        await tracker.record_attempt("rule-2", RepairAttempt(attempt_number=1, strategy=RepairStrategy.UNFIXABLE, repair_notes="nope"))
        await tracker.reset("rule-2")
        assert await tracker.get_attempt_count("rule-2") == 0
        assert await tracker.get_history("rule-2") == []

    async def test_history_is_capped_at_max_history(self):
        from app.healing.models import RepairAttempt

        tracker = HealingAttemptTracker(_FakeRedis(), "test:healing", max_history=2)
        for i in range(5):
            await tracker.record_attempt("rule-3", RepairAttempt(attempt_number=i + 1, strategy=RepairStrategy.UNFIXABLE, repair_notes=f"attempt {i}"))
        assert await tracker.get_attempt_count("rule-3") == 5  # counter is independent of the capped list
        assert len(await tracker.get_history("rule-3")) == 2


# --------------------------------------------------------------------------
# orchestrator (the Requirement-3 retry loop)
# --------------------------------------------------------------------------


@pytest.mark.asyncio
class TestSelfHealingLoop:
    async def test_heals_syntax_error_on_first_attempt(self):
        tracker = HealingAttemptTracker(_FakeRedis(), "test:healing")
        loop = SelfHealingLoop(tracker, Settings(hf_api_token=None, policy_self_healing_max_retries=3))
        failure = _base_failure(error_type=PolicyErrorType.SYNTAX_ERROR, rego_code=_MISSING_DEFAULT_REGO)

        result = await loop.heal(failure, _TEST_FIXTURES)

        assert result.outcome == HealingOutcome.HEALED
        assert len(result.attempts) == 1
        assert result.attempts[0].strategy == RepairStrategy.DETERMINISTIC_FIX
        assert result.final_compilation is not None
        assert result.final_compilation.compiled is True
        assert "default allow" in result.final_compilation.rego.rego_code
        assert result.hitl_flag is not None
        assert result.hitl_flag.severity.value == "advisory"

    async def test_heals_invalid_json_logic_type(self):
        tracker = HealingAttemptTracker(_FakeRedis(), "test:healing")
        loop = SelfHealingLoop(tracker, Settings(hf_api_token=None))
        failure = _base_failure(error_type=PolicyErrorType.INVALID_JSON_LOGIC_TYPE, json_logic=_STRING_TYPED_JSON_LOGIC)

        result = await loop.heal(failure, _TEST_FIXTURES)

        assert result.outcome == HealingOutcome.HEALED
        assert result.final_compilation.json_logic.logic["and"][1][">="][1] == 20

    async def test_escalates_unfixable_without_burning_full_retry_budget(self):
        tracker = HealingAttemptTracker(_FakeRedis(), "test:healing")
        loop = SelfHealingLoop(tracker, Settings(hf_api_token=None, policy_self_healing_max_retries=3))
        failure = _base_failure(error_type=PolicyErrorType.RUNTIME_CRASH, error_message="novel, no known fix")

        result = await loop.heal(failure, _TEST_FIXTURES)

        assert result.outcome == HealingOutcome.ESCALATED_UNFIXABLE
        assert len(result.attempts) == 1  # declined immediately, not retried 3 times against an unchanged input
        assert result.final_compilation is None

    async def test_exhausts_max_retries_when_repairs_never_pass_tests(self, monkeypatch):
        """A repair strategy that keeps claiming success but never
        actually passes the isolated test cases must stop at exactly
        `policy_self_healing_max_retries` attempts -- proven here by
        monkeypatching `repair_policy` itself so this test isolates the
        LOOP's retry/tracking mechanics from the repair heuristics
        already covered above."""

        def _always_wrong_fix(failure, settings, prior_attempts=None):
            return RepairSuggestion(can_repair=True, repaired_json_logic=_GOOD_JSON_LOGIC, repair_notes="looks fixed but isn't"), RepairStrategy.DETERMINISTIC_FIX

        monkeypatch.setattr(orchestrator_module, "repair_policy", _always_wrong_fix)

        tracker = HealingAttemptTracker(_FakeRedis(), "test:healing")
        loop = SelfHealingLoop(tracker, Settings(hf_api_token=None, policy_self_healing_max_retries=3))
        failure = _base_failure(error_type=PolicyErrorType.COMPILE_ERROR)

        # Fixtures that _GOOD_JSON_LOGIC can never satisfy, so every attempt fails its tests.
        impossible_fixtures = [{"description": "impossible", "input": {"entity_type": "Stockbroker", "facts": {"upfront_margin_pct": 21}}, "expect_allow": False}]

        result = await loop.heal(failure, impossible_fixtures)

        assert result.outcome == HealingOutcome.ESCALATED_MAX_RETRIES
        assert len(result.attempts) == 3
        assert all(not a.passed_tests for a in result.attempts)
        assert await tracker.get_attempt_count(failure.rule_id) == 0  # reset() ran on the unsuccessful conclusion


# --------------------------------------------------------------------------
# tasks (Celery wrapper) -- mirrors tests/test_resilience_tasks.py's
# `task.apply()` + monkeypatched `route_to_dlq_sync` convention exactly.
# --------------------------------------------------------------------------


class TestSelfHealPolicyTask:
    def test_disabled_flag_short_circuits_straight_to_dlq(self, monkeypatch):
        import app.healing.tasks as mod

        monkeypatch.setattr(mod, "get_settings", lambda: Settings(policy_self_healing_enabled=False))
        dlq_calls = []
        monkeypatch.setattr(mod, "route_to_dlq_sync", lambda **kw: dlq_calls.append(kw))

        failure_dict = _base_failure().model_dump(mode="json")
        result = mod.self_heal_policy_task.apply(args=[failure_dict, _TEST_FIXTURES]).get()

        assert result["outcome"] == "escalated_max_retries"
        assert len(dlq_calls) == 1
        assert dlq_calls[0]["category"].value == "policy_self_heal_exhausted"

    def test_healed_outcome_does_not_touch_the_dlq(self, monkeypatch):
        import app.healing.tasks as mod
        from app.healing.models import HealingOutcome as _Outcome
        from app.healing.models import SelfHealingResult

        monkeypatch.setattr(mod, "get_settings", lambda: Settings(policy_self_healing_enabled=True))
        dlq_calls = []
        monkeypatch.setattr(mod, "route_to_dlq_sync", lambda **kw: dlq_calls.append(kw))

        async def _fake_run_heal(failure_dict, test_fixtures):
            return SelfHealingResult(rule_id=failure_dict["rule_id"], outcome=_Outcome.HEALED, attempts=[])

        monkeypatch.setattr(mod, "_run_heal", _fake_run_heal)

        failure_dict = _base_failure().model_dump(mode="json")
        result = mod.self_heal_policy_task.apply(args=[failure_dict, _TEST_FIXTURES]).get()

        assert result["outcome"] == "healed"
        assert dlq_calls == []

    def test_exhausted_outcome_routes_to_dlq_with_attempt_count(self, monkeypatch):
        import app.healing.tasks as mod
        from app.healing.models import HealingOutcome as _Outcome
        from app.healing.models import RepairAttempt, SelfHealingResult

        monkeypatch.setattr(mod, "get_settings", lambda: Settings(policy_self_healing_enabled=True))
        dlq_calls = []
        monkeypatch.setattr(mod, "route_to_dlq_sync", lambda **kw: dlq_calls.append(kw))

        async def _fake_run_heal(failure_dict, test_fixtures):
            attempts = [RepairAttempt(attempt_number=i + 1, strategy=RepairStrategy.UNFIXABLE, repair_notes="nope") for i in range(3)]
            return SelfHealingResult(rule_id=failure_dict["rule_id"], outcome=_Outcome.ESCALATED_MAX_RETRIES, attempts=attempts)

        monkeypatch.setattr(mod, "_run_heal", _fake_run_heal)

        failure_dict = _base_failure().model_dump(mode="json")
        result = mod.self_heal_policy_task.apply(args=[failure_dict, _TEST_FIXTURES]).get()

        assert result["outcome"] == "escalated_max_retries"
        assert len(dlq_calls) == 1
        assert dlq_calls[0]["attempt_count"] == 3
        assert dlq_calls[0]["payload"]["failure"]["rule_id"] == failure_dict["rule_id"]
