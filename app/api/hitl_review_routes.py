"""The clause-level HITL review portal: approving or rejecting a compiled
policy (app.db.models.CompiledRule) that the compiler flagged
(app.db.models.HITLReview) as blocked or advisory before it may be
activated and published to OPA.

Every mutating endpoint here requires the Compliance_Officer role
EXCLUSIVELY -- not System_Admin, not even a token holding both roles
bypasses the check by having Compliance_Officer too (it still must). This
is the literal control this module exists to enforce: infrastructure
access and compliance sign-off authority are different privileges, and
holding one must never imply the other (see app.security.models.Role's
docstring for the separation-of-duties rationale).

Distinct from app.api.execution_routes' /v1/execution/hitl/cases/* -- those
resolve an ambiguous LIVE TRANSACTION (app.execution.models.HITLCase,
Redis-backed); this resolves an ambiguous CLAUSE/POLICY VERSION
(app.db.models.HITLReview, Postgres-backed) before it is ever compiled
into something a transaction could be evaluated against.
"""
from __future__ import annotations

import datetime as dt
import logging

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.backtest.models import RuleImpactPreviewReport
from app.case_law.models import CaseLawAnalysisResult
from app.db.models import Clause, CompiledRule, HITLReview
from app.db.session import get_db_session
from app.execution.dependencies import get_policy_publisher
from app.execution.policy_publisher import PolicyPublisher
from app.security.dependencies import require_roles
from app.security.models import Principal, Role
from app.security.step_up import require_step_up_mfa
from app.services.hitl_service import HITLReviewService

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/hitl-reviews", tags=["hitl-review-portal"])


class HITLReviewOut(BaseModel):
    id: int
    review_id: str
    clause_id: int
    compiled_rule_id: int | None
    reason_code: str
    severity: str
    description: str
    source_excerpt: str | None
    status: str
    compliance_officer_id: str | None
    resolution_notes: str | None
    flagged_at: dt.datetime
    resolved_at: dt.datetime | None
    approved_rule_version: int | None = None
    approved_policy_sha256: str | None = None

    model_config = {"from_attributes": True}


class ReviewResolutionRequest(BaseModel):
    notes: str | None = Field(None, max_length=4000)


async def _get_review_or_404(session: AsyncSession, review_id: str) -> HITLReview:
    result = await session.execute(select(HITLReview).where(HITLReview.review_id == review_id))
    review = result.scalar_one_or_none()
    if review is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"No HITL review '{review_id}'.")
    return review


@router.get("", response_model=list[HITLReviewOut])
async def list_reviews(
    status_filter: str | None = None,
    session: AsyncSession = Depends(get_db_session),
    # Read access is broader than approval authority: an admin auditing
    # the queue's backlog is a legitimate, common operational need.
    _principal: Principal = Depends(require_roles(Role.COMPLIANCE_OFFICER, Role.SYSTEM_ADMIN)),
) -> list[HITLReview]:
    query = select(HITLReview).order_by(HITLReview.flagged_at.asc())
    if status_filter:
        query = query.where(HITLReview.status == status_filter)
    result = await session.execute(query)
    return list(result.scalars().all())


@router.get("/{review_id}", response_model=HITLReviewOut)
async def get_review(
    review_id: str,
    session: AsyncSession = Depends(get_db_session),
    _principal: Principal = Depends(require_roles(Role.COMPLIANCE_OFFICER, Role.SYSTEM_ADMIN)),
) -> HITLReview:
    return await _get_review_or_404(session, review_id)


@router.post("/{review_id}/approve", response_model=HITLReviewOut)
async def approve_review(
    review_id: str,
    resolution: ReviewResolutionRequest,
    session: AsyncSession = Depends(get_db_session),
    principal: Principal = Depends(require_roles(Role.COMPLIANCE_OFFICER)),
    _stepped_up: Principal = Depends(require_step_up_mfa),
    policy_publisher: PolicyPublisher = Depends(get_policy_publisher),
) -> HITLReview:
    """Approves a HITL review.

    Gating Logic:
    A CompiledRule is ONLY activated (is_active=True, hitl_status='RESOLVED')
    if EVERY required blocking review for that rule has been resolved and approved,
    and no review is rejected or revision-required. If other blocking reviews remain,
    the review itself is marked RESOLVED, but the CompiledRule remains inactive
    and un-published until all reviews have cleared the gate.

    Requires step-up MFA (app.security.step_up.require_step_up_mfa).
    """
    review, _rule_activated, _compiled_rule = await HITLReviewService.approve_review(
        session=session,
        review_id=review_id,
        principal_subject=principal.subject,
        notes=resolution.notes,
        policy_publisher=policy_publisher,
    )
    return review


@router.post("/{review_id}/reject", response_model=HITLReviewOut)
async def reject_review(
    review_id: str,
    resolution: ReviewResolutionRequest,
    session: AsyncSession = Depends(get_db_session),
    principal: Principal = Depends(require_roles(Role.COMPLIANCE_OFFICER)),
) -> HITLReview:
    """Rejects the compiled policy review: it is never activated. The clause
    stays flagged for re-extraction/manual authoring."""
    return await HITLReviewService.reject_review(
        session=session,
        review_id=review_id,
        principal_subject=principal.subject,
        notes=resolution.notes,
    )


@router.post("/{review_id}/request-revision", response_model=HITLReviewOut)
@router.post("/{review_id}/revision-required", response_model=HITLReviewOut, include_in_schema=False)
async def request_revision_review(
    review_id: str,
    resolution: ReviewResolutionRequest,
    session: AsyncSession = Depends(get_db_session),
    principal: Principal = Depends(require_roles(Role.COMPLIANCE_OFFICER)),
) -> HITLReview:
    """Flags the review as requiring revision: the policy is not activated
    and routes back for re-extraction."""
    return await HITLReviewService.request_revision(
        session=session,
        review_id=review_id,
        principal_subject=principal.subject,
        notes=resolution.notes,
    )


@router.get("/{review_id}/precedents", response_model=CaseLawAnalysisResult)
async def get_review_precedents(
    review_id: str,
    top_k: int = 3,
    min_similarity: float = 0.70,
    session: AsyncSession = Depends(get_db_session),
    _principal: Principal = Depends(require_roles(Role.COMPLIANCE_OFFICER, Role.SYSTEM_ADMIN)),
) -> CaseLawAnalysisResult:
    """Retrieves semantically similar approved case-law precedents to assist the officer
    reviewing this HITL review case.

    SAFETY GUARANTEE: Precedents are strictly advisory and cannot override current regulatory text.
    """
    review = await _get_review_or_404(session, review_id)
    clause = review.clause
    if clause is None and review.clause_id:
        clause = await session.get(Clause, review.clause_id)

    clause_text = (clause.text if clause else None) or review.source_excerpt or review.description
    clause_num = clause.clause_number if clause else None

    from app.case_law import get_case_law_agent

    agent = get_case_law_agent()
    return await agent.analyze_clause(
        clause_text=clause_text,
        clause_number=clause_num,
        tenant_id=review.tenant_id,
        top_k=top_k,
        min_similarity=min_similarity,
    )


# --- Rule-Impact Preview / Digital Twin HITL Endpoints (PRD Section 8.4) ---

@router.post("/{review_id}/preview-impact", response_model=RuleImpactPreviewReport)
async def trigger_review_impact_preview(
    review_id: str,
    lookback_days: int = Query(30, ge=1, le=365),
    max_transactions: int = Query(5000, ge=1, le=50000),
    session: AsyncSession = Depends(get_db_session),
    _principal: Principal = Depends(require_roles(Role.COMPLIANCE_OFFICER, Role.SYSTEM_ADMIN)),
) -> RuleImpactPreviewReport:
    """Executes on-demand real-data rule-impact preview / digital twin replay for the candidate
    CompiledRule associated with this HITL review case.

    SAFETY INVARIANTS:
    1. Candidate CompiledRule is NEVER deployed or activated (is_active remains False).
    2. Zero historical impact does NOT constitute approval and does NOT infer regulatory correctness.
    3. Historical ledger records are strictly read-only and immutable.
    4. Evaluates with strict tenant isolation (review.tenant_id).
    """
    from app.backtest.models import PreviewDatasetScope, PreviewRunRequest, RuleImpactPreviewReport
    from app.backtest.orchestrator import run_rule_impact_preview
    from app.backtest.tasks import save_preview_report
    from app.config import get_settings
    from app.ledger.db import get_ledger_engine

    review = await _get_review_or_404(session, review_id)
    if review.compiled_rule_id is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"HITL review '{review_id}' has no associated compiled rule to preview.",
        )

    compiled_rule = await session.get(CompiledRule, review.compiled_rule_id)
    if compiled_rule is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Associated CompiledRule '{review.compiled_rule_id}' was not found.",
        )

    if compiled_rule.jsonlogic_ast is None and compiled_rule.opa_package_name is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"CompiledRule '{compiled_rule.rule_id}' has neither jsonlogic_ast nor opa_package_name.",
        )

    scope = PreviewDatasetScope(
        tenant_id=review.tenant_id,
        rule_id=compiled_rule.rule_id,
        lookback_days=lookback_days,
        max_transactions=max_transactions,
    )

    request = PreviewRunRequest(
        candidate_rule_id=compiled_rule.rule_id,
        candidate_jsonlogic_ast=compiled_rule.jsonlogic_ast,
        candidate_opa_package=compiled_rule.opa_package_name,
        candidate_compiled_rule_id=compiled_rule.id,
        scope=scope,
    )

    settings = get_settings()
    # Use session's bind engine if available (e.g. in test suites), otherwise default ledger engine
    ledger_engine = getattr(session, "bind", None) or get_ledger_engine()

    report = await run_rule_impact_preview(
        request=request,
        settings=settings,
        ledger_engine=ledger_engine,
        preview_id=f"preview-{review.review_id}",
    )

    # Cache report keyed by review_id
    save_preview_report(report)

    # Invariant assertion: guarantee candidate rule remains un-activated
    assert compiled_rule.is_active is False, "CRITICAL SAFETY VIOLATION: Candidate rule was activated during preview"

    return report


@router.get("/{review_id}/preview-impact", response_model=RuleImpactPreviewReport)
async def get_review_impact_preview(
    review_id: str,
    session: AsyncSession = Depends(get_db_session),
    _principal: Principal = Depends(require_roles(Role.COMPLIANCE_OFFICER, Role.SYSTEM_ADMIN)),
) -> RuleImpactPreviewReport:
    """Retrieves the latest cached RuleImpactPreviewReport for this HITL review screen."""
    from app.backtest.tasks import get_preview_report

    review = await _get_review_or_404(session, review_id)
    report = get_preview_report(f"preview-{review.review_id}")
    if report is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No impact preview report found for review '{review_id}'. Run POST /v1/hitl-reviews/{review_id}/preview-impact first.",
        )
    return report

