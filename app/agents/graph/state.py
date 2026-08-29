"""Shared state schema for the dynamic agent graph.

A `TypedDict` (LangGraph's idiomatic state shape, not a Pydantic model) --
LangGraph merges partial node return values into the accumulated state by
key, which is exactly `TypedDict`'s update semantics; a Pydantic model
would need extra reducer wiring to get the same "each node returns only
the keys it changed" behavior LangGraph expects by default.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, TypedDict


class ComplexityRoute(str, Enum):
    STANDARD = "standard"                      # no special signal detected -- the general Extraction Agent
    QUANTITATIVE = "quantitative"                # math formulas detected -- Quantitative Parsing Agent
    REFERENCE_RESOLUTION = "reference_resolution"  # nested cross-references detected -- Reference Resolution Agent


@dataclass(frozen=True)
class ComplexityFlags:
    has_math_formulas: bool
    has_cross_references: bool
    math_signals: tuple[str, ...] = field(default_factory=tuple)
    cross_reference_signals: tuple[str, ...] = field(default_factory=tuple)

    @property
    def route(self) -> ComplexityRoute:
        # Math-formula routing takes precedence when a clause exhibits
        # both signals: a formula's own internal cross-references (e.g. a
        # CRAR formula that also says "as defined in clause 2.1") are
        # still fundamentally a quantitative-decomposition problem first --
        # the Quantitative Parsing Agent has build_clause_context available
        # too (see app.agents.crew.build_quantitative_parsing_agent's tool
        # list) and can still resolve a reference it encounters, whereas
        # routing to Reference Resolution first would hand a formula to an
        # agent not specialized for formula decomposition at all.
        if self.has_math_formulas:
            return ComplexityRoute.QUANTITATIVE
        if self.has_cross_references:
            return ComplexityRoute.REFERENCE_RESOLUTION
        return ComplexityRoute.STANDARD


class AgentGraphState(TypedDict, total=False):
    # --- Inputs (set once, never mutated by a node) ---
    run_id: str
    chunk: dict[str, Any]              # ClauseChunk.model_dump()
    sibling_chunks: list[dict[str, Any]]
    settings_dict: dict[str, Any]      # Settings fields this graph actually needs, not the whole Settings object (keeps state JSON-serializable for Redis)

    # --- Routing ---
    complexity_flags: dict[str, Any]   # ComplexityFlags fields, as a plain dict
    route_taken: str                   # ComplexityRoute value

    # --- Extraction ---
    extracted_rule: dict[str, Any] | None       # ExtractedComplianceRule.model_dump()
    extraction_confidence: float | None
    extraction_model: str                        # which model actually produced extracted_rule -- primary or fallback

    # --- Fallback loop ---
    fallback_count: int
    used_fallback: bool

    # --- Audit ---
    audit_result: dict[str, Any] | None          # ComplianceRuleAudit.model_dump()
    prior_findings: list[dict[str, Any]] | None
    revision_round: int

    # --- Observability (Requirement 2) ---
    token_usage: dict[str, int]        # cumulative {"input_tokens": N, "output_tokens": N} across every node
    node_history: list[str]            # ordered list of node names visited, for tracing the actual path taken
    error: str | None
