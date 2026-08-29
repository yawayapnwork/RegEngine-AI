"""Data contracts for the translation parity checker. Pydantic
throughout (matches app.localization/app.execution conventions) since
these are serialized both over the FastAPI routes and into the Redis-
backed discrepancy queue (app.translation_parity.queue).
"""
from __future__ import annotations

import datetime as dt
from enum import Enum

from pydantic import BaseModel, Field

from app.localization.verification import TranslationVerificationResult


class DiscrepancyType(str, Enum):
    NUMERIC_MISMATCH = "numeric_mismatch"                  # a number present in one language has no counterpart in the other
    SEMANTIC_DRIFT = "semantic_drift"                       # cross-lingual similarity below translation_parity's hard floor
    AMBIGUOUS_TRANSLATION = "ambiguous_translation"          # similarity in the "worth a human glance" band, not a hard failure
    MISSING_CLAUSE_IN_HINDI = "missing_clause_in_hindi"      # an English clause has no aligned Hindi counterpart at all
    MISSING_CLAUSE_IN_ENGLISH = "missing_clause_in_english"  # a Hindi clause has no aligned English counterpart at all


class DiscrepancySeverity(str, Enum):
    """Same two-value vocabulary as app.db.models.HITLReview's
    `_HITL_SEVERITIES` ("blocking"/"advisory") -- kept identical even
    though this queue is a separate (Redis-backed) store, so a
    compliance officer reading either review surface sees the same
    severity meaning."""

    BLOCKING = "blocking"
    ADVISORY = "advisory"


class ClauseRef(BaseModel):
    """A minimal, language-tagged pointer to one clause -- enough to
    render a diff and cite a location, without coupling this
    pre-compilation check to `app.db.models.Clause` rows that may not
    exist yet (see this package's docstring: this runs BEFORE either
    language's clauses are necessarily persisted)."""

    clause_number: str | None
    text: str


class ClauseAlignment(BaseModel):
    """One pairing decision the aligner made (or a deliberate non-pairing
    for a clause it could not match at all)."""

    english_clause: ClauseRef | None
    hindi_clause: ClauseRef | None
    match_method: str = Field(..., description='"clause_number" (exact numbering match) or "embedding" (cross-lingual similarity fallback) or "unmatched".')
    match_confidence: float | None = Field(None, description="Only set for embedding-matched pairs; clause_number matches are treated as certain (1.0).")


class ClauseDiscrepancy(BaseModel):
    discrepancy_type: DiscrepancyType
    severity: DiscrepancySeverity
    english_clause_number: str | None
    hindi_clause_number: str | None
    description: str
    english_excerpt: str | None = None
    hindi_excerpt: str | None = None
    verification: TranslationVerificationResult | None = Field(None, description="Set for NUMERIC_MISMATCH/SEMANTIC_DRIFT/AMBIGUOUS_TRANSLATION -- the full app.localization.verification result this discrepancy was derived from.")


class TranslationParityReport(BaseModel):
    report_id: str
    circular_number: str
    alignments: list[ClauseAlignment]
    discrepancies: list[ClauseDiscrepancy]
    mean_semantic_similarity: float | None
    requires_hitl_review: bool
    generated_at: dt.datetime = Field(default_factory=lambda: dt.datetime.now(dt.timezone.utc))


class DiscrepancyReviewStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"  # compliance officer confirmed the discrepancy is a genuine translation error -- corrected text must be re-ingested before compilation
    DISMISSED = "dismissed"  # compliance officer reviewed and judged it a false positive (e.g. a benign unit-word rendering difference)


class DiscrepancyCase(BaseModel):
    """One Redis-queue item -- one circular's full parity report plus
    its rendered side-by-side HTML diffs, awaiting compliance-officer
    triage. Mirrors app.execution.models.HITLCase's shape (case_id,
    payload, reason, status, resolution fields) for the same
    "execution/ingestion-time, transient, ops-workflow item" precedent
    that queue already establishes -- see app.translation_parity.queue's
    module docstring for why this is Redis-backed rather than a new
    Postgres table."""

    case_id: str
    report: TranslationParityReport
    diff_html_by_clause_pair: dict[str, str] = Field(default_factory=dict, description="Key is 'en_clause|hi_clause' (either side may be empty for a missing-clause pairing); value is the rendered side-by-side HTML diff fragment.")
    status: DiscrepancyReviewStatus = DiscrepancyReviewStatus.PENDING
    created_at: dt.datetime = Field(default_factory=lambda: dt.datetime.now(dt.timezone.utc))
    resolved_at: dt.datetime | None = None
    resolved_by: str | None = None
    resolution_notes: str | None = None
