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
from app.zkp.models import (
    ComplianceCollateralProofSubmission,
    ComplianceCollateralVerificationResult,
    MarginComplianceProofSubmission,
    ZKProofVerificationResult,
)
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


# In-memory replay tracking cache for zero-knowledge proofs
_SEEN_PROOF_HASHES: set[str] = set()


async def verify_and_log_collateral_proof(
    ledger: LedgerService,
    settings,
    submission: ComplianceCollateralProofSubmission,
) -> ComplianceCollateralVerificationResult:
    """Verifies a batch Compliance-as-Collateral Groth16 proof (PRD Addendum v2 Section 8.2).
    Fails closed on:
      - Unknown verification key / circuit_id
      - Structural point or signal malformations
      - Altered public inputs / parameter mismatches
      - Replayed proof hashes
      - Cryptographic pairing equation check failure

    On success, logs a PASS record into `compliance_audit_ledger` recording ONLY
    the proof hash and public commitments -- NEVER proprietary margin figures.
    """
    from app.zkp.models import ComplianceCollateralVerificationResult
    from app.zkp.witness import string_to_field_element

    proof_hash = compute_proof_hash(submission.circuit_id, submission.proof, submission.public_signals)

    # 1. Anti-Replay Check
    if proof_hash in _SEEN_PROOF_HASHES:
        return ComplianceCollateralVerificationResult(
            verified=False,
            circuit_id=submission.circuit_id,
            proof_hash=proof_hash,
            policy_id=submission.policy_id,
            policy_hash=submission.policy_hash,
            reporting_period_id=submission.reporting_period_id,
            dataset_commitment=submission.dataset_commitment,
            margin_threshold=submission.margin_threshold,
            reason="Proof replay detected: this exact proof has already been verified and logged.",
        )

    # 2. Public Signals Structural & Consistency Checks (Fail-Closed)
    if len(submission.public_signals) != 4:
        return ComplianceCollateralVerificationResult(
            verified=False,
            circuit_id=submission.circuit_id,
            proof_hash=proof_hash,
            policy_id=submission.policy_id,
            policy_hash=submission.policy_hash,
            reporting_period_id=submission.reporting_period_id,
            dataset_commitment=submission.dataset_commitment,
            margin_threshold=submission.margin_threshold,
            reason=f"Expected 4 public signals [policy_hash, reporting_period_id, dataset_commitment, margin_threshold], got {len(submission.public_signals)}.",
        )

    expected_policy_field = str(string_to_field_element(submission.policy_hash))
    expected_period_field = str(string_to_field_element(submission.reporting_period_id))
    expected_commitment_field = str(string_to_field_element(submission.dataset_commitment))
    expected_threshold_field = str(string_to_field_element(submission.margin_threshold))

    if submission.public_signals[0] != expected_policy_field:
        return ComplianceCollateralVerificationResult(
            verified=False,
            circuit_id=submission.circuit_id,
            proof_hash=proof_hash,
            policy_id=submission.policy_id,
            policy_hash=submission.policy_hash,
            reporting_period_id=submission.reporting_period_id,
            dataset_commitment=submission.dataset_commitment,
            margin_threshold=submission.margin_threshold,
            reason="Public signal policy_hash does not match submitted policy_hash.",
        )

    if submission.public_signals[1] != expected_period_field:
        return ComplianceCollateralVerificationResult(
            verified=False,
            circuit_id=submission.circuit_id,
            proof_hash=proof_hash,
            policy_id=submission.policy_id,
            policy_hash=submission.policy_hash,
            reporting_period_id=submission.reporting_period_id,
            dataset_commitment=submission.dataset_commitment,
            margin_threshold=submission.margin_threshold,
            reason="Public signal reporting_period_id does not match submitted reporting_period_id.",
        )

    if submission.public_signals[2] != expected_commitment_field:
        return ComplianceCollateralVerificationResult(
            verified=False,
            circuit_id=submission.circuit_id,
            proof_hash=proof_hash,
            policy_id=submission.policy_id,
            policy_hash=submission.policy_hash,
            reporting_period_id=submission.reporting_period_id,
            dataset_commitment=submission.dataset_commitment,
            margin_threshold=submission.margin_threshold,
            reason="Public signal dataset_commitment does not match submitted dataset_commitment.",
        )

    if submission.public_signals[3] != expected_threshold_field:
        return ComplianceCollateralVerificationResult(
            verified=False,
            circuit_id=submission.circuit_id,
            proof_hash=proof_hash,
            policy_id=submission.policy_id,
            policy_hash=submission.policy_hash,
            reporting_period_id=submission.reporting_period_id,
            dataset_commitment=submission.dataset_commitment,
            margin_threshold=submission.margin_threshold,
            reason="Public signal margin_threshold does not match submitted margin_threshold.",
        )

    # 3. Verification Key Lookup
    try:
        vk = get_verification_key(settings, submission.circuit_id)
    except UnknownCircuitError:
        return ComplianceCollateralVerificationResult(
            verified=False,
            circuit_id=submission.circuit_id,
            proof_hash=proof_hash,
            policy_id=submission.policy_id,
            policy_hash=submission.policy_hash,
            reporting_period_id=submission.reporting_period_id,
            dataset_commitment=submission.dataset_commitment,
            margin_threshold=submission.margin_threshold,
            reason=f"Unknown or unconfigured circuit_id '{submission.circuit_id}'.",
        )

    # 4. Cryptographic Pairing Verification
    try:
        verified = verify_groth16_proof(vk, submission.proof, submission.public_signals)
    except ProofVerificationError as exc:
        return ComplianceCollateralVerificationResult(
            verified=False,
            circuit_id=submission.circuit_id,
            proof_hash=proof_hash,
            policy_id=submission.policy_id,
            policy_hash=submission.policy_hash,
            reporting_period_id=submission.reporting_period_id,
            dataset_commitment=submission.dataset_commitment,
            margin_threshold=submission.margin_threshold,
            reason=str(exc),
        )

    if not verified:
        return ComplianceCollateralVerificationResult(
            verified=False,
            circuit_id=submission.circuit_id,
            proof_hash=proof_hash,
            policy_id=submission.policy_id,
            policy_hash=submission.policy_hash,
            reporting_period_id=submission.reporting_period_id,
            dataset_commitment=submission.dataset_commitment,
            margin_threshold=submission.margin_threshold,
            reason="Proof failed the Groth16 pairing equation for this verification key.",
        )

    # 5. Ledger Persistence (Public evidence only; zero private data)
    event = ComplianceEvaluationEvent(
        broker_id=submission.broker_id,
        transaction_id=f"collateral_{submission.reporting_period_id}_{submission.dataset_commitment[:16]}",
        circular_id=submission.circular_id,
        clause_hash=submission.clause_hash or ("0" * 64),
        section_reference=submission.section_reference,
        rule_id=submission.rule_id,
        evaluation_result=EvaluationOutcome.PASS,
        details={
            "compliance_collateral": {
                "circuit_id": submission.circuit_id,
                "proof_hash": proof_hash,
                "policy_id": submission.policy_id,
                "policy_hash": submission.policy_hash,
                "reporting_period_id": submission.reporting_period_id,
                "dataset_commitment": submission.dataset_commitment,
                "margin_threshold": submission.margin_threshold,
                "num_transactions": submission.num_transactions,
                "public_signals": submission.public_signals,
            }
        },
    )
    entry = await ledger.append_entry(event)
    _SEEN_PROOF_HASHES.add(proof_hash)

    logger.info(
        "Compliance collateral proof verified and logged: broker_id=%s policy_id=%s period=%s sequence_num=%d",
        submission.broker_id, submission.policy_id, submission.reporting_period_id, entry.sequence_num,
    )
    return ComplianceCollateralVerificationResult(
        verified=True,
        circuit_id=submission.circuit_id,
        proof_hash=proof_hash,
        policy_id=submission.policy_id,
        policy_hash=submission.policy_hash,
        reporting_period_id=submission.reporting_period_id,
        dataset_commitment=submission.dataset_commitment,
        margin_threshold=submission.margin_threshold,
        ledger_sequence_num=entry.sequence_num,
    )

