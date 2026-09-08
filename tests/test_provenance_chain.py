"""Tests for RegEngine AI's strengthened 11-stage cryptographic provenance model.

Validates:
1. End-to-end traversal and independent verification of the complete 11-link chain:
   Original PDF SHA-256
   -> Extracted text SHA-256
   -> Clause SHA-256
   -> Canonical facts digest
   -> Compiled rule version + policy SHA-256
   -> HITL review / approval
   -> Approving principal & timestamp
   -> Transaction input digest
   -> OPA evaluation result
   -> Evidence digest
   -> Ledger entry / hash chain block
2. Multi-version immutability: A newer policy version (e.g. v2) does NOT mutate or
   invalidate the historical provenance of an earlier decision evaluated under v1.
3. Tamper detection: Proves that verification fails and pinpoints the exact broken link
   when any upstream or downstream artifact is modified.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db.models import Circular, Clause, CompiledRule, HITLReview, Tenant
from app.execution.models import Decision, EvaluationResult, PolicyOutcome, TransactionPayload
from app.ledger.hash_chain import GENESIS_HASH, compute_block_hash, compute_payload_digest
from app.ledger.integration import build_ledger_events, compute_evidence_digest, compute_transaction_digest
from app.ledger.models import EvaluationOutcome, compliance_audit_ledger
from app.parsing.hashing import sha256_of_bytes, sha256_of_clause, sha256_of_extracted_text
from app.regulatory.facts import compute_canonical_facts_digest
from app.services.provenance import (
    get_decision_provenance_chain,
    get_rule_provenance_chain,
    verify_decision_provenance,
)


def _make_dummy_pdf() -> bytes:
    return b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n%%EOF\n"


@pytest.fixture
def test_setup():
    pdf_bytes = _make_dummy_pdf()
    pdf_sha = sha256_of_bytes(pdf_bytes)

    extracted_text = "Clause 2.1: Every broker shall collect upfront margin of not less than 20%."
    ext_text_sha = sha256_of_extracted_text(extracted_text)

    clause_text = "Every broker shall collect upfront margin of not less than 20%."
    circ_num = "SEBI/HO/PROV/2026/045"
    clause_num = "2.1"
    clause_sha = sha256_of_clause(circular_number=circ_num, clause_number=clause_num, text=clause_text)

    thresholds = [
        {
            "metric": "Upfront Margin",
            "canonical_fact": "upfront_margin_pct",
            "operator": ">=",
            "value": 20.0,
            "unit": "%",
        }
    ]
    cf_digest = compute_canonical_facts_digest(thresholds)

    rego_code_v1 = (
        "package sebi.broking.circulars.cir_2026_045.clause_2_1\n"
        "import rego.v1\n"
        "default allow := false\n"
        "allow if { input.facts.upfront_margin_pct >= 20 }\n"
        'decision := {"allow": allow, "rule_id": "test_rule", "rule_version": 1}\n'
    )
    policy_sha_v1 = hashlib.sha256(rego_code_v1.encode("utf-8")).hexdigest()

    return {
        "pdf_bytes": pdf_bytes,
        "pdf_sha": pdf_sha,
        "extracted_text": extracted_text,
        "ext_text_sha": ext_text_sha,
        "clause_text": clause_text,
        "circ_num": circ_num,
        "clause_num": clause_num,
        "clause_sha": clause_sha,
        "thresholds": thresholds,
        "cf_digest": cf_digest,
        "rego_code_v1": rego_code_v1,
        "policy_sha_v1": policy_sha_v1,
    }


@pytest.mark.asyncio
async def test_full_11_stage_provenance_verification_success(test_setup: dict) -> None:
    """Requirement 1, 4, 5, 7, 8, 9: Complete 11-stage lineage builds and verifies cleanly."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(compliance_audit_ledger.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async with session_factory() as session:
        # 1. Tenant
        tenant = Tenant(
            tenant_id="sebi_baseline",
            display_name="SEBI Baseline",
            tenant_type="stockbroker",
            opa_bundle_prefix="baseline",
            is_active=True,
        )
        session.add(tenant)
        await session.flush()

        # 2. Circular (Stages 1 & 2)
        circular = Circular(
            tenant_id="sebi_baseline",
            circular_number=test_setup["circ_num"],
            title="Upfront Margin Master Circular",
            source_document_sha256=test_setup["pdf_sha"],
            raw_text_digest=test_setup["ext_text_sha"],
        )
        session.add(circular)
        await session.flush()

        # 3. Clause (Stage 3)
        clause = Clause(
            circular_id=circular.id,
            tenant_id="sebi_baseline",
            clause_number=test_setup["clause_num"],
            section_title="Upfront Margin",
            text=test_setup["clause_text"],
            sha256=test_setup["clause_sha"],
        )
        session.add(clause)
        await session.flush()

        # 4. Compiled Rule v1 (Stages 4 & 5)
        rule_id = f"{test_setup['clause_sha']}:{test_setup['clause_num']}"
        compiled_rule = CompiledRule(
            clause_id=clause.id,
            tenant_id="sebi_baseline",
            rule_id=rule_id,
            rule_version=1,
            rego_policy=test_setup["rego_code_v1"],
            policy_sha256=test_setup["policy_sha_v1"],
            provenance_metadata={"canonical_facts_digest": test_setup["cf_digest"]},
            is_compiled=True,
            is_active=True,
            hitl_status="RESOLVED",
        )
        session.add(compiled_rule)
        await session.flush()

        # 5. HITL Review (Stages 6 & 7)
        now = dt.datetime.now(dt.timezone.utc)
        review = HITLReview(
            clause_id=clause.id,
            compiled_rule_id=compiled_rule.id,
            tenant_id="sebi_baseline",
            review_id="REV-2026-001",
            reason_code="low_extraction_confidence",
            severity="blocking",
            description="Upfront margin review",
            status="RESOLVED",
            compliance_officer_id="compliance_officer_alice",
            resolution_notes="Verified against circular gazette.",
            approved_rule_version=1,
            approved_policy_sha256=test_setup["policy_sha_v1"],
            flagged_at=now,
            resolved_at=now,
        )
        session.add(review)
        await session.flush()

        # 6. Transaction evaluation & Ledger write (Stages 8, 9, 10, 11)
        tx = TransactionPayload(
            transaction_id="TXN-2026-PASS-01",
            broker_id="broker_alpha",
            entity_type="Stockbroker",
            facts={"upfront_margin_pct": 22.5},
        )
        eval_result = EvaluationResult(
            transaction_id=tx.transaction_id,
            decision=Decision.ALLOW,
            evaluated_at=now,
            matched_policies=[
                PolicyOutcome(
                    rule_id=rule_id,
                    package="sebi.broking.circulars.cir_2026_045.clause_2_1",
                    allow=True,
                    violations=[],
                    circular_number=test_setup["circ_num"],
                    clause_number=test_setup["clause_num"],
                    rule_version=1,
                    policy_sha256=test_setup["policy_sha_v1"],
                    canonical_facts_digest=test_setup["cf_digest"],
                    source_document_sha256=test_setup["pdf_sha"],
                    extracted_text_sha256=test_setup["ext_text_sha"],
                    clause_sha256=test_setup["clause_sha"],
                )
            ],
        )

        events = build_ledger_events(tx, eval_result)
        assert len(events) == 1
        event = events[0]

        event_dict = event.model_dump(mode="json")
        event_dict["evaluated_at"] = event.evaluated_at
        payload_digest = compute_payload_digest(event_dict)
        current_hash = compute_block_hash(
            previous_hash=GENESIS_HASH,
            payload_digest=payload_digest,
            sequence_num=0,
            evaluated_at=now,
        )

        await session.execute(
            compliance_audit_ledger.insert().values(
                sequence_num=0,
                broker_id=event.broker_id,
                transaction_id=event.transaction_id,
                evaluated_at=now,
                circular_id=event.circular_id,
                clause_hash=event.clause_hash,
                section_reference=event.section_reference,
                rule_id=event.rule_id,
                evaluation_result=event.evaluation_result.value,
                hitl_review_id=None,
                details=event.details,
                payload_digest=payload_digest,
                previous_hash=GENESIS_HASH,
                current_hash=current_hash,
                created_at=now,
                circular_ref_id=circular.id,
                clause_ref_id=clause.id,
                compiled_rule_ref_id=compiled_rule.id,
                hitl_review_ref_id=review.id,
            )
        )
        await session.commit()

        # Verify full 11-stage decision provenance
        verification = await verify_decision_provenance(
            session=session,
            transaction_id="TXN-2026-PASS-01",
            raw_pdf_bytes=test_setup["pdf_bytes"],
            raw_extracted_text=test_setup["extracted_text"],
            clause_text=test_setup["clause_text"],
        )

        assert verification.valid is True
        assert len(verification.breaks) == 0
        assert verification.rule_version == 1

        chain = verification.chain
        assert chain is not None
        assert chain.is_valid is True
        assert len(chain.stages) == 11

        # Check all 11 stages in exact order
        assert chain.stages[0].stage_name == "Original PDF"
        assert chain.stages[0].hash_value == test_setup["pdf_sha"]

        assert chain.stages[1].stage_name == "Extracted Text"
        assert chain.stages[1].hash_value == test_setup["ext_text_sha"]

        assert chain.stages[2].stage_name == "Clause"
        assert chain.stages[2].hash_value == test_setup["clause_sha"]

        assert chain.stages[3].stage_name == "Canonical Facts"
        assert chain.stages[3].hash_value == test_setup["cf_digest"]

        assert chain.stages[4].stage_name == "Compiled Rule Version + Policy Hash"
        assert chain.stages[4].hash_value == test_setup["policy_sha_v1"]

        assert chain.stages[5].stage_name == "HITL Approval"
        assert chain.stages[5].details["status"] == "RESOLVED"

        assert chain.stages[6].stage_name == "Approving Principal"
        assert chain.stages[6].identifier == "compliance_officer_alice"

        assert chain.stages[7].stage_name == "Transaction Input Digest"
        assert chain.stages[7].hash_value != "none"

        assert chain.stages[8].stage_name == "OPA Evaluation Result"
        assert chain.stages[8].details["verdict"] == "PASS"

        assert chain.stages[9].stage_name == "Evidence Digest"
        assert chain.stages[9].hash_value != "none"

        assert chain.stages[10].stage_name == "Ledger Hash Chain"
        assert chain.stages[10].hash_value == current_hash

    await engine.dispose()


@pytest.mark.asyncio
async def test_multi_version_immutability(test_setup: dict) -> None:
    """Requirement 6: A later policy version (v2) must not mutate the provenance of an earlier decision (v1)."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(compliance_audit_ledger.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async with session_factory() as session:
        tenant = Tenant(
            tenant_id="sebi_baseline", display_name="SEBI Baseline", tenant_type="stockbroker",
            opa_bundle_prefix="baseline", is_active=True,
        )
        session.add(tenant)
        circular = Circular(
            tenant_id="sebi_baseline", circular_number=test_setup["circ_num"],
            source_document_sha256=test_setup["pdf_sha"], raw_text_digest=test_setup["ext_text_sha"],
        )
        session.add(circular)
        await session.flush()

        clause = Clause(
            circular_id=circular.id, tenant_id="sebi_baseline",
            clause_number=test_setup["clause_num"], text=test_setup["clause_text"],
            sha256=test_setup["clause_sha"],
        )
        session.add(clause)
        await session.flush()

        rule_id = f"{test_setup['clause_sha']}:{test_setup['clause_num']}"

        # Version 1 Rule
        rule_v1 = CompiledRule(
            clause_id=clause.id, tenant_id="sebi_baseline", rule_id=rule_id,
            rule_version=1, rego_policy=test_setup["rego_code_v1"],
            policy_sha256=test_setup["policy_sha_v1"],
            provenance_metadata={"canonical_facts_digest": test_setup["cf_digest"]},
            is_compiled=True, is_active=True, hitl_status="RESOLVED",
        )
        session.add(rule_v1)
        await session.flush()

        now1 = dt.datetime(2026, 1, 15, 10, 0, 0, tzinfo=dt.timezone.utc)
        review_v1 = HITLReview(
            clause_id=clause.id, compiled_rule_id=rule_v1.id, tenant_id="sebi_baseline",
            review_id="REV-V1-001", reason_code="low_extraction_confidence", severity="blocking",
            description="V1 approval", status="RESOLVED", compliance_officer_id="officer_bob",
            approved_rule_version=1, approved_policy_sha256=test_setup["policy_sha_v1"],
            flagged_at=now1, resolved_at=now1,
        )
        session.add(review_v1)
        await session.flush()

        # Decision on Transaction 1 under Version 1
        tx1 = TransactionPayload(
            transaction_id="TXN-HISTORICAL-01",
            broker_id="broker_1",
            entity_type="Stockbroker",
            facts={"upfront_margin_pct": 21.0},
        )
        eval1 = EvaluationResult(
            transaction_id=tx1.transaction_id,
            decision=Decision.ALLOW,
            evaluated_at=now1,
            matched_policies=[
                PolicyOutcome(
                    rule_id=rule_id,
                    package="sebi.broking.test",
                    allow=True,
                    circular_number=test_setup["circ_num"],
                    clause_number=test_setup["clause_num"],
                    rule_version=1,
                    policy_sha256=test_setup["policy_sha_v1"],
                    canonical_facts_digest=test_setup["cf_digest"],
                    clause_sha256=test_setup["clause_sha"],
                    source_document_sha256=test_setup["pdf_sha"],
                    extracted_text_sha256=test_setup["ext_text_sha"],
                )
            ],
        )
        events1 = build_ledger_events(tx1, eval1)
        event1_dict = events1[0].model_dump(mode="json")
        event1_dict["evaluated_at"] = now1
        p_digest_1 = compute_payload_digest(event1_dict)
        block_hash_1 = compute_block_hash(
            previous_hash=GENESIS_HASH, payload_digest=p_digest_1, sequence_num=0, evaluated_at=now1
        )
        await session.execute(
            compliance_audit_ledger.insert().values(
                sequence_num=0, broker_id="broker_1", transaction_id=tx1.transaction_id,
                evaluated_at=now1, circular_id=test_setup["circ_num"], clause_hash=test_setup["clause_sha"],
                section_reference=test_setup["clause_num"], rule_id=rule_id, evaluation_result="PASS",
                hitl_review_id=None, details=events1[0].details, payload_digest=p_digest_1,
                previous_hash=GENESIS_HASH, current_hash=block_hash_1, created_at=now1,
                circular_ref_id=circular.id, clause_ref_id=clause.id, compiled_rule_ref_id=rule_v1.id,
                hitl_review_ref_id=review_v1.id,
            )
        )
        await session.commit()

        # Now: Author and activate Version 2 of the policy (deactivates v1)
        rule_v1.is_active = False
        rego_code_v2 = (
            "package sebi.broking.test\nimport rego.v1\ndefault allow := false\n"
            "allow if { input.facts.upfront_margin_pct >= 25 }\n"  # Changed from 20% to 25%
        )
        policy_sha_v2 = hashlib.sha256(rego_code_v2.encode("utf-8")).hexdigest()
        rule_v2 = CompiledRule(
            clause_id=clause.id, tenant_id="sebi_baseline", rule_id=rule_id,
            rule_version=2, rego_policy=rego_code_v2, policy_sha256=policy_sha_v2,
            provenance_metadata={"canonical_facts_digest": test_setup["cf_digest"]},
            is_compiled=True, is_active=True, hitl_status="RESOLVED",
        )
        session.add(rule_v2)
        await session.flush()
        now2 = dt.datetime(2026, 2, 1, 10, 0, 0, tzinfo=dt.timezone.utc)
        review_v2 = HITLReview(
            clause_id=clause.id, compiled_rule_id=rule_v2.id, tenant_id="sebi_baseline",
            review_id="REV-V2-002", reason_code="low_extraction_confidence", severity="blocking",
            description="V2 approval", status="RESOLVED", compliance_officer_id="officer_carol",
            approved_rule_version=2, approved_policy_sha256=policy_sha_v2,
            flagged_at=now2, resolved_at=now2,
        )
        session.add(review_v2)
        await session.commit()

        # Decision on Transaction 2 under Version 2
        tx2 = TransactionPayload(
            transaction_id="TXN-NEW-02", broker_id="broker_1", entity_type="Stockbroker",
            facts={"upfront_margin_pct": 23.0},  # Passes v1 (>=20) but FAILS v2 (>=25)
        )
        eval2 = EvaluationResult(
            transaction_id=tx2.transaction_id, decision=Decision.DENY, evaluated_at=now2,
            matched_policies=[
                PolicyOutcome(
                    rule_id=rule_id, package="sebi.broking.test", allow=False,
                    violations=["Margin 23% < 25% required by v2"],
                    circular_number=test_setup["circ_num"], clause_number=test_setup["clause_num"],
                    rule_version=2, policy_sha256=policy_sha_v2,
                    canonical_facts_digest=test_setup["cf_digest"], clause_sha256=test_setup["clause_sha"],
                )
            ],
        )
        events2 = build_ledger_events(tx2, eval2)
        event2_dict = events2[0].model_dump(mode="json")
        event2_dict["evaluated_at"] = now2
        p_digest_2 = compute_payload_digest(event2_dict)
        block_hash_2 = compute_block_hash(
            previous_hash=block_hash_1, payload_digest=p_digest_2, sequence_num=1, evaluated_at=now2
        )
        await session.execute(
            compliance_audit_ledger.insert().values(
                sequence_num=1, broker_id="broker_1", transaction_id=tx2.transaction_id,
                evaluated_at=now2, circular_id=test_setup["circ_num"], clause_hash=test_setup["clause_sha"],
                section_reference=test_setup["clause_num"], rule_id=rule_id, evaluation_result="FAIL",
                hitl_review_id=None, details=events2[0].details, payload_digest=p_digest_2,
                previous_hash=block_hash_1, current_hash=block_hash_2, created_at=now2,
                circular_ref_id=circular.id, clause_ref_id=clause.id, compiled_rule_ref_id=rule_v2.id,
                hitl_review_ref_id=review_v2.id,
            )
        )
        await session.commit()

        # Crucial Test: Verify TXN-1 (evaluated under v1) remains 100% valid and anchored to v1!
        verification_v1 = await verify_decision_provenance(session, transaction_id="TXN-HISTORICAL-01")
        assert verification_v1.valid is True
        assert len(verification_v1.breaks) == 0
        assert verification_v1.rule_version == 1
        assert verification_v1.chain.policy_sha256 == test_setup["policy_sha_v1"]
        assert verification_v1.chain.approving_principal == "officer_bob"

        # Verify TXN-2 is anchored to v2
        verification_v2 = await verify_decision_provenance(session, transaction_id="TXN-NEW-02")
        assert verification_v2.valid is True
        assert len(verification_v2.breaks) == 0
        assert verification_v2.rule_version == 2
        assert verification_v2.chain.policy_sha256 == policy_sha_v2
        assert verification_v2.chain.approving_principal == "officer_carol"

    await engine.dispose()


@pytest.mark.asyncio
async def test_tamper_detection_breaks_verification(test_setup: dict) -> None:
    """Requirement 10: Prove that tampering with any upstream or downstream artifact breaks verification."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(compliance_audit_ledger.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async with session_factory() as session:
        tenant = Tenant(
            tenant_id="sebi_baseline", display_name="SEBI Baseline", tenant_type="stockbroker",
            opa_bundle_prefix="baseline", is_active=True,
        )
        session.add(tenant)
        circular = Circular(
            tenant_id="sebi_baseline", circular_number=test_setup["circ_num"],
            source_document_sha256=test_setup["pdf_sha"], raw_text_digest=test_setup["ext_text_sha"],
        )
        session.add(circular)
        await session.flush()

        clause = Clause(
            circular_id=circular.id, tenant_id="sebi_baseline",
            clause_number=test_setup["clause_num"], text=test_setup["clause_text"],
            sha256=test_setup["clause_sha"],
        )
        session.add(clause)
        await session.flush()

        rule_id = f"{test_setup['clause_sha']}:{test_setup['clause_num']}"
        rule = CompiledRule(
            clause_id=clause.id, tenant_id="sebi_baseline", rule_id=rule_id,
            rule_version=1, rego_policy=test_setup["rego_code_v1"],
            policy_sha256=test_setup["policy_sha_v1"],
            provenance_metadata={"canonical_facts_digest": test_setup["cf_digest"]},
            is_compiled=True, is_active=True, hitl_status="RESOLVED",
        )
        session.add(rule)

        now = dt.datetime.now(dt.timezone.utc)
        review = HITLReview(
            clause_id=clause.id, compiled_rule_id=rule.id, tenant_id="sebi_baseline",
            review_id="REV-TAMPER-01", reason_code="low_extraction_confidence", severity="blocking",
            description="Review", status="RESOLVED", compliance_officer_id="officer_alice",
            approved_rule_version=1, approved_policy_sha256=test_setup["policy_sha_v1"],
            flagged_at=now, resolved_at=now,
        )
        session.add(review)
        await session.flush()

        tx = TransactionPayload(
            transaction_id="TXN-TAMPER-TEST", broker_id="broker_1", entity_type="Stockbroker",
            facts={"upfront_margin_pct": 22.0},
        )
        eval_res = EvaluationResult(
            transaction_id=tx.transaction_id, decision=Decision.ALLOW, evaluated_at=now,
            matched_policies=[
                PolicyOutcome(
                    rule_id=rule_id, package="sebi.broking.test", allow=True,
                    circular_number=test_setup["circ_num"], clause_number=test_setup["clause_num"],
                    rule_version=1, policy_sha256=test_setup["policy_sha_v1"],
                    canonical_facts_digest=test_setup["cf_digest"], clause_sha256=test_setup["clause_sha"],
                    source_document_sha256=test_setup["pdf_sha"], extracted_text_sha256=test_setup["ext_text_sha"],
                )
            ],
        )
        events = build_ledger_events(tx, eval_res)
        event_dict = events[0].model_dump(mode="json")
        event_dict["evaluated_at"] = now
        p_digest = compute_payload_digest(event_dict)
        current_hash = compute_block_hash(
            previous_hash=GENESIS_HASH, payload_digest=p_digest, sequence_num=0, evaluated_at=now
        )
        await session.execute(
            compliance_audit_ledger.insert().values(
                sequence_num=0, broker_id="broker_1", transaction_id=tx.transaction_id,
                evaluated_at=now, circular_id=test_setup["circ_num"], clause_hash=test_setup["clause_sha"],
                section_reference=test_setup["clause_num"], rule_id=rule_id, evaluation_result="PASS",
                hitl_review_id=None, details=events[0].details, payload_digest=p_digest,
                previous_hash=GENESIS_HASH, current_hash=current_hash, created_at=now,
                circular_ref_id=circular.id, clause_ref_id=clause.id, compiled_rule_ref_id=rule.id,
                hitl_review_ref_id=review.id,
            )
        )
        await session.commit()

        # 1. Tamper Test: Modify original PDF bytes
        tampered_pdf = test_setup["pdf_bytes"] + b"MALICIOUS_BYTES"
        res1 = await verify_decision_provenance(session, "TXN-TAMPER-TEST", raw_pdf_bytes=tampered_pdf)
        assert res1.valid is False
        assert any(b.stage_name == "Original PDF" for b in res1.breaks)

        # 2. Tamper Test: Modify extracted text
        tampered_text = test_setup["extracted_text"] + " TAMPERED"
        res2 = await verify_decision_provenance(session, "TXN-TAMPER-TEST", raw_extracted_text=tampered_text)
        assert res2.valid is False
        assert any(b.stage_name == "Extracted Text" for b in res2.breaks)

        # 3. Tamper Test: Modify clause text
        tampered_clause = test_setup["clause_text"] + " MODIFIED"
        res3 = await verify_decision_provenance(session, "TXN-TAMPER-TEST", clause_text=tampered_clause)
        assert res3.valid is False
        assert any(b.stage_name == "Clause" for b in res3.breaks)

        # 4. Tamper Test: Corrupt compiled Rego policy in DB
        rule.rego_policy = "package corrupted\ndefault allow := true"
        await session.commit()
        res4 = await verify_decision_provenance(session, "TXN-TAMPER-TEST")
        assert res4.valid is False
        assert any(b.stage_name == "Compiled Rule Version + Policy Hash" for b in res4.breaks)

        # Restore Rego policy
        rule.rego_policy = test_setup["rego_code_v1"]
        await session.commit()

        # 5. Tamper Test: Corrupt HITL review approval in DB
        review.status = "REJECTED"
        await session.commit()
        res5 = await verify_decision_provenance(session, "TXN-TAMPER-TEST")
        assert res5.valid is False
        assert any(b.stage_name == "HITL Approval" for b in res5.breaks)

        # Restore review
        review.status = "RESOLVED"
        await session.commit()

        # 6. Tamper Test: Alter ledger row details in DB
        tampered_details = dict(events[0].details)
        tampered_details["facts"] = {"upfront_margin_pct": 99.9}
        await session.execute(
            compliance_audit_ledger.update()
            .where(compliance_audit_ledger.c.transaction_id == "TXN-TAMPER-TEST")
            .values(details=tampered_details)
        )
        await session.commit()
        res6 = await verify_decision_provenance(session, "TXN-TAMPER-TEST")
        assert res6.valid is False
        # Breaks at Transaction Input Digest, Evidence Digest, and Ledger Hash Chain!
        assert any(b.stage_name in ("Transaction Input Digest", "Evidence Digest", "Ledger Hash Chain") for b in res6.breaks)

    await engine.dispose()
