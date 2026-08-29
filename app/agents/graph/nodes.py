"""Node functions for the dynamic agent graph. Each node wraps exactly one
CrewAI agent invocation (or pure routing/gating logic), records its own
execution to Redis via `app.agents.graph.state_store` (Requirement 2)
before returning, and returns only the state keys it changed -- LangGraph
merges these into the accumulated `AgentGraphState`.

`crewai` is imported lazily inside each node function, matching
app.agents.crew's existing convention, so `app.agents.graph.state`/
`complexity_router`/`state_store` (and their tests) remain usable without
crewai installed.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from app.agents.graph.complexity_router import detect_complexity
from app.agents.graph.state import AgentGraphState, ComplexityRoute
from app.agents.graph.state_store import GraphExecutionStateStore
from app.config import Settings
from app.models import ClauseChunk
from app.regulatory.taxonomy import Regulator, resolve_domain

logger = logging.getLogger(__name__)

MAX_REVISION_ROUNDS = 2  # mirrors app.agents.crew.MAX_REVISION_ROUNDS


def _settings_from_state(state: AgentGraphState) -> Settings:
    return Settings(**state["settings_dict"])


def _store(state: AgentGraphState) -> GraphExecutionStateStore:
    import redis.asyncio as redis

    settings = _settings_from_state(state)
    redis_client = redis.from_url(settings.redis_url, decode_responses=True)
    return GraphExecutionStateStore(redis_client, settings.agent_graph_state_key_prefix, settings.agent_graph_state_ttl_seconds)


async def classify_complexity_node(state: AgentGraphState) -> dict[str, Any]:
    """Requirement 1's dynamic router entry point: decides which
    specialist agent this clause needs BEFORE any LLM is invoked."""
    started = time.perf_counter()
    chunk = state["chunk"]
    flags = detect_complexity(chunk["text"], chunk.get("clause_number"))
    route = flags.route.value

    store = _store(state)
    await store.record_node_execution(
        state["run_id"], "classify_complexity", route_taken=route, duration_ms=(time.perf_counter() - started) * 1000,
        extra={"math_signals": list(flags.math_signals), "cross_reference_signals": list(flags.cross_reference_signals)},
    )

    return {
        "complexity_flags": {
            "has_math_formulas": flags.has_math_formulas,
            "has_cross_references": flags.has_cross_references,
            "math_signals": list(flags.math_signals),
            "cross_reference_signals": list(flags.cross_reference_signals),
        },
        "route_taken": route,
        "node_history": [*state.get("node_history", []), "classify_complexity"],
    }


_ROUTE_TO_BUILDER_NAME: dict[str, str] = {
    ComplexityRoute.STANDARD.value: "build_extraction_agent",
    ComplexityRoute.QUANTITATIVE.value: "build_quantitative_parsing_agent",
    ComplexityRoute.REFERENCE_RESOLUTION.value: "build_reference_resolution_agent",
}


def _extract_token_usage(crew) -> dict[str, int]:
    """CrewAI exposes token usage on `crew.usage_metrics` (a
    UsageMetrics-like object) in recent versions; best-effort extraction
    with a safe empty-dict fallback since this varies across crewai
    releases and must never be what breaks an extraction run."""
    try:
        metrics = getattr(crew, "usage_metrics", None)
        if metrics is None:
            return {}
        return {
            "input_tokens": int(getattr(metrics, "prompt_tokens", 0) or 0),
            "output_tokens": int(getattr(metrics, "completion_tokens", 0) or 0),
        }
    except Exception:  # noqa: BLE001 - usage metrics are observability, never load-bearing
        return {}


def _merge_token_usage(existing: dict[str, int], new: dict[str, int]) -> dict[str, int]:
    return {
        "input_tokens": existing.get("input_tokens", 0) + new.get("input_tokens", 0),
        "output_tokens": existing.get("output_tokens", 0) + new.get("output_tokens", 0),
    }


async def _run_extraction_node(state: AgentGraphState, builder_name: str, node_name: str, model_override: str | None = None) -> dict[str, Any]:
    from app.agents import crew as crew_module  # deferred: pulls in crewai
    from app.agents.schemas import ExtractedComplianceRule

    settings = _settings_from_state(state)
    chunk = ClauseChunk.model_validate(state["chunk"])
    builder = getattr(crew_module, builder_name)

    started = time.perf_counter()

    def _run_sync() -> ExtractedComplianceRule:
        from crewai import Crew, Process

        agent = builder(settings, chunk.regulator, model_override=model_override)
        task = crew_module.build_extraction_task(agent, chunk, state.get("prior_findings"))
        crew = Crew(
            agents=[agent], tasks=[task], process=Process.sequential,
            memory=False, cache=False, verbose=settings.agent_verbose, max_rpm=settings.agent_max_rpm,
        )
        crew.kickoff()
        extracted = task.output.pydantic
        if not isinstance(extracted, ExtractedComplianceRule):
            raise ValueError(f"{node_name}: crew did not return a schema-conformant ExtractedComplianceRule.")
        extracted.regulator = chunk.regulator
        primary_entity = extracted.target_entities[0].normalized_entity if extracted.target_entities else None
        extracted.regulatory_domain = resolve_domain(chunk.regulator, primary_entity)
        extracted._token_usage = _extract_token_usage(crew)  # stashed for the caller; not a schema field
        return extracted

    error: str | None = None
    token_usage: dict[str, int] = {}
    confidence: float | None = None
    try:
        extracted = await asyncio.to_thread(_run_sync)
        confidence = extracted.extraction_confidence
        token_usage = getattr(extracted, "_token_usage", {})
        extracted_dict = extracted.model_dump(mode="json")
    except Exception as exc:  # noqa: BLE001 - recorded then re-raised so the graph run fails loudly, not silently
        error = str(exc)
        logger.exception("%s failed for chunk_id=%s", node_name, chunk.chunk_id)
        raise
    finally:
        store = _store(state)
        await store.record_node_execution(
            state["run_id"], node_name, route_taken=state.get("route_taken"),
            confidence_score=confidence, token_usage=token_usage,
            duration_ms=(time.perf_counter() - started) * 1000, error=error,
        )

    return {
        "extracted_rule": extracted_dict,
        "extraction_confidence": confidence,
        "extraction_model": model_override or "primary",
        "token_usage": _merge_token_usage(state.get("token_usage", {}), token_usage),
        "node_history": [*state.get("node_history", []), node_name],
    }


async def standard_extraction_node(state: AgentGraphState) -> dict[str, Any]:
    return await _run_extraction_node(state, "build_extraction_agent", "standard_extraction")


async def quantitative_parsing_node(state: AgentGraphState) -> dict[str, Any]:
    return await _run_extraction_node(state, "build_quantitative_parsing_agent", "quantitative_parsing")


async def reference_resolution_node(state: AgentGraphState) -> dict[str, Any]:
    return await _run_extraction_node(state, "build_reference_resolution_agent", "reference_resolution")


async def fallback_extraction_node(state: AgentGraphState) -> dict[str, Any]:
    """Requirement 3: invoked when the confidence gate finds
    `extraction_confidence` below `settings.agent_confidence_threshold`.
    Re-runs the SAME specialist that originally ran (route_taken decides
    which builder), but on `settings.agent_fallback_model` -- a distinct
    model/checkpoint from the primary, not a repeat call to it."""
    settings = _settings_from_state(state)
    route = state.get("route_taken", ComplexityRoute.STANDARD.value)
    builder_name = _ROUTE_TO_BUILDER_NAME.get(route, "build_extraction_agent")

    result = await _run_extraction_node(state, builder_name, "fallback_extraction", model_override=settings.agent_fallback_model)
    result["fallback_count"] = state.get("fallback_count", 0) + 1
    result["used_fallback"] = True
    return result


async def audit_node(state: AgentGraphState) -> dict[str, Any]:
    from app.agents import crew as crew_module  # deferred: pulls in crewai
    from app.agents.schemas import ComplianceRuleAudit, ExtractedComplianceRule

    settings = _settings_from_state(state)
    chunk = ClauseChunk.model_validate(state["chunk"])
    extracted = ExtractedComplianceRule.model_validate(state["extracted_rule"])

    started = time.perf_counter()

    def _run_sync() -> ComplianceRuleAudit:
        from crewai import Crew, Process

        audit_agent = crew_module.build_audit_agent(settings, chunk.regulator)

        # Same "stub a completed Task" pattern app.llm_ops.cached_extraction
        # uses: we already have the extraction result as a validated
        # Pydantic object (from a PRIOR node, possibly several graph steps
        # ago), so build_audit_task's `context=[extraction_task]` wiring
        # only needs something exposing `.output.pydantic` -- re-running
        # extraction through a fresh CrewAI Task just to satisfy that
        # interface would waste an LLM call reproducing output we already have.
        class _StubExtractionTask:
            class _Output:
                def __init__(self, pydantic_obj: ExtractedComplianceRule) -> None:
                    self.pydantic = pydantic_obj

            def __init__(self, pydantic_obj: ExtractedComplianceRule) -> None:
                self.output = self._Output(pydantic_obj)

        stub = _StubExtractionTask(extracted)
        audit_task = crew_module.build_audit_task(audit_agent, chunk, stub, state.get("sibling_chunks", []))
        crew = Crew(
            agents=[audit_agent], tasks=[audit_task], process=Process.sequential,
            memory=False, cache=False, verbose=settings.agent_verbose, max_rpm=settings.agent_max_rpm,
        )
        crew.kickoff()
        audit = audit_task.output.pydantic
        if not isinstance(audit, ComplianceRuleAudit):
            raise ValueError("audit_node: crew did not return a schema-conformant ComplianceRuleAudit.")
        audit._token_usage = _extract_token_usage(crew)
        return audit

    error: str | None = None
    token_usage: dict[str, int] = {}
    fidelity_score: float | None = None
    try:
        audit = await asyncio.to_thread(_run_sync)
        token_usage = getattr(audit, "_token_usage", {})
        fidelity_score = audit.fidelity_score
        audit_dict = audit.model_dump(mode="json")
    except Exception as exc:  # noqa: BLE001
        error = str(exc)
        logger.exception("audit_node failed for chunk_id=%s", chunk.chunk_id)
        raise
    finally:
        store = _store(state)
        await store.record_node_execution(
            state["run_id"], "audit", route_taken=state.get("route_taken"),
            confidence_score=fidelity_score,
            token_usage=token_usage, duration_ms=(time.perf_counter() - started) * 1000, error=error,
        )

    result: dict[str, Any] = {
        "audit_result": audit_dict,
        "token_usage": _merge_token_usage(state.get("token_usage", {}), token_usage),
        "node_history": [*state.get("node_history", []), "audit"],
    }
    # Prepares the revision-loop state BEFORE app.agents.graph.edges.route_after_audit
    # decides whether to use it -- a conditional edge function only
    # routes, it cannot itself mutate state, so this must happen here.
    if audit_dict.get("verdict") == "needs_revision":
        result["prior_findings"] = audit_dict.get("findings", [])
        result["revision_round"] = state.get("revision_round", 0) + 1
    return result
