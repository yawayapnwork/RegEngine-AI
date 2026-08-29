"""Conditional-edge functions for the dynamic agent graph -- pure
functions of `AgentGraphState`, deliberately free of any LangGraph import
so they're unit-testable without the package installed and without
constructing a graph at all.
"""
from __future__ import annotations

from app.agents.graph.nodes import MAX_REVISION_ROUNDS
from app.agents.graph.state import AgentGraphState, ComplexityRoute
from app.config import Settings

# Node names, as constants so a typo in an edge function and the actual
# graph wiring (app.agents.graph.graph) can never silently disagree.
NODE_STANDARD_EXTRACTION = "standard_extraction"
NODE_QUANTITATIVE_PARSING = "quantitative_parsing"
NODE_REFERENCE_RESOLUTION = "reference_resolution"
NODE_FALLBACK_EXTRACTION = "fallback_extraction"
NODE_AUDIT = "audit"
END_MARKER = "__end__"

_ROUTE_TO_NODE: dict[str, str] = {
    ComplexityRoute.STANDARD.value: NODE_STANDARD_EXTRACTION,
    ComplexityRoute.QUANTITATIVE.value: NODE_QUANTITATIVE_PARSING,
    ComplexityRoute.REFERENCE_RESOLUTION.value: NODE_REFERENCE_RESOLUTION,
}


def route_by_complexity(state: AgentGraphState) -> str:
    """Requirement 1's dynamic router: dispatches to the specialist node
    `classify_complexity_node` selected, by name."""
    route = state.get("route_taken", ComplexityRoute.STANDARD.value)
    return _ROUTE_TO_NODE.get(route, NODE_STANDARD_EXTRACTION)


def route_confidence_gate(state: AgentGraphState) -> str:
    """Requirement 3's fallback mechanism: routes to the fallback node
    when `extraction_confidence` is below threshold AND the fallback
    budget (`agent_max_fallback_attempts`) isn't exhausted -- an
    already-exhausted fallback budget proceeds to audit anyway rather
    than looping forever, so a persistently low-confidence clause still
    reaches a human via the audit stage's own HITL flagging path instead
    of being silently stuck."""
    settings = Settings(**state["settings_dict"])
    confidence = state.get("extraction_confidence")
    fallback_count = state.get("fallback_count", 0)

    if (
        confidence is not None
        and confidence < settings.agent_confidence_threshold
        and fallback_count < settings.agent_max_fallback_attempts
    ):
        return NODE_FALLBACK_EXTRACTION
    return NODE_AUDIT


def route_after_audit(state: AgentGraphState) -> str:
    """Mirrors app.agents.crew.run_dual_validation's revision loop: on
    `needs_revision`, route back to whichever extraction node originally
    ran (route_taken), feeding the auditor's findings back as
    `prior_findings` -- capped at MAX_REVISION_ROUNDS, matching the
    original CrewAI-only pipeline's bound exactly, so switching a
    deployment's `agent_graph_orchestration_enabled` flag does not
    silently change how many revision attempts a clause gets."""
    audit_result = state.get("audit_result") or {}
    verdict = audit_result.get("verdict")
    revision_round = state.get("revision_round", 0)

    if verdict == "needs_revision" and revision_round < MAX_REVISION_ROUNDS:
        route = state.get("route_taken", ComplexityRoute.STANDARD.value)
        return _ROUTE_TO_NODE.get(route, NODE_STANDARD_EXTRACTION)
    return END_MARKER
