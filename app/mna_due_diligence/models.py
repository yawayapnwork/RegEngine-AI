"""Data models and contracts for the M&A Compliance Due-Diligence Agent.

Defines comparison request shapes, isolated entity snapshots, finding taxonomies,
provenance tracking, reviewable due-diligence reports, and job progress states.
"""
from __future__ import annotations

import datetime as dt
from enum import Enum
from typing import Any
import uuid

from pydantic import BaseModel, Field


class MNADifferenceType(str, Enum):
    """Classification of differences found between Entity A and Entity B compliance postures."""
    EXACT_DIFFERENCE = "exact_difference"           # Byte/hash or AST mismatch on the same rule/clause
    SEMANTIC_DIFFERENCE = "semantic_difference"     # Different numeric thresholds or phrasing on the same obligation
    POTENTIAL_CONFLICT = "potential_conflict"       # Contradictory rules/clauses (e.g. A requires X, B prohibits X)
    UNRESOLVED_AMBIGUITY = "unresolved_ambiguity"   # Unresolved HITL items, ambiguous clauses, or qualitative directives
    MISSING_POLICY = "missing_policy"               # Obligation/rule present in one entity but absent in the other


class MNAFindingSeverity(str, Enum):
    """Impact severity of an M&A compliance finding."""
    INFO = "INFO"
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class MNAFindingProvenance(BaseModel):
    """Retains full cryptographic and reference provenance back to source artifacts on both entities."""
    entity_a_artifact_ref: str | None = Field(None, description="Identifier of Entity A artifact (e.g. rule:margin_v1 or clause:45)")
    entity_b_artifact_ref: str | None = Field(None, description="Identifier of Entity B artifact (e.g. rule:margin_v2 or None if missing)")
    entity_a_hash: str | None = Field(None, description="SHA-256 hash of Entity A policy/clause")
    entity_b_hash: str | None = Field(None, description="SHA-256 hash of Entity B policy/clause")
    metric_field: str | None = Field(None, description="Normalized canonical fact metric field (e.g. facts.margin_percentage)")
    evidence_notes: str = Field("", description="Specific evidence and justification for the finding")


class MNAFinding(BaseModel):
    """One individual due-diligence finding comparing Entity A and Entity B."""
    finding_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    difference_type: MNADifferenceType
    severity: MNAFindingSeverity
    title: str
    description: str
    is_advisory: bool = Field(False, description="True if derived from LLM semantic evaluation; False for deterministic diffs")
    provenance: MNAFindingProvenance
    entity_a_value: Any | None = None
    entity_b_value: Any | None = None
    recommendation: str | None = Field(None, description="Non-mutating compliance advice for human officer review")


class EntityRuleSnapshot(BaseModel):
    """Snapshot of one compiled compliance rule belonging to an entity."""
    rule_id: str
    rule_version: int = 1
    policy_sha256: str | None = None
    rego_policy: str | None = None
    jsonlogic_ast: dict[str, Any] | None = None
    opa_package_name: str | None = None
    is_active: bool = False
    hitl_status: str = "NONE"
    clause_id: int | None = None
    clause_sha256: str | None = None
    clause_number: str | None = None
    section_reference: str | None = None


class EntityClauseSnapshot(BaseModel):
    """Snapshot of a circular clause scoped to an entity or shared baseline."""
    clause_number: str | None = None
    text: str
    sha256: str
    section_path: list[str] = Field(default_factory=list)
    circular_number: str | None = None


class EntityHITLReviewSnapshot(BaseModel):
    """Snapshot of an open/unresolved HITL review item belonging to an entity."""
    review_id: str
    reason_code: str
    severity: str
    description: str
    source_excerpt: str | None = None
    status: str
    clause_id: int | None = None
    compiled_rule_id: int | None = None
    flagged_at: dt.datetime | None = None


class EntityHistoricalViolationsSummary(BaseModel):
    """Aggregated historical non-PASS compliance records without exposing raw transactions or PII."""
    total_evaluations: int = 0
    total_fails: int = 0
    total_hitl_reviews: int = 0
    failed_rule_ids: list[str] = Field(default_factory=list)
    top_violation_metrics: dict[str, int] = Field(default_factory=dict)
    has_transaction_level_data: bool = False


class EntityGraphObligationSnapshot(BaseModel):
    """Graph obligation and relationship snapshot for an entity."""
    obligation_id: str
    metric: str
    operator: str
    value: float
    unit: str
    domain: str | None = None


class EntityComplianceSnapshot(BaseModel):
    """Isolated, complete compliance posture snapshot for one regulated entity."""
    entity_id: str
    display_name: str
    tenant_type: str
    is_active: bool = True
    snapshot_timestamp: dt.datetime = Field(default_factory=lambda: dt.datetime.now(dt.timezone.utc))
    snapshot_hash: str = Field(..., description="Cryptographic SHA-256 digest ensuring snapshot consistency")
    rules: list[EntityRuleSnapshot] = Field(default_factory=list)
    risk_overlay: dict[str, Any] = Field(default_factory=dict)
    clauses: list[EntityClauseSnapshot] = Field(default_factory=list)
    unresolved_hitl: list[EntityHITLReviewSnapshot] = Field(default_factory=list)
    historical_violations_summary: EntityHistoricalViolationsSummary = Field(default_factory=EntityHistoricalViolationsSummary)
    graph_obligations: list[EntityGraphObligationSnapshot] = Field(default_factory=list)


class MNAComparisonRequest(BaseModel):
    """Request payload to initiate an M&A compliance due-diligence comparison."""
    entity_a_id: str = Field(..., description="First regulated entity identifier (e.g. acquiring firm)")
    entity_b_id: str = Field(..., description="Second regulated entity identifier (e.g. target firm)")
    comparison_scope: list[str] = Field(
        default_factory=lambda: ["rules", "thresholds", "risk_overlay", "hitl", "violations", "graph"],
        description="Aspects to compare across both entities"
    )
    run_in_background: bool = Field(False, description="Run comparison asynchronously in background worker")
    advisory_mode: bool = Field(True, description="Enable advisory LLM semantic analysis for qualitative clauses")


class MNADueDiligenceReport(BaseModel):
    """Comprehensive due-diligence audit report comparing Entity A and Entity B."""
    report_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    job_id: str
    entity_a_id: str
    entity_b_id: str
    generated_at: dt.datetime = Field(default_factory=lambda: dt.datetime.now(dt.timezone.utc))
    initiator_subject: str
    entity_a_snapshot_hash: str
    entity_b_snapshot_hash: str
    findings: list[MNAFinding] = Field(default_factory=list)
    summary_counts: dict[str, int] = Field(default_factory=dict)
    overall_risk_score: float = Field(0.0, ge=0.0, le=100.0)
    executive_summary: str = ""
    disclaimer: str = Field(
        default=(
            "LEGAL & REGULATORY NOTICE: This M&A Compliance Due-Diligence Report is an advisory analysis "
            "prepared strictly for compliance officer review. Findings, differences, and risk scores are advisory "
            "and do NOT constitute legal opinion or formal regulatory approval. No production policies or risk "
            "overlays have been altered or merged. Deterministic rules remain unchanged."
        )
    )


class MNAJobStatus(str, Enum):
    """Lifecycle states of an M&A comparison job."""
    QUEUED = "QUEUED"
    SNAPSHOT_ACQUISITION = "SNAPSHOT_ACQUISITION"
    DETERMINISTIC_DIFFING = "DETERMINISTIC_DIFFING"
    SEMANTIC_ANALYSIS = "SEMANTIC_ANALYSIS"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class MNAJobProgress(BaseModel):
    """Status tracking for an in-progress or finished M&A due-diligence job."""
    job_id: str
    entity_a_id: str
    entity_b_id: str
    initiator_subject: str
    status: MNAJobStatus = MNAJobStatus.QUEUED
    progress_pct: int = Field(0, ge=0, le=100)
    current_step: str = "Initialized"
    started_at: dt.datetime = Field(default_factory=lambda: dt.datetime.now(dt.timezone.utc))
    updated_at: dt.datetime = Field(default_factory=lambda: dt.datetime.now(dt.timezone.utc))
    completed_at: dt.datetime | None = None
    error_message: str | None = None
    report: MNADueDiligenceReport | None = None
    cancelled: bool = False
