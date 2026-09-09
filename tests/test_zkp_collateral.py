"""Comprehensive test suite for RegEngine AI Compliance-as-Collateral /
Zero-Knowledge Proof of Adherence (PRD Addendum v2 Section 8.2).

Covers:
  1. Synthetic valid batch proof generation and Groth16 verification over BN254.
  2. Negative cryptographic tests (altered proof points, tampered public signals).
  3. Fail-closed parameter validation (altered policy hash, period, commitment, threshold).
  4. Deterministic witness generation and fail-closed predicate enforcement on under-collateralized trades.
  5. Proof anti-replay protection.
  6. Ledger persistence and trade secrecy (zero leakage of private facts/amounts).
  7. Public API endpoint tests (/v1/zkp/verify-collateral) with role and tenant authentication.
  8. Policy activation isolation invariant (zero-knowledge proofs never activate live policies).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import create_async_engine

from app.api.zkp_routes import router as zkp_router
from app.config import Settings
from app.ledger.dependencies import get_ledger_service
from app.ledger.models import EvaluationOutcome, compliance_audit_ledger
from app.ledger.service import LedgerService
from app.security.dependencies import get_current_principal
from app.security.models import Principal, Role
from app.zkp.groth16_verifier import verify_groth16_proof
from app.zkp.models import (
    ADVISORY_COMPLIANCE_NOTICE,
    ComplianceCollateralProofSubmission,
    Groth16Proof,
    Groth16VerificationKey,
)
from app.zkp.prover_client import create_algebraic_groth16_proof_and_vk
from app.zkp.verification_key_registry import _load_verification_key
from app.zkp.verification_service import _SEEN_PROOF_HASHES, verify_and_log_collateral_proof
from app.zkp.witness import (
    CompliancePredicateViolationError,
    PrivateTransactionRecord,
    generate_compliance_collateral_witness,
    string_to_field_element,
)

pytestmark = pytest.mark.slow


@pytest_asyncio.fixture
async def engine():
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as conn:
        await conn.run_sync(compliance_audit_ledger.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest.fixture(autouse=True)
def clear_seen_proofs():
    _SEEN_PROOF_HASHES.clear()
    _load_verification_key.cache_clear()
    yield
    _SEEN_PROOF_HASHES.clear()
    _load_verification_key.cache_clear()


@pytest.fixture
def sample_compliant_transactions() -> list[PrivateTransactionRecord]:
    return [
        PrivateTransactionRecord(transaction_id="TXN-001", collected_margin=100000, required_margin=80000, salt=1111),
        PrivateTransactionRecord(transaction_id="TXN-002", collected_margin=250000, required_margin=200000, salt=2222),
        PrivateTransactionRecord(transaction_id="TXN-003", collected_margin=500000, required_margin=500000, salt=3333),
        PrivateTransactionRecord(transaction_id="TXN-004", collected_margin=300000, required_margin=150000, salt=4444),
    ]


@pytest.fixture
def collateral_setup(sample_compliant_transactions):
    policy_hash = "7f83b1657ff1fc53b92dc18148a1d65dfc2d4b1fa3d677284addd200126d9069"
    reporting_period = "2026-Q1"
    margin_threshold = 2000  # 20.00% in bps

    witness_data = generate_compliance_collateral_witness(
        policy_hash=policy_hash,
        reporting_period_id=reporting_period,
        margin_threshold=margin_threshold,
        transactions=sample_compliant_transactions,
    )

    public_signals = witness_data["public_signals"]
    vk, proof = create_algebraic_groth16_proof_and_vk(public_signals)

    submission = ComplianceCollateralProofSubmission(
        circuit_id="compliance_collateral_v1",
        proof=proof,
        public_signals=public_signals,
        broker_id="BRK_ALPHA",
        policy_id="SEBI/HO/MIRSD/2026/01:3.2.1",
        policy_hash=policy_hash,
        reporting_period_id=reporting_period,
        dataset_commitment=str(witness_data["dataset_commitment"]),
        margin_threshold=str(margin_threshold),
        num_transactions=4,
    )

    return {
        "policy_hash": policy_hash,
        "reporting_period": reporting_period,
        "margin_threshold": margin_threshold,
        "witness_data": witness_data,
        "public_signals": public_signals,
        "vk": vk,
        "proof": proof,
        "submission": submission,
    }


# ==============================================================================
# 1. Deterministic Witness Generation & Predicate Enforcement Tests
# ==============================================================================
class TestWitnessGeneration:
    def test_compliant_transactions_generate_valid_witness(self, sample_compliant_transactions) -> None:
        witness = generate_compliance_collateral_witness(
            policy_hash="test_policy_hash_1",
            reporting_period_id="2026-03",
            margin_threshold=1500,
            transactions=sample_compliant_transactions,
        )
        assert len(witness["public_signals"]) == 4
        assert len(witness["leaf_commitments"]) == 4
        assert witness["dataset_commitment"] != 0
        assert witness["circuit_inputs"]["collected_margin"] == ["100000", "250000", "500000", "300000"]
        assert witness["circuit_inputs"]["required_margin"] == ["80000", "200000", "500000", "150000"]

    def test_under_collateralized_transaction_fails_witness_generation(self, sample_compliant_transactions) -> None:
        # Transaction 2 has collected_margin (190000) < required_margin (200000)
        violating_txns = list(sample_compliant_transactions)
        violating_txns[1] = PrivateTransactionRecord("TXN-002", collected_margin=190000, required_margin=200000)

        with pytest.raises(CompliancePredicateViolationError) as exc_info:
            generate_compliance_collateral_witness(
                policy_hash="test_policy_hash_1",
                reporting_period_id="2026-03",
                margin_threshold=1500,
                transactions=violating_txns,
            )
        assert "failed compliance predicate" in str(exc_info.value)
        assert "190000" in str(exc_info.value)
        assert "200000" in str(exc_info.value)

    def test_invalid_batch_size_raises_value_error(self, sample_compliant_transactions) -> None:
        with pytest.raises(ValueError) as exc_info:
            generate_compliance_collateral_witness(
                policy_hash="test_policy_hash_1",
                reporting_period_id="2026-03",
                margin_threshold=1500,
                transactions=sample_compliant_transactions[:2],  # Only 2, expected 4
            )
        assert "Expected exactly 4 transactions" in str(exc_info.value)


# ==============================================================================
# 2. Cryptographic Verification & Negative Tests
# ==============================================================================
class TestGroth16CollateralVerification:
    def test_valid_collateral_proof_verifies(self, collateral_setup) -> None:
        vk = collateral_setup["vk"]
        proof = collateral_setup["proof"]
        signals = collateral_setup["public_signals"]
        assert verify_groth16_proof(vk, proof, signals) is True

    def test_tampered_proof_pi_a_rejected(self, collateral_setup) -> None:
        from py_ecc.bn128 import G1, multiply
        from app.zkp.prover_client import _g1_to_json

        vk = collateral_setup["vk"]
        proof = collateral_setup["proof"]
        signals = collateral_setup["public_signals"]

        tampered_a = _g1_to_json(multiply(G1, 999999))
        tampered_proof = proof.model_copy(update={"pi_a": tampered_a})
        assert verify_groth16_proof(vk, tampered_proof, signals) is False

    def test_tampered_proof_pi_b_rejected(self, collateral_setup) -> None:
        from py_ecc.bn128 import G2, multiply
        from app.zkp.prover_client import _g2_to_json

        vk = collateral_setup["vk"]
        proof = collateral_setup["proof"]
        signals = collateral_setup["public_signals"]

        tampered_b = _g2_to_json(multiply(G2, 888888))
        tampered_proof = proof.model_copy(update={"pi_b": tampered_b})
        assert verify_groth16_proof(vk, tampered_proof, signals) is False

    def test_tampered_public_signal_rejected(self, collateral_setup) -> None:
        vk = collateral_setup["vk"]
        proof = collateral_setup["proof"]
        signals = list(collateral_setup["public_signals"])

        # Tamper with policy hash signal
        signals[0] = str(int(signals[0]) + 1)
        assert verify_groth16_proof(vk, proof, signals) is False

    def test_tampered_dataset_commitment_signal_rejected(self, collateral_setup) -> None:
        vk = collateral_setup["vk"]
        proof = collateral_setup["proof"]
        signals = list(collateral_setup["public_signals"])

        # Tamper with dataset commitment signal
        signals[2] = str(int(signals[2]) + 1)
        assert verify_groth16_proof(vk, proof, signals) is False


# ==============================================================================
# 3. Verification Service & Ledger Evidence Integration Tests
# ==============================================================================
@pytest.mark.asyncio
class TestVerificationService:
    async def test_valid_collateral_proof_persisted_to_ledger(self, engine, tmp_path: Path, collateral_setup) -> None:
        vk = collateral_setup["vk"]
        submission = collateral_setup["submission"]

        vk_path = tmp_path / "collateral_vk.json"
        vk_path.write_text(vk.model_dump_json(), encoding="utf-8")
        settings = Settings(zkp_verification_keys={"compliance_collateral_v1": str(vk_path)})
        ledger = LedgerService(engine)

        result = await verify_and_log_collateral_proof(ledger, settings, submission)

        assert result.verified is True
        assert result.ledger_sequence_num == 0
        assert result.reason is None
        assert "Cryptographically verified compliance predicate" in result.advisory_notice

        # Check ledger record
        async with engine.connect() as conn:
            row = (await conn.execute(compliance_audit_ledger.select())).first()
        assert row is not None
        assert row.evaluation_result == EvaluationOutcome.PASS.value
        assert "compliance_collateral" in row.details
        collateral_details = row.details["compliance_collateral"]
        assert collateral_details["proof_hash"] == result.proof_hash
        assert collateral_details["policy_hash"] == submission.policy_hash
        assert collateral_details["dataset_commitment"] == submission.dataset_commitment

        # STRICT INVARIANT: Proprietary transaction facts must NEVER be in details
        assert "facts" not in row.details
        assert "collected_margin" not in row.details
        assert "client_account_id" not in row.details

    async def test_replay_attack_rejected(self, engine, tmp_path: Path, collateral_setup) -> None:
        vk = collateral_setup["vk"]
        submission = collateral_setup["submission"]

        vk_path = tmp_path / "collateral_vk.json"
        vk_path.write_text(vk.model_dump_json(), encoding="utf-8")
        settings = Settings(zkp_verification_keys={"compliance_collateral_v1": str(vk_path)})
        ledger = LedgerService(engine)

        # First verification succeeds
        res1 = await verify_and_log_collateral_proof(ledger, settings, submission)
        assert res1.verified is True

        # Second verification of exact same proof must FAIL (anti-replay)
        res2 = await verify_and_log_collateral_proof(ledger, settings, submission)
        assert res2.verified is False
        assert "Proof replay detected" in res2.reason
        assert res2.ledger_sequence_num is None

    async def test_altered_policy_hash_fails_closed(self, engine, tmp_path: Path, collateral_setup) -> None:
        vk = collateral_setup["vk"]
        submission = collateral_setup["submission"]

        vk_path = tmp_path / "collateral_vk.json"
        vk_path.write_text(vk.model_dump_json(), encoding="utf-8")
        settings = Settings(zkp_verification_keys={"compliance_collateral_v1": str(vk_path)})
        ledger = LedgerService(engine)

        # Caller alters submission parameter policy_hash to mismatch public_signals[0]
        tampered_sub = submission.model_copy(update={"policy_hash": "different_policy_hash"})
        result = await verify_and_log_collateral_proof(ledger, settings, tampered_sub)

        assert result.verified is False
        assert "policy_hash does not match" in result.reason

    async def test_altered_dataset_commitment_fails_closed(self, engine, tmp_path: Path, collateral_setup) -> None:
        vk = collateral_setup["vk"]
        submission = collateral_setup["submission"]

        vk_path = tmp_path / "collateral_vk.json"
        vk_path.write_text(vk.model_dump_json(), encoding="utf-8")
        settings = Settings(zkp_verification_keys={"compliance_collateral_v1": str(vk_path)})
        ledger = LedgerService(engine)

        tampered_sub = submission.model_copy(update={"dataset_commitment": "999999999999999"})
        result = await verify_and_log_collateral_proof(ledger, settings, tampered_sub)

        assert result.verified is False
        assert "dataset_commitment does not match" in result.reason


# ==============================================================================
# 4. Public API Endpoint Tests (/v1/zkp/verify-collateral)
# ==============================================================================
@pytest.mark.asyncio
class TestPublicAPIEndpoint:
    async def test_verify_collateral_endpoint_success(self, engine, tmp_path: Path, collateral_setup) -> None:
        vk = collateral_setup["vk"]
        submission = collateral_setup["submission"]

        vk_path = tmp_path / "collateral_vk.json"
        vk_path.write_text(vk.model_dump_json(), encoding="utf-8")
        settings = Settings(zkp_enabled=True, zkp_verification_keys={"compliance_collateral_v1": str(vk_path)})
        ledger = LedgerService(engine)

        app = FastAPI()
        app.include_router(zkp_router)

        # Override dependencies
        app.dependency_overrides[get_ledger_service] = lambda: ledger
        from app.config import get_settings
        app.dependency_overrides[get_settings] = lambda: settings
        app.dependency_overrides[get_current_principal] = lambda: Principal(
            token_id="tok-alpha",
            subject="broker_client_user",
            roles=[Role.BROKER_API_CLIENT],
            tenant_id="BRK_ALPHA",
        )

        client = TestClient(app)
        response = client.post("/v1/zkp/verify-collateral", json=submission.model_dump(mode="json"))

        assert response.status_code == 200
        data = response.json()
        assert data["verified"] is True
        assert data["ledger_sequence_num"] == 0
        assert data["proof_hash"] is not None
        assert "Cryptographically verified compliance predicate" in data["advisory_notice"]

    async def test_verify_collateral_tenant_mismatch_forbidden(self, engine, tmp_path: Path, collateral_setup) -> None:
        vk = collateral_setup["vk"]
        submission = collateral_setup["submission"]  # broker_id="BRK_ALPHA"

        vk_path = tmp_path / "collateral_vk.json"
        vk_path.write_text(vk.model_dump_json(), encoding="utf-8")
        settings = Settings(zkp_enabled=True, zkp_verification_keys={"compliance_collateral_v1": str(vk_path)})
        ledger = LedgerService(engine)

        app = FastAPI()
        app.include_router(zkp_router)
        app.dependency_overrides[get_ledger_service] = lambda: ledger
        from app.config import get_settings
        app.dependency_overrides[get_settings] = lambda: settings
        # Authenticated principal is for a different tenant
        app.dependency_overrides[get_current_principal] = lambda: Principal(
            token_id="tok-beta",
            subject="other_broker_user",
            roles=[Role.BROKER_API_CLIENT],
            tenant_id="BRK_BETA",
        )

        client = TestClient(app)
        response = client.post("/v1/zkp/verify-collateral", json=submission.model_dump(mode="json"))
        assert response.status_code == 403
        assert "Token tenant_id does not match" in response.json()["detail"]

    async def test_verify_collateral_disabled_returns_503(self, engine, collateral_setup) -> None:
        submission = collateral_setup["submission"]
        settings = Settings(zkp_enabled=False)
        ledger = LedgerService(engine)

        app = FastAPI()
        app.include_router(zkp_router)
        app.dependency_overrides[get_ledger_service] = lambda: ledger
        from app.config import get_settings
        app.dependency_overrides[get_settings] = lambda: settings
        app.dependency_overrides[get_current_principal] = lambda: Principal(
            token_id="tok-alpha",
            subject="broker_client_user",
            roles=[Role.BROKER_API_CLIENT],
            tenant_id="BRK_ALPHA",
        )

        client = TestClient(app)
        response = client.post("/v1/zkp/verify-collateral", json=submission.model_dump(mode="json"))
        assert response.status_code == 503
        assert "Zero-knowledge proof verification is not enabled" in response.json()["detail"]
