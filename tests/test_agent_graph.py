"""Tests for the dynamic agent graph (app.agents.graph): complexity
detection, Redis state recording, and the graph's routing/fallback/
revision topology run end-to-end through LangGraph's real engine with
stub node functions (no CrewAI/Hugging Face call needed to verify the WIRING
is correct -- app.agents.graph.nodes's actual CrewAI-calling logic is
exercised indirectly via tests/test_agent_pipeline.py's existing coverage
of app.agents.crew, which every graph node delegates to).
"""
from __future__ import annotations

import pytest

from app.agents.graph.complexity_router import detect_complexity
from app.agents.graph.edges import (
    END_MARKER,
    NODE_AUDIT,
    NODE_FALLBACK_EXTRACTION,
    NODE_QUANTITATIVE_PARSING,
    NODE_REFERENCE_RESOLUTION,
    NODE_STANDARD_EXTRACTION,
    route_after_audit,
    route_by_complexity,
    route_confidence_gate,
)
from app.agents.graph.state import ComplexityRoute
from app.agents.graph.state_store import GraphExecutionStateStore


class TestComplexityRouter:
    def test_simple_clause_routes_standard(self) -> None:
        text = "Every stock broker shall maintain upfront margin of not less than 20% of the transaction value."
        flags = detect_complexity(text, own_clause_number="4.2.b")
        assert flags.route == ComplexityRoute.STANDARD
        assert not flags.has_math_formulas
        assert not flags.has_cross_references

    def test_formula_clause_routes_quantitative(self) -> None:
        text = (
            "The Capital to Risk-Weighted Assets Ratio shall be calculated using the formula: "
            "CRAR = (Tier I Capital + Tier II Capital) / Risk-Weighted Assets, and shall not be less than 11.5%."
        )
        flags = detect_complexity(text, own_clause_number="2.1")
        assert flags.route == ComplexityRoute.QUANTITATIVE
        assert flags.math_signals

    def test_cross_reference_clause_routes_reference_resolution(self) -> None:
        text = (
            "The limits specified in clause 4.1 shall apply, read with the conditions in "
            "clause 3.2.1 and Annexure B of this circular."
        )
        flags = detect_complexity(text, own_clause_number="5.1")
        assert flags.route == ComplexityRoute.REFERENCE_RESOLUTION
        assert flags.cross_reference_signals

    def test_math_takes_precedence_over_cross_reference_when_both_present(self) -> None:
        text = (
            "The exposure limit shall be calculated using the formula: Limit = Net Worth * 0.25, "
            "read with clause 3.2.1 and clause 4.1."
        )
        flags = detect_complexity(text, own_clause_number="6.1")
        assert flags.has_math_formulas and flags.has_cross_references
        assert flags.route == ComplexityRoute.QUANTITATIVE

    def test_single_incidental_clause_mention_does_not_trigger_cross_reference(self) -> None:
        text = "Unlike clause 2.1, this provision applies to portfolio managers only."
        flags = detect_complexity(text, own_clause_number="2.2")
        assert not flags.has_cross_references

    def test_own_clause_number_excluded_from_reference_count(self) -> None:
        text = "3.2.1 The broker shall comply with this clause 3.2.1 in full."
        flags = detect_complexity(text, own_clause_number="3.2.1")
        assert not flags.has_cross_references


class TestGraphRoutingEdges:
    def _settings_dict(self, **overrides) -> dict:
        base = {"agent_confidence_threshold": 0.85, "agent_max_fallback_attempts": 2}
        base.update(overrides)
        return base

    def test_route_by_complexity_maps_each_route_to_its_node(self) -> None:
        assert route_by_complexity({"route_taken": "standard"}) == NODE_STANDARD_EXTRACTION
        assert route_by_complexity({"route_taken": "quantitative"}) == NODE_QUANTITATIVE_PARSING
        assert route_by_complexity({"route_taken": "reference_resolution"}) == NODE_REFERENCE_RESOLUTION

    def test_confidence_gate_triggers_fallback_below_threshold(self) -> None:
        state = {"settings_dict": self._settings_dict(), "extraction_confidence": 0.60, "fallback_count": 0}
        assert route_confidence_gate(state) == NODE_FALLBACK_EXTRACTION

    def test_confidence_gate_proceeds_to_audit_above_threshold(self) -> None:
        state = {"settings_dict": self._settings_dict(), "extraction_confidence": 0.90, "fallback_count": 0}
        assert route_confidence_gate(state) == NODE_AUDIT

    def test_confidence_gate_stops_looping_once_fallback_budget_exhausted(self) -> None:
        state = {"settings_dict": self._settings_dict(agent_max_fallback_attempts=2), "extraction_confidence": 0.5, "fallback_count": 2}
        assert route_confidence_gate(state) == NODE_AUDIT

    def test_route_after_audit_loops_back_on_needs_revision(self) -> None:
        state = {"audit_result": {"verdict": "needs_revision"}, "revision_round": 0, "route_taken": "reference_resolution"}
        assert route_after_audit(state) == NODE_REFERENCE_RESOLUTION

    def test_route_after_audit_ends_on_approved(self) -> None:
        state = {"audit_result": {"verdict": "approved"}, "revision_round": 0, "route_taken": "standard"}
        assert route_after_audit(state) == END_MARKER

    def test_route_after_audit_ends_once_revision_budget_exhausted(self) -> None:
        state = {"audit_result": {"verdict": "needs_revision"}, "revision_round": 2, "route_taken": "standard"}
        assert route_after_audit(state) == END_MARKER


@pytest.mark.asyncio
class TestGraphExecutionEndToEnd:
    """Runs the REAL app.agents.graph.edges routing functions through
    LangGraph's actual engine, with trivial stub node functions standing
    in for the CrewAI-calling nodes -- proves the graph TOPOLOGY (dynamic
    routing, fallback loop, revision loop) is wired correctly, independent
    of whether CrewAI/Hugging Face are available."""

    async def _build_stub_graph(self, quant_confidences: list[float]):
        from langgraph.graph import END, StateGraph

        from app.agents.graph.state import AgentGraphState

        calls: list[str] = []
        confidences = iter(quant_confidences)

        async def classify(state):
            calls.append("classify")
            return {"route_taken": "quantitative", "node_history": ["classify"]}

        async def quant(state):
            calls.append("quant" if not state.get("used_fallback") else "quant_after_fallback")
            return {"extracted_rule": {"x": 1}, "extraction_confidence": next(confidences)}

        async def standard(state):
            calls.append("standard")
            return {"extracted_rule": {"x": 1}, "extraction_confidence": 0.95}

        async def ref(state):
            calls.append("ref")
            return {"extracted_rule": {"x": 1}, "extraction_confidence": 0.95}

        async def fallback(state):
            calls.append("fallback")
            return {
                "extracted_rule": {"x": 1}, "extraction_confidence": next(confidences),
                "fallback_count": state.get("fallback_count", 0) + 1, "used_fallback": True,
            }

        async def audit(state):
            calls.append("audit")
            return {"audit_result": {"verdict": "approved"}}

        graph = StateGraph(AgentGraphState)
        graph.add_node("classify_complexity", classify)
        graph.add_node(NODE_STANDARD_EXTRACTION, standard)
        graph.add_node(NODE_QUANTITATIVE_PARSING, quant)
        graph.add_node(NODE_REFERENCE_RESOLUTION, ref)
        graph.add_node(NODE_FALLBACK_EXTRACTION, fallback)
        graph.add_node(NODE_AUDIT, audit)
        graph.set_entry_point("classify_complexity")
        graph.add_conditional_edges("classify_complexity", route_by_complexity, {
            NODE_STANDARD_EXTRACTION: NODE_STANDARD_EXTRACTION,
            NODE_QUANTITATIVE_PARSING: NODE_QUANTITATIVE_PARSING,
            NODE_REFERENCE_RESOLUTION: NODE_REFERENCE_RESOLUTION,
        })
        for n in (NODE_STANDARD_EXTRACTION, NODE_QUANTITATIVE_PARSING, NODE_REFERENCE_RESOLUTION, NODE_FALLBACK_EXTRACTION):
            graph.add_conditional_edges(n, route_confidence_gate, {NODE_FALLBACK_EXTRACTION: NODE_FALLBACK_EXTRACTION, NODE_AUDIT: NODE_AUDIT})
        graph.add_conditional_edges(NODE_AUDIT, route_after_audit, {
            NODE_STANDARD_EXTRACTION: NODE_STANDARD_EXTRACTION,
            NODE_QUANTITATIVE_PARSING: NODE_QUANTITATIVE_PARSING,
            NODE_REFERENCE_RESOLUTION: NODE_REFERENCE_RESOLUTION,
            END_MARKER: END,
        })
        return graph.compile(), calls

    async def test_high_confidence_skips_fallback_entirely(self) -> None:
        compiled, calls = await self._build_stub_graph(quant_confidences=[0.95])
        initial = {"settings_dict": {"agent_confidence_threshold": 0.85, "agent_max_fallback_attempts": 2}, "fallback_count": 0, "revision_round": 0}
        final = await compiled.ainvoke(initial)
        assert calls == ["classify", "quant", "audit"]
        assert final["extraction_confidence"] == 0.95
        assert not final.get("used_fallback")

    async def test_low_confidence_triggers_exactly_one_fallback_round(self) -> None:
        compiled, calls = await self._build_stub_graph(quant_confidences=[0.5, 0.99])
        initial = {"settings_dict": {"agent_confidence_threshold": 0.85, "agent_max_fallback_attempts": 2}, "fallback_count": 0, "revision_round": 0}
        final = await compiled.ainvoke(initial)
        assert calls == ["classify", "quant", "fallback", "audit"]
        assert final["extraction_confidence"] == 0.99
        assert final["used_fallback"] is True
        assert final["fallback_count"] == 1

    async def test_persistently_low_confidence_stops_at_fallback_budget(self) -> None:
        compiled, calls = await self._build_stub_graph(quant_confidences=[0.5, 0.5, 0.5])
        initial = {"settings_dict": {"agent_confidence_threshold": 0.85, "agent_max_fallback_attempts": 2}, "fallback_count": 0, "revision_round": 0}
        final = await compiled.ainvoke(initial)
        # classify -> quant (0.5, fallback) -> fallback (0.5, still below threshold,
        # fallback_count=1 < 2, fallback again) -> fallback (0.5, fallback_count=2,
        # budget exhausted) -> audit
        assert calls == ["classify", "quant", "fallback", "fallback", "audit"]
        assert final["fallback_count"] == 2
        assert final["extraction_confidence"] == 0.5


@pytest.mark.asyncio
class TestGraphExecutionStateStore:
    async def test_record_and_read_back_node_history(self) -> None:
        redis_client = _FakeRedis()
        store = GraphExecutionStateStore(redis_client, "regengine:agent_graph", 3600)

        await store.record_node_execution(
            "run-1", "classify_complexity", route_taken="quantitative",
            extra={"math_signals": ["formula phrase: 'the formula'"]},
        )
        await store.record_node_execution(
            "run-1", "quantitative_parsing", route_taken="quantitative",
            confidence_score=0.6, token_usage={"input_tokens": 100, "output_tokens": 50}, duration_ms=1234.5,
        )

        history = await store.get_node_history("run-1")
        assert [h["node_name"] for h in history] == ["classify_complexity", "quantitative_parsing"]
        assert history[1]["confidence_score"] == 0.6
        assert history[1]["token_usage"] == {"input_tokens": 100, "output_tokens": 50}

        summary = await store.get_run_summary("run-1")
        assert summary["last_node"] == "quantitative_parsing"
        assert summary["route_taken"] == "quantitative"
        assert summary["last_confidence_score"] == "0.6"

    async def test_recording_failure_never_raises(self) -> None:
        class _BrokenRedis:
            def pipeline(self):
                raise RuntimeError("Redis is down")

        store = GraphExecutionStateStore(_BrokenRedis(), "regengine:agent_graph", 3600)
        # Must not raise -- see state_store.py's docstring: a
        # state-recording failure must never abort the extraction/audit
        # work in progress.
        await store.record_node_execution("run-1", "classify_complexity")


class _FakeRedis:
    """Minimal async-Redis-pipeline fake -- same pattern as
    tests/test_incident.py's _FakeRedis, extended with a pipeline()."""

    def __init__(self) -> None:
        self.lists: dict[str, list[str]] = {}
        self.hashes: dict[str, dict[str, str]] = {}

    def pipeline(self):
        return _FakePipeline(self)

    async def hgetall(self, key: str) -> dict[str, str]:
        return dict(self.hashes.get(key, {}))

    async def lrange(self, key: str, start: int, end: int) -> list[str]:
        lst = self.lists.get(key, [])
        return lst[start : end + 1 if end != -1 else None]


class _FakePipeline:
    def __init__(self, redis: _FakeRedis) -> None:
        self._redis = redis
        self._ops: list[tuple[str, tuple]] = []

    def rpush(self, key: str, value: str) -> "_FakePipeline":
        self._ops.append(("rpush", (key, value)))
        return self

    def expire(self, key: str, seconds: int) -> "_FakePipeline":
        self._ops.append(("expire", (key, seconds)))
        return self

    def hset(self, key: str, mapping: dict) -> "_FakePipeline":
        self._ops.append(("hset", (key, mapping)))
        return self

    async def execute(self) -> None:
        for op, args in self._ops:
            if op == "rpush":
                key, value = args
                self._redis.lists.setdefault(key, []).append(value)
            elif op == "hset":
                key, mapping = args
                self._redis.hashes.setdefault(key, {}).update(mapping)
            # "expire" is a no-op in this fake -- TTL behavior isn't under test here
