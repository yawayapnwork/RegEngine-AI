"""Cryptographic provenance and custody chain service.

Tracks, traverses, and cryptographically verifies the 11-stage conceptual lineage of regulatory decisions:
1. Original PDF (`source_document_sha256` - SHA-256 over raw uploaded binary bytes)
2. Extracted text (`extracted_text_sha256` - SHA-256 over normalized extracted text)
3. Clause (`clause.sha256` - SHA-256 over canonicalized clause text + circular + clause number)
4. Canonical facts (`canonical_facts_digest` - SHA-256 over sorted, normalized canonical facts/thresholds)
5. Compiled rule/policy version + SHA-256 (`CompiledRule.rule_version`, `CompiledRule.policy_sha256`)
6. HITL review/approval (`hitl_reviews.status = RESOLVED`, `approved_rule_version`, `approved_policy_sha256`)
7. Approving principal & timestamp (`compliance_officer_id`, `resolved_at`)
8. Transaction input digest (`transaction_input_digest` - SHA-256 over normalized transaction input)
9. OPA evaluation result (`PolicyOutcome`, decision: allow / deny / flagged, violations)
10. Evidence digest (`evidence_digest` - SHA-256 over bound evidence manifest)
11. Ledger entry / hash chain (`compliance_audit_ledger`, chained via payload_digest and current_hash)

Trust Boundary & External Authenticity Note:
--------------------------------------------
Cryptographic hashing (SHA-256) proves internal mathematical integrity, deterministic replay,
and tamper detection: if any bit of the PDF, extracted text, clause, policy, transaction facts,
or ledger is changed after the fact, the recomputed hash breaks immediately.

However, hashing alone does NOT prove external ground truth authenticity:
- It does not verify that an uploaded PDF was legally enacted by SEBI unless anchored to SEBI's
  official PKI digital signature (X.509/DSC) or secure authenticated source feed.
- It does not prove the compliance officer acted without duress unless backed by hardware MFA
  tokens (WebAuthn/FIDO2) and cryptographic non-repudiation.
- It does not prove wall-clock authenticity unless anchored to an RFC 3161 Time Stamping Authority
  or external notary ledger.
See docs/architecture/provenance-trust-boundary.md for the full trust boundary specification.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Circular, Clause, CompiledRule, HITLReview
from app.ledger.hash_chain import GENESIS_HASH, compute_block_hash, compute_payload_digest
from app.ledger.integration import compute_evidence_digest, compute_transaction_digest
from app.ledger.models import compliance_audit_ledger
from app.parsing.hashing import sha256_of_bytes, sha256_of_clause, sha256_of_extracted_text
from app.regulatory.facts import compute_canonical_facts_digest

logger = logging.getLogger(__name__)


class StageProvenance(BaseModel):
    stage_index: int
    stage_name: str
    identifier: str
    hash_value: str
    verified: bool
    details: dict[str, Any] = Field(default_factory=dict)


class ProvenanceChain(BaseModel):
    """Full end-to-end cryptographic provenance chain representing the complete
    lineage from the originating regulatory document to the audit ledger."""

    circular_number: str
    rule_id: str
    source_document_sha256: str
    extracted_text_sha256: str
    clause_sha256: str
    canonical_facts_digest: str | None = None
    compiled_rule_version: int = 1
    policy_sha256: str | None = None
    hitl_status: str
    hitl_review_id: str | None = None
    approving_principal: str | None = None
    approved_at: dt.datetime | None = None
    transaction_id: str | None = None
    transaction_input_digest: str | None = None
    evaluation_result: str | None = None
    evidence_digest: str | None = None
    ledger_sequence_num: int | None = None
    ledger_payload_digest: str | None = None
    ledger_current_hash: str | None = None
    stages: list[StageProvenance] = Field(default_factory=list)
    is_valid: bool = True
    verification_notes: list[str] = Field(default_factory=list)


class ProvenanceBreak(BaseModel):
    stage_index: int
    stage_name: str
    reason: str
    expected: str
    actual: str


class ProvenanceVerificationResult(BaseModel):
    valid: bool
    transaction_id: str
    rule_id: str
    rule_version: int
    breaks: list[ProvenanceBreak] = Field(default_factory=list)
    chain: ProvenanceChain | None = None
    verified_at: dt.datetime = Field(default_factory=lambda: dt.datetime.now(dt.timezone.utc))


async def get_rule_provenance_chain(
    session: AsyncSession,
    rule_id: str,
    raw_pdf_bytes: bytes | None = None,
    raw_extracted_text: str | None = None,
    rule_version: int | None = None,
    full_chain: bool = False,
) -> ProvenanceChain | None:
    """Traverses the provenance chain for a given compiled rule.

    If full_chain is False, returns the 8 legacy stages (for backward compatibility).
    If full_chain is True, returns all 11 stages of the conceptual chain.
    """
    stmt = select(CompiledRule).where(CompiledRule.rule_id == rule_id)
    if rule_version is not None:
        stmt = stmt.where(CompiledRule.rule_version == rule_version)
    else:
        stmt = stmt.order_by(CompiledRule.rule_version.desc())
    rule = (await session.execute(stmt)).scalars().first()
    if rule is None:
        return None

    clause = await session.get(Clause, rule.clause_id)
    if clause is None:
        return None

    circular = await session.get(Circular, clause.circular_id)
    if circular is None:
        return None

    review = (
        await session.execute(
            select(HITLReview)
            .where(
                (HITLReview.compiled_rule_id == rule.id)
                | (
                    (HITLReview.clause_id == clause.id)
                    & (
                        (HITLReview.approved_rule_version == rule.rule_version)
                        | (HITLReview.approved_rule_version.is_(None))
                    )
                )
            )
            .order_by(HITLReview.resolved_at.desc().nullslast())
        )
    ).scalars().first()

    # Query ledger for evaluation evidence row referencing this rule/clause
    ledger_row = (
        await session.execute(
            compliance_audit_ledger.select()
            .where(
                (compliance_audit_ledger.c.rule_id == rule.rule_id)
                | (compliance_audit_ledger.c.compiled_rule_ref_id == rule.id)
                | (compliance_audit_ledger.c.clause_hash == clause.sha256)
            )
            .order_by(compliance_audit_ledger.c.sequence_num.desc())
            .limit(1)
        )
    ).first()

    stages: list[StageProvenance] = []
    notes: list[str] = []
    is_valid = True

    # 1. Original PDF bytes
    src_doc_hash = circular.source_document_sha256 or "unknown"
    stage1_verified = True
    if raw_pdf_bytes is not None:
        expected_pdf_hash = sha256_of_bytes(raw_pdf_bytes)
        stage1_verified = (expected_pdf_hash == src_doc_hash)
        if not stage1_verified:
            is_valid = False
            notes.append(f"Stage 1 mismatch: raw PDF bytes hash {expected_pdf_hash} != stored {src_doc_hash}")

    stages.append(
        StageProvenance(
            stage_index=1,
            stage_name="Original PDF",
            identifier=circular.source_url or getattr(circular, "source_filename", None) or circular.circular_number,
            hash_value=src_doc_hash,
            verified=stage1_verified,
            details={
                "title": circular.title,
                "circular_number": circular.circular_number,
                "source_url": circular.source_url,
                "source_filename": getattr(circular, "source_filename", None),
            },
        )
    )

    # 2. Extracted text
    ext_text_hash = circular.raw_text_digest
    stage2_verified = True
    if raw_extracted_text is not None:
        expected_text_hash = sha256_of_extracted_text(raw_extracted_text)
        stage2_verified = (expected_text_hash == ext_text_hash)
        if not stage2_verified:
            is_valid = False
            notes.append(f"Stage 2 mismatch: extracted text hash {expected_text_hash} != stored {ext_text_hash}")

    stages.append(
        StageProvenance(
            stage_index=2,
            stage_name="Extracted Text",
            identifier=f"extracted_text:{circular.circular_number}",
            hash_value=ext_text_hash,
            verified=stage2_verified,
            details={"raw_text_digest": circular.raw_text_digest},
        )
    )

    # 3. Clause
    clause_hash = clause.sha256
    expected_clause_hash = sha256_of_clause(
        circular_number=circular.circular_number,
        clause_number=clause.clause_number,
        text=clause.text,
    )
    stage3_verified = (clause_hash == expected_clause_hash)
    if not stage3_verified:
        is_valid = False
        notes.append(f"Stage 3 mismatch: recomputed clause hash {expected_clause_hash} != stored {clause_hash}")

    stages.append(
        StageProvenance(
            stage_index=3,
            stage_name="Clause",
            identifier=f"clause:{clause.clause_number or clause.id}",
            hash_value=clause_hash,
            verified=stage3_verified,
            details={"clause_number": clause.clause_number, "section_title": clause.section_title},
        )
    )

    # Policy SHA-256 and Canonical Facts Digest
    policy_hash = rule.policy_sha256
    if not policy_hash and rule.rego_policy:
        policy_hash = hashlib.sha256(rule.rego_policy.encode("utf-8")).hexdigest()

    cf_digest = None
    if rule.provenance_metadata:
        cf_digest = rule.provenance_metadata.get("canonical_facts_digest")

    if full_chain:
        # 4. Canonical Facts
        stages.append(
            StageProvenance(
                stage_index=4,
                stage_name="Canonical Facts",
                identifier=f"canonical_facts:{rule.rule_id}",
                hash_value=cf_digest or "unspecified",
                verified=True,
                details={"canonical_facts_digest": cf_digest},
            )
        )
        # 5. Compiled Rule Version + Policy Hash
        stage5_verified = bool(rule.is_compiled and rule.rego_policy)
        stages.append(
            StageProvenance(
                stage_index=5,
                stage_name="Compiled Rule Version + Policy Hash",
                identifier=f"{rule.rule_id}:v{rule.rule_version}",
                hash_value=policy_hash or "none",
                verified=stage5_verified,
                details={"rule_version": rule.rule_version, "is_active": rule.is_active, "hitl_status": rule.hitl_status},
            )
        )
        # 6. HITL Review / Approval
        stage6_verified = bool(review and review.status == "RESOLVED")
        stages.append(
            StageProvenance(
                stage_index=6,
                stage_name="HITL Approval",
                identifier=review.review_id if review else "unassigned",
                hash_value=review.status if review else "NONE",
                verified=stage6_verified,
                details={
                    "status": review.status if review else "NONE",
                    "approved_rule_version": getattr(review, "approved_rule_version", None) if review else None,
                },
            )
        )
        # 7. Approving Principal
        stage7_verified = bool(review and review.compliance_officer_id)
        stages.append(
            StageProvenance(
                stage_index=7,
                stage_name="Approving Principal",
                identifier=review.compliance_officer_id if (review and review.compliance_officer_id) else "none",
                hash_value=review.compliance_officer_id or "none" if review else "none",
                verified=stage7_verified,
                details={
                    "officer_id": review.compliance_officer_id if review else None,
                    "resolved_at": review.resolved_at.isoformat() if (review and review.resolved_at) else None,
                },
            )
        )
        # 8. Transaction Input Digest
        tx_digest = ledger_row.details.get("transaction_input_digest") if ledger_row else None
        stages.append(
            StageProvenance(
                stage_index=8,
                stage_name="Transaction Input Digest",
                identifier=ledger_row.transaction_id if ledger_row else "none",
                hash_value=tx_digest or "none",
                verified=bool(tx_digest),
                details={"transaction_id": ledger_row.transaction_id if ledger_row else None},
            )
        )
        # 9. OPA Evaluation Result
        eval_result = ledger_row.evaluation_result if ledger_row else None
        stages.append(
            StageProvenance(
                stage_index=9,
                stage_name="OPA Evaluation Result",
                identifier=ledger_row.transaction_id if ledger_row else "none",
                hash_value=eval_result or "none",
                verified=bool(ledger_row),
                details={"verdict": eval_result, "violations": ledger_row.details.get("violations", []) if ledger_row else []},
            )
        )
        # 10. Evidence Digest
        ev_digest = ledger_row.details.get("evidence_digest") if ledger_row else None
        stages.append(
            StageProvenance(
                stage_index=10,
                stage_name="Evidence Digest",
                identifier=f"evidence:{ledger_row.transaction_id}" if ledger_row else "none",
                hash_value=ev_digest or (ledger_row.payload_digest if ledger_row else "none"),
                verified=bool(ledger_row),
                details=ledger_row.details if ledger_row else {},
            )
        )
        # 11. Ledger Entry / Hash Chain
        stages.append(
            StageProvenance(
                stage_index=11,
                stage_name="Ledger Hash Chain",
                identifier=f"seq:{ledger_row.sequence_num}" if ledger_row else "none",
                hash_value=ledger_row.current_hash if ledger_row else "none",
                verified=bool(ledger_row),
                details={
                    "sequence_num": ledger_row.sequence_num if ledger_row else None,
                    "previous_hash": ledger_row.previous_hash if ledger_row else None,
                    "payload_digest": ledger_row.payload_digest if ledger_row else None,
                },
            )
        )
    else:
        # Legacy 8-stage traversal (for backward compatibility with existing tests)
        # 4. Compiled Rule / Policy
        stage4_verified = bool(rule.is_compiled and rule.rego_policy)
        stages.append(
            StageProvenance(
                stage_index=4,
                stage_name="Compiled Rule/Policy",
                identifier=rule.rule_id,
                hash_value=clause.sha256,
                verified=stage4_verified,
                details={"rule_version": rule.rule_version, "is_active": rule.is_active, "hitl_status": rule.hitl_status},
            )
        )
        # 5. HITL Approval
        stage5_verified = bool(review and review.status == "RESOLVED")
        stages.append(
            StageProvenance(
                stage_index=5,
                stage_name="HITL Approval",
                identifier=review.review_id if review else "unassigned",
                hash_value=rule.rule_id,
                verified=stage5_verified,
                details={
                    "status": review.status if review else "NONE",
                    "officer_id": review.compliance_officer_id if review else None,
                },
            )
        )
        # 6. Evaluation
        eval_result = ledger_row.evaluation_result if ledger_row else None
        stages.append(
            StageProvenance(
                stage_index=6,
                stage_name="Evaluation",
                identifier=ledger_row.transaction_id if ledger_row else "none",
                hash_value=eval_result or "none",
                verified=bool(ledger_row),
                details={"verdict": eval_result} if ledger_row else {},
            )
        )
        # 7. Evidence
        evidence_details = ledger_row.details if ledger_row else {}
        stages.append(
            StageProvenance(
                stage_index=7,
                stage_name="Evidence",
                identifier=f"evidence:{ledger_row.transaction_id}" if ledger_row else "none",
                hash_value=ledger_row.payload_digest if ledger_row else "none",
                verified=bool(ledger_row),
                details=evidence_details,
            )
        )
        # 8. Ledger
        stages.append(
            StageProvenance(
                stage_index=8,
                stage_name="Ledger",
                identifier=f"seq:{ledger_row.sequence_num}" if ledger_row else "none",
                hash_value=ledger_row.current_hash if ledger_row else "none",
                verified=bool(ledger_row),
                details={
                    "sequence_num": ledger_row.sequence_num if ledger_row else None,
                    "previous_hash": ledger_row.previous_hash if ledger_row else None,
                },
            )
        )

    return ProvenanceChain(
        circular_number=circular.circular_number,
        rule_id=rule.rule_id,
        source_document_sha256=src_doc_hash,
        extracted_text_sha256=ext_text_hash,
        clause_sha256=clause_hash,
        canonical_facts_digest=cf_digest,
        compiled_rule_version=rule.rule_version,
        policy_sha256=policy_hash,
        hitl_status=rule.hitl_status,
        hitl_review_id=review.review_id if review else None,
        approving_principal=review.compliance_officer_id if review else None,
        approved_at=review.resolved_at if review else None,
        transaction_id=ledger_row.transaction_id if ledger_row else None,
        transaction_input_digest=ledger_row.details.get("transaction_input_digest") if ledger_row else None,
        evaluation_result=ledger_row.evaluation_result if ledger_row else None,
        evidence_digest=ledger_row.details.get("evidence_digest") if ledger_row else None,
        ledger_sequence_num=ledger_row.sequence_num if ledger_row else None,
        ledger_payload_digest=ledger_row.payload_digest if ledger_row else None,
        ledger_current_hash=ledger_row.current_hash if ledger_row else None,
        stages=stages,
        is_valid=is_valid,
        verification_notes=notes,
    )


async def get_decision_provenance_chain(
    session: AsyncSession,
    transaction_id: str,
    rule_id: str | None = None,
    sequence_num: int | None = None,
    raw_pdf_bytes: bytes | None = None,
    raw_extracted_text: str | None = None,
) -> ProvenanceChain | None:
    """Traverses the full 11-stage provenance chain for a specific historical transaction decision.

    Guarantees Multi-Version Immutability:
    Queries the specific rule_version that evaluated this transaction, so that subsequent
    re-compilations (e.g. version 2) never mutate the historical provenance of an earlier decision.
    """
    query = compliance_audit_ledger.select().where(compliance_audit_ledger.c.transaction_id == transaction_id)
    if rule_id is not None:
        query = query.where(compliance_audit_ledger.c.rule_id == rule_id)
    if sequence_num is not None:
        query = query.where(compliance_audit_ledger.c.sequence_num == sequence_num)
    query = query.order_by(compliance_audit_ledger.c.sequence_num.desc()).limit(1)

    ledger_row = (await session.execute(query)).first()
    if ledger_row is None:
        return None

    rule_id = ledger_row.rule_id
    historical_rule_version = ledger_row.details.get("rule_version", 1)

    return await get_rule_provenance_chain(
        session=session,
        rule_id=rule_id,
        raw_pdf_bytes=raw_pdf_bytes,
        raw_extracted_text=raw_extracted_text,
        rule_version=historical_rule_version,
        full_chain=True,
    )


async def verify_decision_provenance(
    session: AsyncSession,
    transaction_id: str,
    raw_pdf_bytes: bytes | None = None,
    raw_extracted_text: str | None = None,
    clause_text: str | None = None,
    sequence_num: int | None = None,
) -> ProvenanceVerificationResult:
    """Independently verifies the cryptographic integrity of all 11 provenance links
    for a specific regulatory transaction decision.
    """
    query = compliance_audit_ledger.select().where(compliance_audit_ledger.c.transaction_id == transaction_id)
    if sequence_num is not None:
        query = query.where(compliance_audit_ledger.c.sequence_num == sequence_num)
    query = query.order_by(compliance_audit_ledger.c.sequence_num.desc()).limit(1)

    ledger_row = (await session.execute(query)).first()
    if ledger_row is None:
        return ProvenanceVerificationResult(
            valid=False,
            transaction_id=transaction_id,
            rule_id="unknown",
            rule_version=0,
            breaks=[
                ProvenanceBreak(
                    stage_index=11,
                    stage_name="Ledger Hash Chain",
                    reason="No ledger entry found for transaction",
                    expected="A persisted ledger row",
                    actual="None",
                )
            ],
        )

    rule_id = ledger_row.rule_id
    historical_rule_version = ledger_row.details.get("rule_version", 1)

    # 1. Fetch exact compiled rule version
    rule = (
        await session.execute(
            select(CompiledRule)
            .where(
                (CompiledRule.rule_id == rule_id)
                & (CompiledRule.rule_version == historical_rule_version)
            )
        )
    ).scalars().first()

    breaks: list[ProvenanceBreak] = []

    if rule is None:
        breaks.append(
            ProvenanceBreak(
                stage_index=5,
                stage_name="Compiled Rule Version + Policy Hash",
                reason=f"CompiledRule {rule_id} version {historical_rule_version} not found in database",
                expected=f"CompiledRule with rule_version={historical_rule_version}",
                actual="None",
            )
        )
        return ProvenanceVerificationResult(
            valid=False,
            transaction_id=transaction_id,
            rule_id=rule_id,
            rule_version=historical_rule_version,
            breaks=breaks,
        )

    clause = await session.get(Clause, rule.clause_id)
    if clause is None:
        breaks.append(
            ProvenanceBreak(
                stage_index=3,
                stage_name="Clause",
                reason=f"Parent Clause id={rule.clause_id} not found",
                expected="Clause row",
                actual="None",
            )
        )
        return ProvenanceVerificationResult(
            valid=False,
            transaction_id=transaction_id,
            rule_id=rule_id,
            rule_version=historical_rule_version,
            breaks=breaks,
        )

    circular = await session.get(Circular, clause.circular_id)
    if circular is None:
        breaks.append(
            ProvenanceBreak(
                stage_index=1,
                stage_name="Original PDF",
                reason=f"Parent Circular id={clause.circular_id} not found",
                expected="Circular row",
                actual="None",
            )
        )
        return ProvenanceVerificationResult(
            valid=False,
            transaction_id=transaction_id,
            rule_id=rule_id,
            rule_version=historical_rule_version,
            breaks=breaks,
        )

    # --- Verify Stage 1: Original PDF ---
    if raw_pdf_bytes is not None:
        computed_pdf_hash = sha256_of_bytes(raw_pdf_bytes)
        if computed_pdf_hash != circular.source_document_sha256:
            breaks.append(
                ProvenanceBreak(
                    stage_index=1,
                    stage_name="Original PDF",
                    reason="Recomputed PDF bytes SHA-256 does not match stored circular digest",
                    expected=circular.source_document_sha256 or "",
                    actual=computed_pdf_hash,
                )
            )

    # --- Verify Stage 2: Extracted Text ---
    if raw_extracted_text is not None:
        computed_text_hash = sha256_of_extracted_text(raw_extracted_text)
        if computed_text_hash != circular.raw_text_digest:
            breaks.append(
                ProvenanceBreak(
                    stage_index=2,
                    stage_name="Extracted Text",
                    reason="Recomputed extracted text SHA-256 does not match stored raw_text_digest",
                    expected=circular.raw_text_digest,
                    actual=computed_text_hash,
                )
            )

    # --- Verify Stage 3: Clause ---
    expected_clause_text = clause_text if clause_text is not None else clause.text
    computed_clause_hash = sha256_of_clause(
        circular_number=circular.circular_number,
        clause_number=clause.clause_number,
        text=expected_clause_text,
    )
    if computed_clause_hash != clause.sha256:
        breaks.append(
            ProvenanceBreak(
                stage_index=3,
                stage_name="Clause",
                reason="Recomputed clause SHA-256 does not match stored clause digest",
                expected=clause.sha256,
                actual=computed_clause_hash,
            )
        )
    if ledger_row.clause_hash != clause.sha256:
        breaks.append(
            ProvenanceBreak(
                stage_index=3,
                stage_name="Clause",
                reason="Ledger clause_hash does not match stored clause SHA-256",
                expected=clause.sha256,
                actual=ledger_row.clause_hash,
            )
        )

    # --- Verify Stage 4: Canonical Facts ---
    stored_cf_digest = ledger_row.details.get("canonical_facts_digest")
    if stored_cf_digest and rule.provenance_metadata:
        rule_cf_digest = rule.provenance_metadata.get("canonical_facts_digest")
        if rule_cf_digest and stored_cf_digest != rule_cf_digest:
            breaks.append(
                ProvenanceBreak(
                    stage_index=4,
                    stage_name="Canonical Facts",
                    reason="Ledger canonical_facts_digest does not match compiled rule canonical facts digest",
                    expected=rule_cf_digest,
                    actual=stored_cf_digest,
                )
            )

    # --- Verify Stage 5: Compiled Rule Version & Policy SHA-256 ---
    if rule.rego_policy:
        computed_policy_hash = hashlib.sha256(rule.rego_policy.encode("utf-8")).hexdigest()
        stored_policy_hash = ledger_row.details.get("policy_sha256")
        if stored_policy_hash and computed_policy_hash != stored_policy_hash:
            breaks.append(
                ProvenanceBreak(
                    stage_index=5,
                    stage_name="Compiled Rule Version + Policy Hash",
                    reason="Recomputed policy SHA-256 does not match policy_sha256 in ledger details",
                    expected=stored_policy_hash,
                    actual=computed_policy_hash,
                )
            )

    # --- Verify Stage 6 & 7: HITL Review & Approving Principal ---
    review = None
    if getattr(ledger_row, "hitl_review_ref_id", None):
        review = await session.get(HITLReview, ledger_row.hitl_review_ref_id)
    elif getattr(ledger_row, "hitl_review_id", None):
        review = (
            await session.execute(
                select(HITLReview).where(HITLReview.review_id == ledger_row.hitl_review_id)
            )
        ).scalars().first()

    if review is None:
        review = (
            await session.execute(
                select(HITLReview)
                .where(
                    (HITLReview.compiled_rule_id == rule.id)
                    | (
                        (HITLReview.clause_id == clause.id)
                        & (
                            (HITLReview.approved_rule_version == historical_rule_version)
                            | (HITLReview.approved_rule_version.is_(None))
                        )
                    )
                )
                .order_by(HITLReview.resolved_at.desc().nullslast())
            )
        ).scalars().first()

    if review is not None:
        if review.status != "RESOLVED":
            breaks.append(
                ProvenanceBreak(
                    stage_index=6,
                    stage_name="HITL Approval",
                    reason=f"HITL review {review.review_id} status is {review.status}, not RESOLVED",
                    expected="RESOLVED",
                    actual=review.status,
                )
            )
        if review.approved_rule_version is not None and review.approved_rule_version != historical_rule_version:
            breaks.append(
                ProvenanceBreak(
                    stage_index=6,
                    stage_name="HITL Approval",
                    reason="HITL review approved a different rule version than evaluated",
                    expected=str(historical_rule_version),
                    actual=str(review.approved_rule_version),
                )
            )
        if not review.compliance_officer_id:
            breaks.append(
                ProvenanceBreak(
                    stage_index=7,
                    stage_name="Approving Principal",
                    reason="HITL review has no approving compliance officer recorded",
                    expected="A non-null compliance_officer_id",
                    actual="None",
                )
            )

    # --- Verify Stage 8: Transaction Input Digest ---
    stored_tx_digest = ledger_row.details.get("transaction_input_digest")
    if stored_tx_digest and "facts" in ledger_row.details:
        tx_canonical = {
            "broker_id": ledger_row.broker_id,
            "entity_type": ledger_row.details.get("entity_type", ""),
            "facts": ledger_row.details.get("facts", {}),
            "transaction_id": ledger_row.transaction_id,
        }
        computed_tx_digest = hashlib.sha256(
            json.dumps(tx_canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
        ).hexdigest()
        if computed_tx_digest != stored_tx_digest:
            breaks.append(
                ProvenanceBreak(
                    stage_index=8,
                    stage_name="Transaction Input Digest",
                    reason="Recomputed transaction input digest does not match stored transaction_input_digest",
                    expected=stored_tx_digest,
                    actual=computed_tx_digest,
                )
            )

    # --- Verify Stage 10: Evidence Digest ---
    stored_ev_digest = ledger_row.details.get("evidence_digest")
    if stored_ev_digest:
        evidence_payload = {
            "transaction_id": ledger_row.transaction_id,
            "transaction_input_digest": stored_tx_digest,
            "rule_id": ledger_row.rule_id,
            "rule_version": historical_rule_version,
            "policy_sha256": ledger_row.details.get("policy_sha256"),
            "package": ledger_row.details.get("package", ""),
            "circular_id": ledger_row.circular_id,
            "section_reference": ledger_row.section_reference,
            "clause_sha256": ledger_row.clause_hash,
            "source_document_sha256": ledger_row.details.get("source_document_sha256"),
            "extracted_text_sha256": ledger_row.details.get("extracted_text_sha256"),
            "canonical_facts_digest": stored_cf_digest,
            "evaluation_result": ledger_row.evaluation_result,
            "violations": ledger_row.details.get("violations", []),
            "decision": ledger_row.details.get("transaction_decision", ""),
            "hitl_case_id": ledger_row.hitl_review_id,
        }
        computed_ev_digest = compute_evidence_digest(evidence_payload)
        if computed_ev_digest != stored_ev_digest:
            breaks.append(
                ProvenanceBreak(
                    stage_index=10,
                    stage_name="Evidence Digest",
                    reason="Recomputed evidence digest does not match evidence_digest in ledger details",
                    expected=stored_ev_digest,
                    actual=computed_ev_digest,
                )
            )

    # --- Verify Stage 11: Ledger Payload Digest & Hash Chain Block ---
    recomputed_payload_digest = compute_payload_digest(dict(ledger_row._mapping))
    if recomputed_payload_digest != ledger_row.payload_digest:
        breaks.append(
            ProvenanceBreak(
                stage_index=11,
                stage_name="Ledger Hash Chain",
                reason="Recomputed ledger payload_digest does not match stored payload_digest",
                expected=ledger_row.payload_digest,
                actual=recomputed_payload_digest,
            )
        )

    recomputed_block_hash = compute_block_hash(
        previous_hash=ledger_row.previous_hash,
        payload_digest=ledger_row.payload_digest,
        sequence_num=ledger_row.sequence_num,
        evaluated_at=ledger_row.evaluated_at,
    )
    if recomputed_block_hash != ledger_row.current_hash:
        breaks.append(
            ProvenanceBreak(
                stage_index=11,
                stage_name="Ledger Hash Chain",
                reason="Recomputed block current_hash does not match stored current_hash",
                expected=ledger_row.current_hash,
                actual=recomputed_block_hash,
            )
        )

    chain = await get_decision_provenance_chain(
        session=session,
        transaction_id=transaction_id,
        rule_id=rule_id,
        sequence_num=ledger_row.sequence_num,
        raw_pdf_bytes=raw_pdf_bytes,
        raw_extracted_text=raw_extracted_text,
    )

    return ProvenanceVerificationResult(
        valid=len(breaks) == 0,
        transaction_id=transaction_id,
        rule_id=rule_id,
        rule_version=historical_rule_version,
        breaks=breaks,
        chain=chain,
    )
