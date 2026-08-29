"""Orchestrates Requirements 2 and 3: verify a submitted Groth16 proof
server-side, and only on success write a ledger entry that records the
fact and hash of the proof -- never the concealed facts a normal
transaction evaluation would carry.

Contrast with app.ledger.integration.build_ledger_events: an ordinary
compliance evaluation's `details` includes `entity_type`/`facts` (the
full input snapshot, kept so app.backtest can replay it later). A
zk-verified evaluation deliberately omits both -- `facts` is exactly
what `collected_margin`/`client_account_id` would leak, and the entire
point of this module is that RegEngine never sees them at all, so there
is nothing to snapshot.
"""
from __future__ import annotations

import logging

from app.ledger.models import ComplianceEvaluationEvent, EvaluationOutcome
from app.ledger.service import LedgerService
from app.zkp.groth16_verifier import ProofVerificationError, verify_groth16_proof
from app.zkp.models import MarginComplianceProofSubmission, ZKProofVerificationResult
from app.zkp.proof_hashing import compute_proof_hash
from app.zkp.verification_key_registry import UnknownCircuitError, get_verification_key

logger = logging.getLogger(__name__)


async def verify_and_log_proof(
    ledger: LedgerService,
    settings,
    submission: MarginComplianceProofSubmission,
) -> ZKProofVerificationResult:
    proof_hash = compute_proof_hash(submission.circuit_id, submission.proof, submission.public_signals)

    try:
        vk = get_verification_key(settings, submission.circuit_id)
    except UnknownCircuitError:
        return ZKProofVerificationResult(
            verified=False,
            circuit_id=submission.circuit_id,
            proof_hash=proof_hash,
            reason=f"Unknown or unconfigured circuit_id '{submission.circuit_id}'.",
        )

    try:
        verified = verify_groth16_proof(vk, submission.proof, submission.public_signals)
    except ProofVerificationError as exc:
        return ZKProofVerificationResult(verified=False, circuit_id=submission.circuit_id, proof_hash=proof_hash, reason=str(exc))

    if not verified:
        return ZKProofVerificationResult(
            verified=False,
            circuit_id=submission.circuit_id,
            proof_hash=proof_hash,
            reason="Proof failed the Groth16 pairing equation for this verification key.",
        )

    event = ComplianceEvaluationEvent(
        broker_id=submission.broker_id,
        transaction_id=submission.transaction_id,
        circular_id=submission.circular_id,
        clause_hash=submission.clause_hash,
        section_reference=submission.section_reference,
        rule_id=submission.rule_id,
        evaluation_result=EvaluationOutcome.PASS,
        details={
            "zk_proof": {
                "circuit_id": submission.circuit_id,
                "proof_hash": proof_hash,
                "public_signals": submission.public_signals,
            },
        },
    )
    entry = await ledger.append_entry(event)

    logger.info(
        "zk-SNARK proof verified and logged: broker_id=%s transaction_id=%s rule_id=%s circuit_id=%s sequence_num=%d",
        submission.broker_id, submission.transaction_id, submission.rule_id, submission.circuit_id, entry.sequence_num,
    )
    return ZKProofVerificationResult(
        verified=True,
        circuit_id=submission.circuit_id,
        proof_hash=proof_hash,
        ledger_sequence_num=entry.sequence_num,
    )
