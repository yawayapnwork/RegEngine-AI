"""Data models and schemas for the Compliance Case-Law Memory Agent.

Encapsulates:
- PrecedentRecord: immutable historical record of an APPROVED/RESOLVED HITL decision with full cryptographic and review provenance.
- PrecedentMatch: candidate match returned by vector search with similarity score and provenance.
- PrecedentQuery: tenant-isolated query parameters.
- CaseLawAnalysisResult: advisory synthesis distinguishing current regulatory text, historical precedent, and reviewer guidance.
"""
from __future__ import annotations

import datetime as dt
from typing import Any

from pydantic import BaseModel, Field


class PrecedentRecord(BaseModel):
    """Immutable record of an approved human-in-the-loop compliance decision,
    indexed into the case-law vector memory.

    Provenance guarantees:
    - Retains full cryptographic binding back to the original circular, raw document hash,
      clause hash, and compiled policy hash.
    - Captures approving officer ID, timestamp, and resolution notes.
    - Strictly partitions by tenant_id (or marks is_shared=True for baseline regulator circulars).
    """

    precedent_id: str = Field(..., description="Unique precedent identifier (e.g. prec_<uuid>).")
    review_id: str = Field(..., description="Business ID of the approved HITLReview record.")
    tenant_id: str = Field(..., description="Tenant owning this precedent decision.")

    # Regulatory document provenance
    circular_id: str = Field(..., description="Database ID or slug of the source circular.")
    circular_number: str = Field(..., description="Official regulator circular reference number.")
    source_document_sha256: str = Field(..., description="SHA-256 digest of original physical PDF container.")

    # Clause provenance
    clause_id: str = Field(..., description="Database ID of the reviewed clause.")
    clause_number: str | None = Field(None, description="Clause number in circular (e.g. '3.2.1').")
    clause_sha256: str = Field(..., description="SHA-256 digest of normalized clause text.")
    original_clause_text: str = Field(..., description="Verbatim text of the source clause.")

    # Reviewer decision & reasoning
    decision: str = Field("APPROVED", description="Decision outcome ('APPROVED' or 'RESOLVED').")
    compliance_officer_id: str | None = Field(None, description="ID of human officer who approved the review.")
    resolution_notes: str | None = Field(None, description="Human officer's reasoning and interpretation notes.")
    reason_code: str | None = Field(None, description="Original HITL reason code (e.g. 'qualitative_directive').")

    # Policy version provenance
    approved_rule_version: int | str | None = Field(None, description="Version of compiled rule promoted to active.")
    approved_policy_sha256: str | None = Field(None, description="SHA-256 of active OPA policy bundle.")
    approval_timestamp: dt.datetime = Field(..., description="Timestamp when review was approved.")

    # Multi-tenancy flags & metadata
    is_shared: bool = Field(False, description="Whether this precedent originates from shared regulator baseline.")
    metadata: dict[str, Any] = Field(default_factory=dict, description="Additional contextual attributes.")


class PrecedentMatch(BaseModel):
    """A semantically similar precedent candidate returned during retrieval."""

    precedent: PrecedentRecord
    similarity_score: float = Field(..., ge=0.0, le=1.0, description="Cosine similarity score.")
    provenance_summary: str = Field(..., description="Human-readable provenance citation string.")


class PrecedentQuery(BaseModel):
    """Parameters for tenant-isolated precedent retrieval."""

    query_text: str = Field(..., min_length=3, description="Clause text or ambiguous directive to search for.")
    tenant_id: str = Field(..., min_length=1, description="Tenant ID enforcing mandatory isolation boundary.")
    top_k: int = Field(3, ge=1, le=20, description="Maximum number of candidates to retrieve.")
    min_similarity: float = Field(0.70, ge=0.0, le=1.0, description="Minimum cosine similarity threshold.")
    allow_shared: bool = Field(True, description="Whether shared baseline regulator circulars can be matched.")
    max_age_days: int | None = Field(None, description="Maximum age of precedent in days (None = unlimited).")


class CaseLawAnalysisResult(BaseModel):
    """Advisory output synthesized by the Compliance Case-Law Memory Agent.

    CRITICAL SAFETY RULES:
    1. Current regulatory source text ALWAYS has absolute supremacy.
    2. Precedent is historical, untrusted guidance; it must NEVER automatically override active text,
       invent new thresholds, or bypass human review.
    3. If any conflict exists between current text and precedent, conflict_flag_required is TRUE.
    """

    current_clause_text: str
    current_clause_number: str | None = None
    precedents: list[PrecedentMatch] = Field(default_factory=list)
    has_matching_precedent: bool = False

    # Conflict detection
    conflicts_detected: list[str] = Field(default_factory=list)
    conflict_flag_required: bool = False

    # Tri-part structured context distinguishing source law, historical precedent, and model guidance
    current_source_text: str
    historical_precedents_summary: str
    model_interpretation: str
    reviewer_guidance: str | None = None

    analyzed_at: dt.datetime = Field(default_factory=lambda: dt.datetime.now(dt.timezone.utc))

    @property
    def structured_context(self) -> str:
        guidance_part = f"\n\nGuidance: {self.reviewer_guidance}" if self.reviewer_guidance else ""
        return (
            f"{self.current_source_text}\n\n"
            f"{self.historical_precedents_summary}\n\n"
            f"=== 3. AGENT SYNTHESIS & REVIEW GUIDANCE ===\n"
            f"{self.model_interpretation}"
            f"{guidance_part}"
        )

    @property
    def precedent_matches(self) -> list[PrecedentMatch]:
        return self.precedents

    @property
    def conflicts(self) -> list[str]:
        return self.conflicts_detected
