"""Requirement 3's compliance-officer review dashboard API: run a
cross-lingual parity check on an English/Hindi circular pair, list/get
pending discrepancy cases with their rendered side-by-side HTML diffs,
and resolve them.

Structurally mirrors app.api.execution_routes' `/v1/execution/hitl/cases/*`
routes (Redis-backed queue, same list/get/resolve shape) rather than
app.api.hitl_review_routes (Postgres-backed `HITLReview`) -- see
app.translation_parity.queue's module docstring for why this queue is
Redis-backed. Every route requires the Compliance_Officer role, same
separation-of-duties rationale as app.api.hitl_review_routes: a
translation-parity finding is a legal-content judgment call, not an
infrastructure operation System_Admin should be able to resolve alone.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.config import Settings, get_settings
from app.translation_parity.checker import SemanticParityChecker
from app.translation_parity.dependencies import get_semantic_parity_checker, get_translation_discrepancy_queue
from app.translation_parity.diff_rendering import render_side_by_side_diff
from app.translation_parity.models import ClauseRef, DiscrepancyCase, DiscrepancyReviewStatus, TranslationParityReport
from app.translation_parity.queue import TranslationDiscrepancyQueue
from app.security.dependencies import require_roles
from app.security.models import Principal, Role
from app.models import ClauseChunk

router = APIRouter(prefix="/v1/translation-parity", tags=["translation-parity"])

_require_review_role = Depends(require_roles(Role.COMPLIANCE_OFFICER, Role.SYSTEM_ADMIN))


class ClauseInput(BaseModel):
    clause_number: str | None = None
    text: str


class ParityCheckRequest(BaseModel):
    circular_number: str
    english_clauses: list[ClauseInput] = Field(..., min_length=1)
    hindi_clauses: list[ClauseInput] = Field(..., min_length=1)


class ParityCheckResponse(BaseModel):
    report: TranslationParityReport
    case_id: str | None = Field(None, description="Set only when the report requires HITL review -- the discrepancy case's id in the review queue.")


class DiscrepancyResolutionRequest(BaseModel):
    status: DiscrepancyReviewStatus = Field(..., description="Must be 'approved' or 'dismissed' -- a case cannot be re-flagged as 'pending'.")
    notes: str | None = None


def _to_clause_chunks(clauses: list[ClauseInput]) -> list[ClauseChunk]:
    return [
        ClauseChunk(chunk_id=f"tmp:{i}", sha256="0" * 64, text=c.text, clause_number=c.clause_number)
        for i, c in enumerate(clauses)
    ]


def _diff_key(english_clause: ClauseRef | None, hindi_clause: ClauseRef | None) -> str:
    return f"{english_clause.clause_number if english_clause else ''}|{hindi_clause.clause_number if hindi_clause else ''}"


@router.post("/check", response_model=ParityCheckResponse, dependencies=[_require_review_role])
async def check_translation_parity(
    request: ParityCheckRequest,
    settings: Settings = Depends(get_settings),
    checker: SemanticParityChecker = Depends(get_semantic_parity_checker),
    discrepancy_queue: TranslationDiscrepancyQueue = Depends(get_translation_discrepancy_queue),
) -> ParityCheckResponse:
    """Runs the full parity check (Requirements 1 & 2) and, if any
    BLOCKING discrepancy is found, enqueues a review case with rendered
    side-by-side diffs (Requirement 3) -- the caller (e.g. the ingestion
    pipeline, before handing clauses to app.compiler) is expected to
    halt compilation for this circular whenever `case_id` is set."""
    if not settings.translation_parity_enabled:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Translation parity checking is disabled (translation_parity_enabled=false).")

    english_clauses = _to_clause_chunks(request.english_clauses)
    hindi_clauses = _to_clause_chunks(request.hindi_clauses)

    report = await checker.check(request.circular_number, english_clauses, hindi_clauses)

    case_id = None
    if report.requires_hitl_review:
        numeric_result_by_pair = {
            (d.english_clause_number, d.hindi_clause_number): d.verification.numeric_precision
            for d in report.discrepancies if d.verification is not None
        }
        diff_html_by_clause_pair = {}
        for alignment in report.alignments:
            en_number = alignment.english_clause.clause_number if alignment.english_clause else None
            hi_number = alignment.hindi_clause.clause_number if alignment.hindi_clause else None
            diff_html_by_clause_pair[_diff_key(alignment.english_clause, alignment.hindi_clause)] = render_side_by_side_diff(
                alignment.english_clause.text if alignment.english_clause else None,
                alignment.hindi_clause.text if alignment.hindi_clause else None,
                numeric_result_by_pair.get((en_number, hi_number)),
                english_clause_number=en_number,
                hindi_clause_number=hi_number,
            )
        case = await discrepancy_queue.enqueue(report, diff_html_by_clause_pair)
        case_id = case.case_id

    return ParityCheckResponse(report=report, case_id=case_id)


@router.get("/discrepancies", response_model=list[DiscrepancyCase], dependencies=[_require_review_role])
async def list_discrepancy_cases(discrepancy_queue: TranslationDiscrepancyQueue = Depends(get_translation_discrepancy_queue)) -> list[DiscrepancyCase]:
    return await discrepancy_queue.list_pending()


@router.get("/discrepancies/{case_id}", response_model=DiscrepancyCase, dependencies=[_require_review_role])
async def get_discrepancy_case(case_id: str, discrepancy_queue: TranslationDiscrepancyQueue = Depends(get_translation_discrepancy_queue)) -> DiscrepancyCase:
    case = await discrepancy_queue.get(case_id)
    if case is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"No translation discrepancy case '{case_id}'.")
    return case


@router.post("/discrepancies/{case_id}/resolve", response_model=DiscrepancyCase)
async def resolve_discrepancy_case(
    case_id: str,
    resolution: DiscrepancyResolutionRequest,
    discrepancy_queue: TranslationDiscrepancyQueue = Depends(get_translation_discrepancy_queue),
    principal: Principal = Depends(require_roles(Role.COMPLIANCE_OFFICER)),
) -> DiscrepancyCase:
    """Resolving as APPROVED confirms a genuine translation error (the
    circular must be corrected and re-submitted before compilation);
    DISMISSED records the compliance officer's judgment that the flag
    was a false positive. The authenticated principal is always the
    audit-trail `resolved_by`, never a request-body field, matching
    app.api.execution_routes.resolve_hitl_case's rationale. Unlike
    app.api.hitl_review_routes.approve_review, no step-up MFA is
    required here -- resolving a case never itself publishes anything
    to OPA (compilation is a separate, later step this queue only
    gates), the same reasoning app.api.execution_routes.resolve_hitl_case
    already applies to its own resolve endpoint."""
    if resolution.status not in (DiscrepancyReviewStatus.APPROVED, DiscrepancyReviewStatus.DISMISSED):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Resolution status must be 'approved' or 'dismissed'.")

    try:
        return await discrepancy_queue.resolve(case_id, resolution.status, principal.subject, resolution.notes)
    except KeyError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
