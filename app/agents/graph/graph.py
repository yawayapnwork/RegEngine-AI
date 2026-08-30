"""Builds and compiles the dynamic agent `StateGraph`, and exposes
`run_graph_pipeline` -- the async entrypoint `app.agents.pipeline`
dispatches to when `settings.agent_graph_orchestration_enabled` is True.

Topology:

                                   +-------------------+
                                   | classify_complexity|
                                   +---------+---------+
                                             |
                    route_by_complexity (Requirement 1)
                     /              |               \\
                    v               v                v
      standard_extraction  quantitative_parsing  reference_resolution
                    \\              |               /
                     \\             |              /
                      route_confidence_gate (Requirement 3)
                       /                          \\
                      v                            v
          fallback_extraction  <-------loop------  |
                      \\                            |
                       \\___________________________v
                                     audit
                                       |
                          route_after_audit (revision loop,
                          bounded at MAX_REVISION_ROUNDS,
                          mirrors app.agents.crew's own bound)
                              /                \\
                             v                  v
                (back to the route's own      END
                 extraction node)

`langgraph` is imported lazily (this module's only import-time cost is
the lightweight state/edges/nodes modules), matching the crewai-lazy-import
convention app.agents.crew already established -- a deployment that
never enables `agent_graph_orchestration_enabled` never needs
`langgraph` installed at all.
"""
from __future__ import annotations

import logging
import time
import uuid

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
from app.agents.graph.nodes import (
    audit_node,
    classify_complexity_node,
    fallback_extraction_node,
    quantitative_parsing_node,
    reference_resolution_node,
    standard_extraction_node,
)
from app.agents.graph.state import AgentGraphState
from app.agents.graph.state_store import GraphExecutionStateStore
from app.agents.schemas import AuditedComplianceRule, ComplianceRuleAudit, ExtractedComplianceRule
from app.config import Settings, get_settings
from app.models import ClauseChunk

logger = logging.getLogger(__name__)

_CLASSIFY = "classify_complexity"

_SETTINGS_FIELDS_FOR_STATE = (
    "jwt_algorithm", "jwt_secret_key", "jwt_public_key_pem", "jwt_private_key_pem",
    "jwt_issuer", "jwt_audience",
    "hf_api_token", "hf_model_id", "agent_verbose", "agent_max_rpm",
    "agent_confidence_threshold", "agent_max_fallback_attempts", "agent_fallback_model",
    "redis_url", "agent_graph_state_key_prefix", "agent_graph_state_ttl_seconds",
)


def _settings_to_state_dict(settings: Settings) -> dict:
    """Only the fields the graph's nodes/edges actually read -- not the
    entire Settings object -- both because AgentGraphState must stay JSON
    -serializable for Redis (app.agents.graph.state_store) and because a
    graph run's Redis-recorded state should not incidentally leak every
    other unrelated setting (secrets, third-party API keys) it never used."""
    return {field: getattr(settings, field) for field in _SETTINGS_FIELDS_FOR_STATE}


def build_agent_graph():
    """Compiles the StateGraph once; callers should build/compile a fresh
    instance per process (cheap -- graph compilation does no I/O) rather
    than sharing a module-level singleton, since LangGraph graphs are not
    guaranteed thread-safe across concurrent `asyncio.to_thread` workers."""
    from langgraph.graph import END, StateGraph

    graph = StateGraph(AgentGraphState)

    graph.add_node(_CLASSIFY, classify_complexity_node)
    graph.add_node(NODE_STANDARD_EXTRACTION, standard_extraction_node)
    graph.add_node(NODE_QUANTITATIVE_PARSING, quantitative_parsing_node)
    graph.add_node(NODE_REFERENCE_RESOLUTION, reference_resolution_node)
    graph.add_node(NODE_FALLBACK_EXTRACTION, fallback_extraction_node)
    graph.add_node(NODE_AUDIT, audit_node)

    graph.set_entry_point(_CLASSIFY)

    # Requirement 1: dynamic routing by clause complexity.
    graph.add_conditional_edges(
        _CLASSIFY,
        route_by_complexity,
        {
            NODE_STANDARD_EXTRACTION: NODE_STANDARD_EXTRACTION,
            NODE_QUANTITATIVE_PARSING: NODE_QUANTITATIVE_PARSING,
            NODE_REFERENCE_RESOLUTION: NODE_REFERENCE_RESOLUTION,
        },
    )

    # Requirement 3: every extraction path (including a fallback attempt
    # itself, so a SECOND low-confidence result within budget can still
    # trigger another fallback round) passes through the same confidence
    # gate before reaching audit.
    for extraction_node in (NODE_STANDARD_EXTRACTION, NODE_QUANTITATIVE_PARSING, NODE_REFERENCE_RESOLUTION, NODE_FALLBACK_EXTRACTION):
        graph.add_conditional_edges(
            extraction_node,
            route_confidence_gate,
            {NODE_FALLBACK_EXTRACTION: NODE_FALLBACK_EXTRACTION, NODE_AUDIT: NODE_AUDIT},
        )

    # Revision loop: mirrors app.agents.crew.run_dual_validation, bounded
    # at the same MAX_REVISION_ROUNDS.
    graph.add_conditional_edges(
        NODE_AUDIT,
        route_after_audit,
        {
            NODE_STANDARD_EXTRACTION: NODE_STANDARD_EXTRACTION,
            NODE_QUANTITATIVE_PARSING: NODE_QUANTITATIVE_PARSING,
            NODE_REFERENCE_RESOLUTION: NODE_REFERENCE_RESOLUTION,
            END_MARKER: END,
        },
    )

    return graph.compile()


async def run_graph_pipeline(
    chunk: ClauseChunk,
    sibling_chunks: list[dict] | None = None,
    settings: Settings | None = None,
) -> AuditedComplianceRule:
    """Drop-in replacement for app.agents.crew.run_dual_validation, using
    the dynamic graph instead of the fixed two-agent sequential Crew.
    Same return type, same caller contract (app.agents.pipeline) -- the
    graph is an internal implementation swap, not a new external API."""
    settings = settings or get_settings()
    run_id = str(uuid.uuid4())
    graph = build_agent_graph()

    initial_state: AgentGraphState = {
        "run_id": run_id,
        "chunk": chunk.model_dump(mode="json"),
        "sibling_chunks": sibling_chunks or [],
        "settings_dict": _settings_to_state_dict(settings),
        "fallback_count": 0,
        "revision_round": 0,
        "token_usage": {"input_tokens": 0, "output_tokens": 0},
        "node_history": [],
        "prior_findings": None,
    }

    logger.info("Starting agent graph run_id=%s for chunk_id=%s (clause=%s)", run_id, chunk.chunk_id, chunk.clause_number)
    started = time.perf_counter()
    final_state = await graph.ainvoke(initial_state)
    logger.info(
        "Agent graph run_id=%s finished in %.2fs: route=%s fallback_used=%s node_history=%s",
        run_id, time.perf_counter() - started, final_state.get("route_taken"),
        final_state.get("used_fallback", False), final_state.get("node_history"),
    )

    extracted = ExtractedComplianceRule.model_validate(final_state["extracted_rule"])
    audit = ComplianceRuleAudit.model_validate(final_state["audit_result"])
    return AuditedComplianceRule(rule=extracted, audit=audit, revision_round=final_state.get("revision_round", 0))
