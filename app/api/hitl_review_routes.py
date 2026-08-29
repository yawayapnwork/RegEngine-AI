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

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import CompiledRule, HITLReview
from app.db.session import get_db_session
from app.execution.dependencies import get_policy_publisher
from app.execution.policy_publisher import PolicyPublisher
from app.security.dependencies import require_roles
from app.security.models import Principal, Role
from app.security.step_up import require_step_up_mfa

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
    """Approves the compiled policy this review concerns: activates its
    CompiledRule version (deactivating any prior active version of the
    same rule_id, so exactly one version is ever live -- see
    app.db.models.CompiledRule's partial-unique-index comment), marks the
    review RESOLVED under this officer's identity, and publishes a
    PolicyEvent so every FastAPI worker's PolicyHotReloadSubscriber
    hot-reloads OPA within its next pub/sub poll -- typically low
    milliseconds, never a restart. Publishing happens AFTER the DB commit
    (not inside the same transaction): a Redis PUBLISH cannot be rolled
    back, so it must only ever fire once the approval it describes is
    durably true, not before.

    Requires step-up MFA (app.security.step_up.require_step_up_mfa): a
    valid Compliance_Officer token alone is not sufficient here -- the
    token's underlying authentication event must also be recent and
    MFA-satisfying (fresh `auth_time` + a qualifying `amr` value), since
    this action activates a policy that governs live production trade
    evaluation. `reject_review` below does not require step-up: declining
    to activate something carries materially lower risk than approving it."""
    review = await _get_review_or_404(session, review_id)
    if review.status != "PENDING":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Review '{review_id}' is already '{review.status}'; cannot re-approve.",
        )

    compiled_rule: CompiledRule | None = None
    if review.compiled_rule_id is not None:
        compiled_rule = await session.get(CompiledRule, review.compiled_rule_id)
        if compiled_rule is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Associated compiled_rules row no longer exists.")

        await session.execute(
            CompiledRule.__table__.update()
            .where(CompiledRule.rule_id == compiled_rule.rule_id, CompiledRule.id != compiled_rule.id)
            .values(is_active=False)
        )
        compiled_rule.is_active = True
        compiled_rule.hitl_status = "RESOLVED"

    review.status = "RESOLVED"
    review.compliance_officer_id = principal.subject
    review.resolution_notes = resolution.notes
    review.resolved_at = dt.datetime.now(dt.timezone.utc)

    await session.commit()
    await session.refresh(review)
    logger.info("HITL review '%s' APPROVED by compliance officer '%s'", review_id, principal.subject)

    if compiled_rule is not None:
        try:
            await policy_publisher.publish_approved(compiled_rule, approved_by=principal.subject)
        except Exception:  # noqa: BLE001 - the approval itself is already durably committed; a pub/sub
            # publish failure must never turn into a 500 that makes the officer think approval didn't
            # happen. PolicyCache's TTL safety net (app/execution/policy_cache.py) still bounds staleness.
            logger.exception(
                "Approval of review '%s' committed, but publishing its PolicyEvent failed -- "
                "OPA hot-reload for rule_id=%s will lag until the next event or cache TTL expiry.",
                review_id, compiled_rule.rule_id,
            )

    return review


@router.post("/{review_id}/reject", response_model=HITLReviewOut)
async def reject_review(
    review_id: str,
    resolution: ReviewResolutionRequest,
    session: AsyncSession = Depends(get_db_session),
    principal: Principal = Depends(require_roles(Role.COMPLIANCE_OFFICER)),
) -> HITLReview:
    """Rejects the compiled policy: it is never activated. The clause
    stays flagged for re-extraction/manual authoring; this does not delete
    the CompiledRule row (kept for audit trail of what was proposed and
    why it was refused)."""
    review = await _get_review_or_404(session, review_id)
    if review.status != "PENDING":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Review '{review_id}' is already '{review.status}'; cannot re-reject.",
        )

    review.status = "REJECTED"
    review.compliance_officer_id = principal.subject
    review.resolution_notes = resolution.notes
    review.resolved_at = dt.datetime.now(dt.timezone.utc)

    await session.commit()
    await session.refresh(review)
    logger.info("HITL review '%s' REJECTED by compliance officer '%s'", review_id, principal.subject)
    return review
