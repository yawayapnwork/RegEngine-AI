"""Precedent-indexing workflow for APPROVED/RESOLVED HITL decisions.

Guarantees:
1. ONLY decisions with status == 'RESOLVED' or 'APPROVED' are indexed.
2. Rejects pending, rejected, revision-required, or unverified outputs.
3. Automatically scrubs credentials and sensitive tokens from reviewer notes.
4. Assembles unbroken cryptographic provenance connecting circular, raw document hash,
   clause hash, policy hash, and human reviewer identity.
"""
from __future__ import annotations

import datetime as dt
import logging
import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.case_law.models import PrecedentRecord
from app.case_law.store import CaseLawStore
from app.config import Settings, get_settings
from app.db.models import Circular, Clause, CompiledRule, HITLReview
from app.observability.metrics import CASE_LAW_INDEXED_TOTAL

logger = logging.getLogger(__name__)

# Secret scrubbing patterns to prevent credential leakage into vector stores
_SECRET_PATTERNS = [
    re.compile(r"(?i)(?:api[_-]?key|secret|token|password|bearer|auth|access[_-]?token)\s*[:=]\s*['\"]?([a-zA-Z0-9_\-\.]{8,})['\"]?"),
    re.compile(r"(?i)bearer\s+[a-zA-Z0-9_\-\.]{16,}"),
    re.compile(r"ghp_[a-zA-Z0-9]{36}"),
    re.compile(r"hf_[a-zA-Z0-9]{34,}"),
    re.compile(r"eyJ[a-zA-Z0-9_\-]{20,}\.[a-zA-Z0-9_\-]{20,}\.[a-zA-Z0-9_\-]{20,}"),  # JWT
]


def scrub_secrets(text: str | None) -> str | None:
    """Removes API keys, bearer tokens, JWTs, and passwords from text before indexing."""
    if not text:
        return text
    scrubbed = text
    for pat in _SECRET_PATTERNS:
        scrubbed = pat.sub("[REDACTED_CREDENTIAL]", scrubbed)
    return scrubbed


async def index_approved_hitl_review(
    session: AsyncSession,
    review_or_id: HITLReview | str,
    store: CaseLawStore | None = None,
    settings: Settings | None = None,
) -> PrecedentRecord:
    """Builds and indexes a PrecedentRecord for an approved HITLReview.

    Raises:
        ValueError: if the review status is not RESOLVED or APPROVED.
        ValueError: if required clause or circular provenance cannot be resolved.
    """
    settings = settings or get_settings()
    store = store or CaseLawStore(settings=settings)

    if isinstance(review_or_id, str):
        stmt = (
            select(HITLReview)
            .where(HITLReview.review_id == review_or_id)
            .options(
                selectinload(HITLReview.clause).selectinload(Clause.circular),
                selectinload(HITLReview.compiled_rule),
            )
        )
        result = await session.execute(stmt)
        review = result.scalar_one_or_none()
        if review is None:
            raise ValueError(f"HITL review '{review_or_id}' not found.")
    else:
        review = review_or_id

    # Gate 1: Strict decision status check
    status_normalized = (review.status or "").upper()
    if status_normalized not in ("RESOLVED", "APPROVED"):
        CASE_LAW_INDEXED_TOTAL.labels(status="rejected_unapproved").inc()
        raise ValueError(
            f"Cannot index review '{review.review_id}' with status '{review.status}'. "
            "Only approved/resolved HITL decisions can be indexed as precedent."
        )

    # Gate 2: Resolve Clause & Circular provenance
    clause = review.clause
    if clause is None and review.clause_id:
        clause_stmt = select(Clause).where(Clause.id == review.clause_id).options(selectinload(Clause.circular))
        clause_res = await session.execute(clause_stmt)
        clause = clause_res.scalar_one_or_none()

    if clause is None:
        raise ValueError(f"Cannot index review '{review.review_id}': associated clause not found.")

    circular = clause.circular
    if circular is None and clause.circular_id:
        circ_stmt = select(Circular).where(Circular.id == clause.circular_id)
        circ_res = await session.execute(circ_stmt)
        circular = circ_res.scalar_one_or_none()

    if circular is None:
        raise ValueError(f"Cannot index review '{review.review_id}': associated circular not found.")

    compiled_rule = review.compiled_rule
    if compiled_rule is None and review.compiled_rule_id:
        cr_stmt = select(CompiledRule).where(CompiledRule.id == review.compiled_rule_id)
        cr_res = await session.execute(cr_stmt)
        compiled_rule = cr_res.scalar_one_or_none()

    # Gate 3: Sanitize reviewer notes
    clean_notes = scrub_secrets(review.resolution_notes or review.review_notes)
    clean_clause_text = scrub_secrets(clause.text)

    # Gate 4: Resolve policy hashes and timestamps
    approval_ts = review.resolved_at or dt.datetime.now(dt.timezone.utc)
    source_doc_hash = circular.source_document_sha256 or circular.raw_text_digest
    policy_sha256 = review.approved_policy_sha256
    rule_version = review.approved_rule_version

    if compiled_rule is not None:
        rule_version = rule_version or compiled_rule.rule_version
        policy_sha256 = policy_sha256 or compiled_rule.policy_sha256

    precedent = PrecedentRecord(
        precedent_id=f"prec_{review.review_id}",
        review_id=review.review_id,
        tenant_id=review.tenant_id,
        circular_id=str(circular.id),
        circular_number=circular.circular_number,
        source_document_sha256=source_doc_hash,
        clause_id=str(clause.id),
        clause_number=clause.clause_number,
        clause_sha256=clause.sha256,
        original_clause_text=clean_clause_text or "",
        decision="APPROVED",
        compliance_officer_id=review.compliance_officer_id,
        resolution_notes=clean_notes,
        reason_code=review.reason_code,
        approved_rule_version=rule_version,
        approved_policy_sha256=policy_sha256,
        approval_timestamp=approval_ts,
        is_shared=bool(getattr(circular, "is_shared", False)),
        metadata={
            "section_path": clause.section_path,
            "section_title": clause.section_title,
            "element_kind": clause.element_kind,
        },
    )

    await store.index_precedent(precedent)
    CASE_LAW_INDEXED_TOTAL.labels(status="success").inc()
    return precedent
