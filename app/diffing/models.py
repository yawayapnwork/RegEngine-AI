"""Data contracts for the pre-compilation circular impact-diff engine.

Runs AFTER extraction/audit (app.agents.pipeline produces
AuditedComplianceRule) but BEFORE compilation (app.compiler.pipeline
produces CompiledRego/JsonLogicRule) -- the point where a compliance
officer can see "here is what this new circular actually changes, and
here is what breaks downstream" before a single line of Rego is
generated, let alone deployed.
"""
from __future__ import annotations

import datetime as dt
from enum import Enum

from pydantic import BaseModel, Field


class MatchConfidence(str, Enum):
    """How confident the semantic-similarity search is that a historical
    clause is what the new clause amends, supersedes, or restates."""

    IDENTICAL = "identical"                # >= 0.995 -- byte-for-byte or near-verbatim reprint
    NEAR_DUPLICATE = "near_duplicate"       # >= 0.92  -- same clause, cosmetic wording changes
    LIKELY_AMENDMENT = "likely_amendment"   # >= 0.75  -- clearly the same obligation, substantively reworded
    WEAK_MATCH = "weak_match"               # >= 0.55  -- topically related, ambiguous whether it's the same obligation
    NO_MATCH = "no_match"                   # < 0.55   -- no historical counterpart found; likely genuinely new


class ChangeType(str, Enum):
    THRESHOLD_SHIFT = "threshold_shift"           # a numeric threshold's value/operator changed
    DEADLINE_AMENDMENT = "deadline_amendment"      # a time-bound threshold (settlement cycle, reporting window) changed
    NEW_OBLIGATION = "new_obligation"              # no historical counterpart; a wholly new compliance trigger
    OBLIGATION_REMOVED = "obligation_removed"      # a historical clause has no counterpart in the new circular
    ENTITY_SCOPE_CHANGE = "entity_scope_change"    # same obligation, different/expanded set of target_entities
    WORDING_ONLY = "wording_only"                  # reworded but no change in enforceable meaning
    UNCHANGED = "unchanged"                        # effectively identical to its historical counterpart


class ImpactSeverity(str, Enum):
    LOW = "low"           # WORDING_ONLY / UNCHANGED -- no code change required
    MEDIUM = "medium"      # THRESHOLD_SHIFT on a non-critical metric, ENTITY_SCOPE_CHANGE
    HIGH = "high"          # DEADLINE_AMENDMENT, THRESHOLD_SHIFT on a critical metric (margin, capital adequacy, solvency)
    CRITICAL = "critical"  # NEW_OBLIGATION, OBLIGATION_REMOVED -- a compliance trigger appearing/disappearing outright


class ThresholdDelta(BaseModel):
    """One NumericalThreshold's old-vs-new comparison. `field` is the exact
    `input.facts.<field>` key (app.compiler.naming.metric_field_name) both
    old and new thresholds resolve to -- the mechanical basis for knowing
    they're "the same metric" at all, independent of any wording change."""

    field: str
    metric: str
    unit: str
    old_operator: str | None = None
    old_value: float | None = None
    new_operator: str | None = None
    new_value: float | None = None
    delta_absolute: float | None = None
    delta_pct: float | None = Field(None, description="(new - old) / old * 100; None if old_value is 0 or missing.")
    tightened: bool | None = Field(None, description="True if the new threshold is strictly more restrictive than the old one.")


class ServiceImpact(BaseModel):
    """One internal microservice/endpoint this clause change is predicted
    to require a code update in -- see app.diffing.service_mapping."""

    service_name: str
    endpoints: list[str] = Field(default_factory=list)
    reason: str
    confidence: float = Field(..., ge=0.0, le=1.0, description="How confident the field->service mapping is; 1.0 for an exact metric_field_name match, lower for a domain-level fallback.")


class ClauseDiffResult(BaseModel):
    """The full diff verdict for one new clause/rule."""

    new_rule_id: str
    new_clause_number: str | None = None
    new_circular_number: str | None = None

    matched_historical_chunk_id: str | None = None
    matched_historical_clause_number: str | None = None
    matched_historical_circular_number: str | None = None
    similarity_score: float | None = None
    match_confidence: MatchConfidence = MatchConfidence.NO_MATCH

    change_type: ChangeType
    severity: ImpactSeverity
    classification_method: str = Field(..., description="\"structural\" (deterministic threshold diff) or \"llm\" (app.diffing.llm_classifier, used when structural comparison was inconclusive).")
    classification_confidence: float = Field(..., ge=0.0, le=1.0)

    threshold_deltas: list[ThresholdDelta] = Field(default_factory=list)
    narrative_summary: str = Field(..., description="One or two sentence human-readable explanation of what changed and why it was classified this way.")

    service_impacts: list[ServiceImpact] = Field(default_factory=list)
    requires_hitl_review: bool = False


class CircularImpactReport(BaseModel):
    """Top-level report for one newly ingested circular -- the artifact a
    compliance officer reviews before approving compilation, and what a
    platform/integration team reads to know which services need a
    pre-emptive code change."""

    report_id: str
    circular_number: str | None = None
    regulator: str = "sebi"
    generated_at: dt.datetime = Field(default_factory=lambda: dt.datetime.now(dt.timezone.utc))

    total_new_clauses: int = 0
    change_type_counts: dict[str, int] = Field(default_factory=dict)
    severity_counts: dict[str, int] = Field(default_factory=dict)

    overall_risk_level: ImpactSeverity = ImpactSeverity.LOW
    clause_diffs: list[ClauseDiffResult] = Field(default_factory=list)

    affected_services: list[str] = Field(default_factory=list, description="Deduplicated union of every ServiceImpact.service_name across all clause_diffs -- the quick-glance 'who needs to be in the room' list.")

    obligations_removed: list[str] = Field(
        default_factory=list,
        description="Historical clause numbers (from the prior Master Circular) present in the index but with no corresponding clause found in this new circular -- see app.diffing.report_builder's coverage-check pass.",
    )
