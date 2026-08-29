"""Data contracts for the decision-explanation module.

Turns a technical `deny`/`flagged` OPA decision (app.execution.models.
EvaluationResult, built from app.execution.opa_engine's raw
`data.<package>.decision` responses) into a structured,
compliance-officer/auditor-readable legal justification -- and vice
versa, parses the compiler's own generated Rego violation strings
(app.compiler.rego_compiler._violation_clauses) back into structured
facts so the natural-language layer never has to re-derive them from
scratch or risk fabricating a number that wasn't actually in the
decision.
"""
from __future__ import annotations

import datetime as dt
from enum import Enum

from pydantic import BaseModel, Field


class ExplanationSource(str, Enum):
    DETERMINISTIC = "deterministic"  # app.explainability.trace_parser + nlg_deterministic -- exact regex match on a compiler-generated violation string
    LLM = "llm"                       # app.explainability.llm_explainer -- fallback for a violation string the deterministic parser couldn't structurally match
    UNPARSEABLE = "unparseable"       # neither path could produce a confident explanation; raw text is passed through verbatim


class StructuredViolation(BaseModel):
    """The facts recovered from one Rego `violation` message string --
    the actual "decision trace" this module parses. Every field here is
    either lifted verbatim from the OPA decision object
    (rule_id/circular_number/clause_number) or extracted via regex from
    text the compiler itself generated from those same facts
    (app.compiler.rego_compiler), so nothing here is inferred or
    hallucinated -- it is a structural re-reading of data that was
    already fully determined at compile time."""

    rule_id: str
    circular_number: str | None = None
    clause_number: str | None = None
    regulator: str = "sebi"

    metric: str
    observed_value: float
    unit: str
    operator: str  # ">=" | ">" | "<=" | "<" | "==" | "range_low" | "range_high"
    required_value: float
    applies_to: str | None = None
    raw_violation_text: str


class LegalExplanation(BaseModel):
    """One human-readable justification for one violated policy."""

    rule_id: str
    circular_number: str | None = None
    clause_number: str | None = None

    headline: str = Field(..., description="Single-sentence justification, e.g. 'Trade rejected: Margin collected (15%) is below the mandatory SEBI threshold (20%) required by SEBI Master Circular Clause 4.2.b.'")
    citation: str = Field(..., description="Short regulatory citation string, e.g. 'SEBI Master Circular Clause 4.2.b (SEBI/HO/MIRSD/DOP/CIR/P/2024/100)'.")

    structured_violation: StructuredViolation | None = None
    source: ExplanationSource
    confidence: float = Field(1.0, ge=0.0, le=1.0)


class DecisionExplanationBundle(BaseModel):
    """The full explanation set for one transaction evaluation --
    what gets shown on the HITL review portal / SEBI audit export, and
    (the deterministic-only headline text) what gets written into
    app.ledger's `details.explanation` field alongside the hash chain."""

    transaction_id: str
    decision: str  # "allow" | "deny" | "flagged"
    evaluated_at: dt.datetime
    overall_summary: str
    explanations: list[LegalExplanation] = Field(default_factory=list)
    generated_at: dt.datetime = Field(default_factory=lambda: dt.datetime.now(dt.timezone.utc))
