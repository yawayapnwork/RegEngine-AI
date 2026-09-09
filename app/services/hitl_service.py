"""HITL Review Service encapsulating approval gates, status evaluation,
and concurrency-safe activation for CompiledRule policies.

Rules:
1. A CompiledRule can NEVER become active while any blocking or unresolved
   HITLReview for that same rule remains.
2. If any review is REJECTED or REVISION_REQUIRED, the rule can never activate.
3. Operations acquire database row locks (`SELECT ... FOR UPDATE`) to prevent
   race conditions between simultaneous approvals or conflicting resolutions.
"""
from __future__ import annotations

import asyncio
from collections import defaultdict
from contextlib import asynccontextmanager
import datetime as dt
import hashlib
import logging
from typing import Sequence

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Circular, CircularStateTransition, Clause, CompiledRule, HITLReview
from app.execution.policy_publisher import PolicyPublisher

logger = logging.getLogger(__name__)

_RULE_LOCKS: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
_GLOBAL_LOCK = asyncio.Lock()


@asynccontextmanager
async def _rule_scope_lock(rule_id: int | None):
    if rule_id is None:
        yield
        return
    async with _GLOBAL_LOCK:
        lock = _RULE_LOCKS[rule_id]
    async with lock:
        yield

# Status classifications
UNRESOLVED_STATUSES = frozenset({"PENDING", "IN_REVIEW"})
DISQUALIFYING_STATUSES = frozenset({"REJECTED", "REVISION_REQUIRED", "needs_revision"})
APPROVED_STATUS = "RESOLVED"
BLOCKING_SEVERITY = "blocking"
ADVISORY_SEVERITY = "advisory"


def evaluate_rule_hitl_gate(
    reviews: Sequence[HITLReview],
) -> tuple[bool, str, str | None]:
    """Evaluates whether all reviews for a CompiledRule have passed the HITL gate.

    Returns:
        (can_activate, target_hitl_status, reason_message)
    """
    if not reviews:
        return True, "RESOLVED", None

    # Check for hard disqualifications (rejections or revisions requested)
    for r in reviews:
        status_norm = (r.status or "").upper()
        if status_norm == "REJECTED":
            return False, "BLOCKING", f"Review '{r.review_id}' was REJECTED."
        if status_norm in ("REVISION_REQUIRED", "NEEDS_REVISION"):
            return False, "BLOCKING", f"Review '{r.review_id}' requires revision."

    # Check for unresolved blocking reviews
    unresolved_blocking = [
        r for r in reviews
        if (r.severity or "").lower() == BLOCKING_SEVERITY and (r.status or "").upper() != APPROVED_STATUS
    ]
    if unresolved_blocking:
        ids = [r.review_id for r in unresolved_blocking]
        return False, "BLOCKING", f"Blocking review(s) remaining unresolved: {ids}."

    # Check for remaining unresolved advisory reviews
    unresolved_advisory = [
        r for r in reviews
        if (r.status or "").upper() in UNRESOLVED_STATUSES
    ]
    if unresolved_advisory:
        ids = [r.review_id for r in unresolved_advisory]
        return False, "ADVISORY", f"Advisory review(s) remaining unresolved: {ids}."

    # Verify all reviews have reached APPROVED_STATUS
    non_approved = [
        r for r in reviews
        if (r.status or "").upper() != APPROVED_STATUS
    ]
    if non_approved:
        ids = [r.review_id for r in non_approved]
        return False, "BLOCKING", f"Review(s) not approved: {ids}."

    return True, "RESOLVED", None


class HITLReviewService:
    """Service providing safe, transactional resolution and approval gating for HITL reviews."""

    @staticmethod
    async def get_review_or_404(session: AsyncSession, review_id: str) -> HITLReview:
        result = await session.execute(select(HITLReview).where(HITLReview.review_id == review_id))
        review = result.scalar_one_or_none()
        if review is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"No HITL review '{review_id}'.",
            )
        return review

    @classmethod
    async def approve_review(
        cls,
        session: AsyncSession,
        review_id: str,
        principal_subject: str,
        notes: str | None = None,
        policy_publisher: PolicyPublisher | None = None,
    ) -> tuple[HITLReview, bool, CompiledRule | None]:
        """Approves a single HITL review.

        If the review is tied to a CompiledRule:
        - Locks the CompiledRule and all associated HITLReview records.
        - Updates this review to 'RESOLVED'.
        - Checks if ALL required blocking reviews for the rule are resolved.
        - If and only if the full gate passes, activates the CompiledRule and publishes to OPA.
        - Otherwise, keeps CompiledRule inactive.

        Returns:
            (review, rule_activated, compiled_rule)
        """
        initial_review = await cls.get_review_or_404(session, review_id)
        if initial_review.status != "PENDING":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Review '{review_id}' is already '{initial_review.status}'; cannot re-approve.",
            )

        async with _rule_scope_lock(initial_review.compiled_rule_id):
            # Inside the lock, re-fetch the review to guarantee we have the latest committed state
            review = await cls.get_review_or_404(session, review_id)
            if review.status != "PENDING":
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"Review '{review_id}' is already '{review.status}'; cannot re-approve.",
                )

            compiled_rule: CompiledRule | None = None
            rule_activated = False

            if review.compiled_rule_id is not None:
                # Concurrency safety: acquire exclusive row lock on CompiledRule
                stmt_rule = (
                    select(CompiledRule)
                    .where(CompiledRule.id == review.compiled_rule_id)
                    .with_for_update()
                )
                compiled_rule = (await session.execute(stmt_rule)).scalar_one_or_none()
                if compiled_rule is None:
                    raise HTTPException(
                        status_code=status.HTTP_404_NOT_FOUND,
                        detail="Associated compiled_rules row no longer exists.",
                    )

                # Lock and fetch all sibling reviews for the same CompiledRule
                stmt_reviews = (
                    select(HITLReview)
                    .where(HITLReview.compiled_rule_id == compiled_rule.id)
                    .with_for_update()
                )
                all_reviews = list((await session.execute(stmt_reviews)).scalars().all())

                # Mark this review resolved
                now_utc = dt.datetime.now(dt.timezone.utc)
                review.status = APPROVED_STATUS
                review.compliance_officer_id = principal_subject
                review.resolution_notes = notes
                review.resolved_at = now_utc
                review.approved_rule_version = compiled_rule.rule_version
                policy_hash = compiled_rule.policy_sha256
                if not policy_hash and compiled_rule.rego_policy:
                    policy_hash = hashlib.sha256(compiled_rule.rego_policy.encode("utf-8")).hexdigest()
                    compiled_rule.policy_sha256 = policy_hash
                review.approved_policy_sha256 = policy_hash

                # Evaluate gate across all sibling reviews (including the now-resolved review)
                can_activate, target_status, gate_reason = evaluate_rule_hitl_gate(all_reviews)

                if can_activate:
                    # Deactivate older active versions of the same rule_id
                    await session.execute(
                        CompiledRule.__table__.update()
                        .where(
                            CompiledRule.rule_id == compiled_rule.rule_id,
                            CompiledRule.id != compiled_rule.id,
                        )
                        .values(is_active=False)
                    )
                    compiled_rule.is_active = True
                    compiled_rule.hitl_status = "RESOLVED"
                    rule_activated = True
                    logger.info(
                        "All required HITL reviews for rule '%s' passed gate. Rule ACTIVATED.",
                        compiled_rule.rule_id,
                    )
                else:
                    compiled_rule.is_active = False
                    compiled_rule.hitl_status = target_status
                    rule_activated = False
                    logger.info(
                        "Review '%s' approved, but CompiledRule '%s' remains inactive (%s): %s",
                        review_id,
                        compiled_rule.rule_id,
                        target_status,
                        gate_reason,
                    )
            else:
                now_utc = dt.datetime.now(dt.timezone.utc)
                review.status = APPROVED_STATUS
                review.compliance_officer_id = principal_subject
                review.resolution_notes = notes
                review.resolved_at = now_utc

            # Check if this resolves all pending reviews for the parent circular
            if review.clause_id:
                clause = await session.get(Clause, review.clause_id)
                if clause and clause.circular_id:
                    circular = await session.get(Circular, clause.circular_id)
                    if circular and circular.processing_state == "AWAITING_HITL":
                        circular_clause_ids = list(
                            (
                                await session.execute(
                                    select(Clause.id).where(Clause.circular_id == circular.id)
                                )
                            ).scalars().all()
                        )
                        if circular_clause_ids:
                            all_circ_reviews = list(
                                (
                                    await session.execute(
                                        select(HITLReview).where(HITLReview.clause_id.in_(circular_clause_ids))
                                    )
                                ).scalars().all()
                            )
                            unresolved = [
                                r for r in all_circ_reviews
                                if (r.id == review.id and review.status != APPROVED_STATUS)
                                or (r.id != review.id and r.status in UNRESOLVED_STATUSES)
                            ]
                            disqualified = [
                                r for r in all_circ_reviews
                                if r.status in DISQUALIFYING_STATUSES
                            ]
                            if not unresolved and not disqualified:
                                circular.processing_state = "APPROVED"
                                session.add(
                                    CircularStateTransition(
                                        circular_id=circular.id,
                                        from_state="AWAITING_HITL",
                                        to_state="APPROVED",
                                        triggered_by=principal_subject,
                                        details={"review_id": review_id, "reason": "All HITL reviews approved"},
                                    )
                                )

            await session.commit()
            await session.refresh(review)
            if compiled_rule is not None:
                await session.refresh(compiled_rule)

        logger.info(
            "HITL review '%s' APPROVED by compliance officer '%s' (rule_activated=%s)",
            review_id,
            principal_subject,
            rule_activated,
        )

        # Publish OPA event only if the rule was actually activated and has compiled Rego
        if rule_activated and compiled_rule is not None and compiled_rule.rego_policy and policy_publisher is not None:
            try:
                await policy_publisher.publish_approved(compiled_rule, approved_by=principal_subject)
            except Exception:
                logger.exception(
                    "Approval of review '%s' committed, but publishing its PolicyEvent failed -- "
                    "OPA hot-reload for rule_id=%s will lag until the next event or cache TTL expiry.",
                    review_id,
                    compiled_rule.rule_id,
                )

        # Index approved review as historical case-law precedent if memory subsystem is enabled
        settings = get_settings()
        if getattr(settings, "case_law_memory_enabled", True):
            try:
                from app.case_law.indexer import index_approved_hitl_review
                await index_approved_hitl_review(session, review, settings=settings)
            except Exception as e:
                logger.warning(
                    "Approval of review '%s' succeeded, but indexing into case-law memory failed: %s",
                    review_id,
                    e,
                )

        return review, rule_activated, compiled_rule

    @classmethod
    async def reject_review(
        cls,
        session: AsyncSession,
        review_id: str,
        principal_subject: str,
        notes: str | None = None,
    ) -> HITLReview:
        """Rejects a HITL review. The associated CompiledRule cannot be active."""
        initial_review = await cls.get_review_or_404(session, review_id)
        if initial_review.status != "PENDING":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Review '{review_id}' is already '{initial_review.status}'; cannot re-reject.",
            )

        async with _rule_scope_lock(initial_review.compiled_rule_id):
            review = await cls.get_review_or_404(session, review_id)
            if review.status != "PENDING":
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"Review '{review_id}' is already '{review.status}'; cannot re-reject.",
                )

            now_utc = dt.datetime.now(dt.timezone.utc)

            if review.compiled_rule_id is not None:
                stmt_rule = (
                    select(CompiledRule)
                    .where(CompiledRule.id == review.compiled_rule_id)
                    .with_for_update()
                )
                compiled_rule = (await session.execute(stmt_rule)).scalar_one_or_none()
                if compiled_rule is not None:
                    compiled_rule.is_active = False
                    compiled_rule.hitl_status = "BLOCKING"

            review.status = "REJECTED"
            review.compliance_officer_id = principal_subject
            review.resolution_notes = notes
            review.resolved_at = now_utc

            if review.clause_id:
                clause = await session.get(Clause, review.clause_id)
                if clause and clause.circular_id:
                    circular = await session.get(Circular, clause.circular_id)
                    if circular and circular.processing_state != "FAILED":
                        old_state = circular.processing_state
                        circular.processing_state = "FAILED"
                        circular.error_message = f"HITL review '{review.review_id}' rejected by {principal_subject}"
                        session.add(
                            CircularStateTransition(
                                circular_id=circular.id,
                                from_state=old_state,
                                to_state="FAILED",
                                triggered_by=principal_subject,
                                error_message=circular.error_message,
                                details={"review_id": review.review_id},
                            )
                        )

            await session.commit()
            await session.refresh(review)
            logger.info(
                "HITL review '%s' REJECTED by compliance officer '%s'",
                review_id,
                principal_subject,
            )
            return review

    @classmethod
    async def request_revision(
        cls,
        session: AsyncSession,
        review_id: str,
        principal_subject: str,
        notes: str | None = None,
    ) -> HITLReview:
        """Flags a HITL review as REVISION_REQUIRED. The associated CompiledRule cannot be active."""
        initial_review = await cls.get_review_or_404(session, review_id)
        if initial_review.status != "PENDING":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Review '{review_id}' is already '{initial_review.status}'; cannot request revision.",
            )

        async with _rule_scope_lock(initial_review.compiled_rule_id):
            review = await cls.get_review_or_404(session, review_id)
            if review.status != "PENDING":
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"Review '{review_id}' is already '{review.status}'; cannot request revision.",
                )

            now_utc = dt.datetime.now(dt.timezone.utc)

            if review.compiled_rule_id is not None:
                stmt_rule = (
                    select(CompiledRule)
                    .where(CompiledRule.id == review.compiled_rule_id)
                    .with_for_update()
                )
                compiled_rule = (await session.execute(stmt_rule)).scalar_one_or_none()
                if compiled_rule is not None:
                    compiled_rule.is_active = False
                    compiled_rule.hitl_status = "BLOCKING"

            review.status = "REVISION_REQUIRED"
            review.compliance_officer_id = principal_subject
            review.resolution_notes = notes
            review.resolved_at = now_utc

            if review.clause_id:
                clause = await session.get(Clause, review.clause_id)
                if clause and clause.circular_id:
                    circular = await session.get(Circular, clause.circular_id)
                    if circular and circular.processing_state in ("AWAITING_HITL", "APPROVED"):
                        old_state = circular.processing_state
                        circular.processing_state = "EXTRACTING"
                        session.add(
                            CircularStateTransition(
                                circular_id=circular.id,
                                from_state=old_state,
                                to_state="EXTRACTING",
                                triggered_by=principal_subject,
                                details={"review_id": review.review_id, "reason": "Revision requested"},
                            )
                        )

            await session.commit()
            await session.refresh(review)
            logger.info(
                "HITL review '%s' marked REVISION_REQUIRED by compliance officer '%s'",
                review_id,
                principal_subject,
            )
            return review
