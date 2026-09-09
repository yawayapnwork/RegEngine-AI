"""Zero-knowledge proof verification endpoint (Requirements 2 & 3): a
broker submits a Groth16 proof (generated off-server -- see
app.zkp.prover_client / zk/scripts/generate_proof.sh) that a trade
satisfied `collected_margin >= required_margin`, without ever disclosing
the margin amount or client account identifier. RegEngine verifies the
proof server-side (app.zkp.groth16_verifier, pure-Python BN254 pairing
math, no trust placed in the client's own toolchain) and, only on
success, writes a PASS entry to the existing hash-chained
compliance_audit_ledger -- reusing that table rather than a separate
store, per Requirement 3.

Restricted to Broker_API_Client / System_Admin: this is a broker
submitting evidence of its own compliance, the same actor as
app.api.execution_routes' transaction-evaluation endpoint, not a
Compliance_Officer review action.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status

from app.config import Settings, get_settings
from app.ledger.dependencies import get_ledger_service
from app.ledger.service import LedgerService
from app.security.dependencies import require_roles
from app.security.models import Principal, Role
from app.zkp.models import (
    ComplianceCollateralProofSubmission,
    ComplianceCollateralVerificationResult,
    MarginComplianceProofSubmission,
    ZKProofVerificationResult,
)
from app.zkp.verification_service import verify_and_log_collateral_proof, verify_and_log_proof

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/zkp", tags=["Zero-Knowledge Proof Verification"])

_ALLOWED = require_roles(Role.BROKER_API_CLIENT, Role.SYSTEM_ADMIN)


def _require_enabled(settings: Settings) -> None:
    if not settings.zkp_enabled:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Zero-knowledge proof verification is not enabled on this deployment.")


@router.post("/verify", response_model=ZKProofVerificationResult, dependencies=[Depends(_ALLOWED)])
async def verify_proof(
    submission: MarginComplianceProofSubmission,
    settings: Settings = Depends(get_settings),
    ledger: LedgerService = Depends(get_ledger_service),
    principal: Principal = Depends(_ALLOWED),
) -> ZKProofVerificationResult:
    _require_enabled(settings)

    if not principal.is_admin() and submission.broker_id != (principal.tenant_id or ""):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Token tenant_id does not match submission.broker_id.")

    result = await verify_and_log_proof(ledger, settings, submission)
    if not result.verified:
        logger.warning(
            "zk-SNARK proof rejected: broker_id=%s transaction_id=%s circuit_id=%s reason=%s",
            submission.broker_id, submission.transaction_id, submission.circuit_id, result.reason,
        )
    return result


@router.post("/verify-collateral", response_model=ComplianceCollateralVerificationResult, dependencies=[Depends(_ALLOWED)])
async def verify_collateral_proof(
    submission: ComplianceCollateralProofSubmission,
    settings: Settings = Depends(get_settings),
    ledger: LedgerService = Depends(get_ledger_service),
    principal: Principal = Depends(_ALLOWED),
) -> ComplianceCollateralVerificationResult:
    """Public verification endpoint for Compliance-as-Collateral / Zero-Knowledge Proof of Adherence
    (PRD Addendum v2 Section 8.2).

    Accepts ONLY public signals and proof elements:
      - policy_id / policy_hash
      - reporting_period_id
      - dataset_commitment
      - margin_threshold
      - Groth16 proof (pi_a, pi_b, pi_c)

    Never exposes or accepts private transaction data, trade values, or account IDs.
    Fails closed on altered inputs, bad proofs, or replay attacks.
    """
    _require_enabled(settings)

    if not principal.is_admin() and submission.broker_id != (principal.tenant_id or ""):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Token tenant_id does not match submission.broker_id.")

    result = await verify_and_log_collateral_proof(ledger, settings, submission)
    if not result.verified:
        logger.warning(
            "Compliance collateral zk-SNARK proof rejected: broker_id=%s policy_id=%s circuit_id=%s reason=%s",
            submission.broker_id, submission.policy_id, submission.circuit_id, result.reason,
        )
    return result

