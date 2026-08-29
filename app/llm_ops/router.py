"""Dynamic model-tier router for LLM-backed compliance tasks.

Two-stage routing, not a single static classifier:

  1. Pre-call complexity scoring (`score_complexity`) picks an initial
     tier from the clause text alone -- fast, free, deterministic regex/
     heuristic scoring, no LLM call spent just to decide which LLM to
     call.
  2. Post-call escalation (`should_escalate`) looks at what the cheap
     tier actually produced -- a low `extraction_confidence`, a non-empty
     `ambiguous_spans`, or a schema-validation failure all mean the small
     model itself signaled it couldn't handle this clause confidently,
     regardless of how simple the pre-call heuristic thought it looked.
     This catches the cases a text-only heuristic structurally cannot:
     clauses that read as simple but turn out to hinge on a subtlety only
     visible after an attempted extraction.

Complexity heuristics are deliberately simple, inspectable rules (not a
learned classifier) so a compliance engineer can read exactly why a given
clause was routed where it was -- the same "no black-box reasoning where
regulatory correctness is at stake" principle the extraction/audit agents
already follow (see app.agents.schemas's verbatim_evidence discipline).
"""
from __future__ import annotations

import re

from app.config import Settings
from app.llm_ops.models import ModelTier, RoutingDecision, TaskComplexity

# Qualitative/principle-based language: the extraction agent must map this
# into `qualitative_directives`, not `deterministic_logic`, and doing that
# mapping correctly for ambiguous legal prose is exactly the kind of
# judgment call a frontier model is worth paying for.
_QUALITATIVE_MARKERS = re.compile(
    r"\b(adequate|reasonable|appropriate|prudent|as may be specified|from time to time|"
    r"at its discretion|good faith|best efforts|satisfactory|material adverse|"
    r"in the interest of|as deemed fit)\b",
    re.IGNORECASE,
)

# Cross-references force the extractor to reason about content outside the
# current chunk -- exactly the "needs more context than what's in front of
# it" case that benefits most from a stronger model's context handling.
_CROSS_REFERENCE_MARKERS = re.compile(
    r"\b(annexure|schedule|read with|as specified in|in terms of (clause|regulation|circular)|"
    r"pursuant to)\b",
    re.IGNORECASE,
)

# Nested conditionals ("provided that... unless... subject to...") change
# an obligation's scope in ways a single-pass small model is prone to
# mis-scoping (SCOPE_OVERREACH / MISSING_CONTEXT in the audit agent's own
# FindingType taxonomy -- app.agents.schemas).
_CONDITIONAL_MARKERS = re.compile(r"\b(provided that|unless|subject to|save as|except where)\b", re.IGNORECASE)

_NUMERIC_TOKEN = re.compile(r"\b\d+(\.\d+)?\s*(%|percent|crore|lakh|days?|months?|years?|bps)?\b")

_SIMPLE_MAX_CHARS = 400
_MODERATE_MAX_CHARS = 1200


def score_complexity(clause_text: str) -> tuple[TaskComplexity, list[str]]:
    reasons: list[str] = []

    if _QUALITATIVE_MARKERS.search(clause_text):
        reasons.append("contains qualitative/principle-based language (e.g. 'adequate', 'as deemed fit')")
    if _CROSS_REFERENCE_MARKERS.search(clause_text):
        reasons.append("cross-references another clause/annexure/circular")

    conditional_count = len(_CONDITIONAL_MARKERS.findall(clause_text))
    if conditional_count >= 2:
        reasons.append(f"multiple nested conditionals ({conditional_count})")

    numeric_matches = _NUMERIC_TOKEN.findall(clause_text)
    numeric_count = len(numeric_matches)

    if reasons:
        return TaskComplexity.COMPLEX, reasons

    if len(clause_text) > _MODERATE_MAX_CHARS:
        return TaskComplexity.COMPLEX, ["clause text exceeds length threshold for reliable single-pass small-model extraction"]

    if numeric_count == 0:
        # No deterministic numeric hook at all and no qualitative markers
        # matched either -- ambiguous by omission; treat conservatively.
        return TaskComplexity.MODERATE, ["no explicit numeric threshold found; ambiguous obligation shape"]

    if numeric_count == 1 and conditional_count == 0 and len(clause_text) <= _SIMPLE_MAX_CHARS:
        return TaskComplexity.SIMPLE, ["single unambiguous numeric threshold, no conditionals, short text"]

    return TaskComplexity.MODERATE, [f"{numeric_count} numeric threshold(s), {conditional_count} conditional(s), deterministic but non-trivial"]


class ModelRouter:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def decide(self, clause_text: str) -> RoutingDecision:
        complexity, reasons = score_complexity(clause_text)

        if complexity == TaskComplexity.SIMPLE:
            return RoutingDecision(
                tier=ModelTier.CHEAP_LOCAL,
                model_name=self._settings.llm_router_cheap_model,
                complexity=complexity,
                reasons=reasons,
            )

        # MODERATE still gets a shot at the cheap tier -- should_escalate()
        # is the real gate for whether that attempt was good enough, so a
        # correctly-handled moderate clause still saves the frontier-tier
        # cost. COMPLEX skips straight to frontier: routing a clause the
        # heuristic already flagged as qualitative/cross-referencing
        # through the cheap tier first would just spend a wasted call
        # before escalating anyway.
        if complexity == TaskComplexity.MODERATE:
            return RoutingDecision(
                tier=ModelTier.CHEAP_LOCAL,
                model_name=self._settings.llm_router_cheap_model,
                complexity=complexity,
                reasons=reasons,
            )

        return RoutingDecision(
            tier=ModelTier.FRONTIER,
            model_name=self._settings.llm_router_frontier_model,
            complexity=complexity,
            reasons=reasons,
        )

    def should_escalate(
        self,
        *,
        extraction_confidence: float | None,
        ambiguous_spans: list[str] | None,
        schema_valid: bool,
    ) -> tuple[bool, list[str]]:
        """Decides whether a cheap-tier result must be redone at the
        frontier tier. Called only when the initial decide() routed to
        CHEAP_LOCAL -- a COMPLEX clause already went straight to frontier
        and never reaches this check."""
        reasons: list[str] = []

        if not schema_valid:
            reasons.append("cheap-tier output failed schema validation")
        if extraction_confidence is not None and extraction_confidence < self._settings.llm_router_escalation_confidence_threshold:
            reasons.append(f"extraction_confidence {extraction_confidence:.2f} below threshold {self._settings.llm_router_escalation_confidence_threshold:.2f}")
        if ambiguous_spans:
            reasons.append(f"{len(ambiguous_spans)} ambiguous_span(s) flagged by the cheap-tier model itself")

        return (bool(reasons), reasons)
