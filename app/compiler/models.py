"""Output models for the policy compilation stage."""
from __future__ import annotations

import datetime as dt
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class CompiledRego(BaseModel):
    """A single OPA Rego module compiled from one ExtractedComplianceRule."""

    rule_id: str
    package: str
    rego_code: str
    entrypoints: list[str] = Field(
        default_factory=lambda: ["allow", "deny", "decision"],
        description="Rego rule names intended for external evaluation (opa eval -d policy.rego 'data.<package>.<entrypoint>').",
    )
    thresholds_compiled: int
    generated_at: dt.datetime = Field(default_factory=dt.datetime.utcnow)
    compiler_version: str = "1.0.0"


class JsonLogicRule(BaseModel):
    """A JSON-Logic AST fallback for non-OPA microservices, plus the metadata
    needed to render a human-readable violation message at evaluation time
    (JSON-Logic itself has no string-formatting primitive)."""

    rule_id: str
    logic: dict[str, Any] = Field(..., description="JSON-Logic AST; evaluates truthy when the rule is SATISFIED (compliant).")
    data_schema: dict[str, str] = Field(..., description="Expected input field -> type, e.g. {'facts.upfront_margin_pct': 'number'}.")
    violation_message_template: str = Field(
        ..., description="Python str.format() template rendered by the caller when `logic` evaluates false."
    )
    thresholds_compiled: int
    generated_at: dt.datetime = Field(default_factory=dt.datetime.utcnow)


class HITLReasonCode(str, Enum):
    QUALITATIVE_DIRECTIVE = "qualitative_directive"       # principle-based language, not reducible to logic
    AMBIGUOUS_SPAN = "ambiguous_span"                       # extractor deliberately left this unstructured
    LOW_EXTRACTION_CONFIDENCE = "low_extraction_confidence"
    AUDIT_NOT_APPROVED = "audit_not_approved"               # Logic Auditor rejected/flagged the extraction
    NO_DETERMINISTIC_LOGIC = "no_deterministic_logic"       # mandatory/prohibited obligation but zero thresholds
    CONFLICTING_THRESHOLDS = "conflicting_thresholds"       # two thresholds on the same field are contradictory
    UNRESOLVED_ENTITY = "unresolved_entity"                 # entity could not be normalized to the taxonomy


class HITLSeverity(str, Enum):
    BLOCKING = "blocking"     # rule cannot be compiled/enforced at all until resolved
    ADVISORY = "advisory"     # rule compiled (partially), but a portion needs human sign-off


class HITLFlag(BaseModel):
    flag_id: str
    rule_id: str
    reason_code: HITLReasonCode
    severity: HITLSeverity
    description: str
    source_excerpt: str | None = None
    field_path: str | None = Field(None, description="Path into ExtractedComplianceRule this flag concerns, if scoped to one field.")
    flagged_at: dt.datetime = Field(default_factory=dt.datetime.utcnow)


class CompilationResult(BaseModel):
    rule_id: str
    compiled: bool = Field(..., description="True if at least one enforceable Rego/JSON-Logic rule was produced.")
    rego: CompiledRego | None = None
    json_logic: JsonLogicRule | None = None
    hitl_flags: list[HITLFlag] = Field(default_factory=list)
