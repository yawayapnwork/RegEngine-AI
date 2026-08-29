"""Pydantic schema mappings for SEBI SCORES 2.0 -- see this package's
`__init__.py` for the full, important disclosure on why these are a
best-effort, clearly-labeled interface contract rather than a verified
transcription of SEBI's real published API. Every field below is
annotated with WHERE its shape comes from (a general, publicly known
SCORES grievance-workflow concept vs. this codebase's own invention for
internal bookkeeping) so a future engineer reconciling this against the
real spec knows exactly what to check first.
"""
from __future__ import annotations

import datetime as dt
from enum import Enum

from pydantic import BaseModel, Field


class GrievanceCategory(str, Enum):
    """SCORES groups grievances by category/sub-category (a real,
    documented SCORES concept) -- this enum covers only the categories
    this agent itself ever files under (systemic broker non-compliance
    detected by automated ledger monitoring), not the full category
    taxonomy SCORES exposes to a human investor filing a complaint by
    hand. Confirm the exact category CODE (not just the label) SEBI's
    real API expects before going live -- this enum's `.value` strings
    are human-readable placeholders, not confirmed API enum codes."""

    BROKER_SYSTEMIC_NON_COMPLIANCE = "broker_systemic_non_compliance"
    DELAYED_COLLATERAL_REPORTING = "delayed_collateral_reporting"
    REPEATED_CLAUSE_VIOLATION = "repeated_clause_violation"


class GrievanceRespondent(BaseModel):
    """The broker/intermediary the grievance concerns."""

    sebi_registration_number: str
    broker_id: str = Field(..., description="This platform's internal broker_id -- carried alongside sebi_registration_number for RegEngine-side cross-referencing; not itself a SCORES field.")
    entity_name: str | None = None


class GrievanceComplainant(BaseModel):
    """SCORES requires an identified complainant. For a grievance this
    agent files automatically (not a human investor), the complainant
    is the compliance-monitoring entity/tenant operating this platform
    -- populated from `settings`/tenant configuration, never left blank
    (SCORES filings are not anonymous)."""

    entity_name: str
    contact_email: str
    tenant_id: str | None = None


class GrievanceEvidenceDocument(BaseModel):
    """One piece of supporting evidence attached to the grievance.
    SCORES' real API almost certainly expects a file UPLOAD (multipart)
    for supporting documents, not inline JSON -- this model represents
    the evidence as structured, inlineable JSON/text specifically
    because `app.grievance_escalation.evidence`'s package (clause hash,
    transaction payload, ledger proof) is itself structured data, not a
    scanned document; `scores_client.py` is the seam where this would
    be rendered to whatever file format SCORES' real upload endpoint
    requires (e.g. a signed PDF or JSON attachment) before going live."""

    label: str
    content_type: str = "application/json"
    content: str = Field(..., description="The evidence content, JSON-encoded as a string for this document.")
    sha256: str = Field(..., description="SHA-256 of `content` (UTF-8 bytes) -- lets a reviewer verify this document's integrity independent of transport.")


class GrievanceSubmissionRequest(BaseModel):
    """The outbound payload `scores_client.submit_grievance` sends.
    Field NAMES below (`category`, `respondent`, `complainant`,
    `description`, `evidence`, `reference_id`) are this codebase's own
    naming, chosen for readability -- they are NOT asserted to match
    SEBI's real request body's exact JSON keys; see this module's
    docstring."""

    reference_id: str = Field(..., description="This platform's own grievance_id (see queue.py) -- sent so SCORES' acknowledgment can be correlated back to our internal record, assuming SCORES' real API supports a client-supplied reference/external-id field (a common but unconfirmed pattern).")
    category: GrievanceCategory
    respondent: GrievanceRespondent
    complainant: GrievanceComplainant
    description: str
    evidence: list[GrievanceEvidenceDocument]
    filed_at: dt.datetime = Field(default_factory=lambda: dt.datetime.now(dt.timezone.utc))


class ScoresGrievanceStatus(str, Enum):
    """SCORES' real status vocabulary is presumed to be richer than
    this (e.g. distinct "pending with company" vs "pending with SEBI"
    sub-states are a real, documented SCORES concept) -- this enum
    covers only the states this agent's own status-tracking state
    machine (queue.py's GrievanceStatus) needs to distinguish, mapped
    from whatever SCORES' real status field actually returns via
    `scores_client._map_scores_status` (see that function's docstring
    on why unrecognized values fail closed to UNKNOWN rather than being
    silently treated as any particular known state)."""

    SUBMITTED = "submitted"
    UNDER_REVIEW = "under_review"
    PENDING_WITH_RESPONDENT = "pending_with_respondent"
    RESOLVED = "resolved"
    REJECTED = "rejected"
    UNKNOWN = "unknown"


class GrievanceSubmissionResponse(BaseModel):
    """SCORES' real submission acknowledgment shape -- assumed to
    return SOME kind of tracking number synchronously on submission
    (`scores_reference_number`) per typical grievance-portal behavior;
    confirm this against the real API before going live."""

    scores_reference_number: str
    status: ScoresGrievanceStatus = ScoresGrievanceStatus.SUBMITTED
    acknowledged_at: dt.datetime = Field(default_factory=lambda: dt.datetime.now(dt.timezone.utc))
    raw_response: str | None = Field(None, description="The raw JSON response body, kept for audit/debugging regardless of how well this model's typed fields matched it.")


class GrievanceStatusResponse(BaseModel):
    """The shape `scores_client.get_grievance_status` returns from
    SCORES' (presumed) status-polling endpoint."""

    scores_reference_number: str
    status: ScoresGrievanceStatus
    last_updated_at: dt.datetime
    resolution_summary: str | None = None
    raw_response: str | None = None
