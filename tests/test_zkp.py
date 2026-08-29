"""Tests for app.zkp: the Groth16 verifier is exercised against a
mathematically-constructed toy proof using real py_ecc BN254 pairing
arithmetic (no mocked cryptography) -- see `_build_toy_setup` below for
how a valid proof is derived directly from the verification equation
rather than from a real circom trusted setup (which needs snarkjs/circom,
neither installed in this sandbox). Proof hashing, the verification-key
registry, and the end-to-end verify-then-ledger-write path (against a
real in-memory SQLite ledger, matching tests/test_ledger.py's fixture)
are also covered.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import Settings
from app.ledger.models import EvaluationOutcome, compliance_audit_ledger
from app.ledger.service import LedgerService
from app.zkp.groth16_verifier import ProofVerificationError, verify_groth16_proof
from app.zkp.models import Groth16Proof, Groth16VerificationKey, MarginComplianceProofSubmission
from app.zkp.proof_hashing import compute_proof_hash
from app.zkp.verification_key_registry import UnknownCircuitError, _load_verification_key, get_verification_key
from app.zkp.verification_service import verify_and_log_proof


def _g1_to_json(point) -> list[str]:
    return [str(int(point[0])), str(int(point[1])), "1"]


def _g2_to_json(point) -> list[list[str]]:
    x, y = point
    return [[str(int(x.coeffs[0])), str(int(x.coeffs[1]))], [str(int(y.coeffs[0])), str(int(y.coeffs[1]))], ["1", "0"]]


def _build_toy_setup(public_signal: int):
    """Constructs a verification key and a proof that satisfies Groth16's
    pairing equation by direct algebraic construction:

        e(pi_a, pi_b) == e(alpha1,beta2) * e(vk_x,gamma2) * e(pi_c,delta2)

    Every point is `scalar * generator`, so both sides reduce to
    `e(G1,G2)` raised to a scalar exponent; `pi_c`'s scalar is solved for
    so the exponents match exactly:

        pa*pb == a*b + vk_x_scalar*g + pc*d   (mod curve_order)
        pc == (pa*pb - a*b - vk_x_scalar*g) * inverse(d)   (mod curve_order)

    This is not a real circuit's trusted setup (there is no R1CS behind
    it), but it genuinely exercises the same pairing check
    app.zkp.groth16_verifier runs against real snarkjs output -- a
    tampered public signal or proof point breaks the same equation
    either way, which is what the negative tests below confirm.
    """
    from py_ecc.bn128 import G1, G2, curve_order, multiply

    a, b, g, d = 7, 11, 13, 17
    ic0_scalar, ic1_scalar = 3, 5
    pa, pb = 19, 23

    vk_x_scalar = (ic0_scalar + public_signal * ic1_scalar) % curve_order
    pc = (pa * pb - a * b - vk_x_scalar * g) * pow(d, -1, curve_order) % curve_order

    vk = Groth16VerificationKey(
        nPublic=1,
        vk_alpha_1=_g1_to_json(multiply(G1, a)),
        vk_beta_2=_g2_to_json(multiply(G2, b)),
        vk_gamma_2=_g2_to_json(multiply(G2, g)),
        vk_delta_2=_g2_to_json(multiply(G2, d)),
        IC=[_g1_to_json(multiply(G1, ic0_scalar)), _g1_to_json(multiply(G1, ic1_scalar))],
    )
    proof = Groth16Proof(
        pi_a=_g1_to_json(multiply(G1, pa)),
        pi_b=_g2_to_json(multiply(G2, pb)),
        pi_c=_g1_to_json(multiply(G1, pc)),
    )
    return vk, proof


class TestGroth16Verifier:
    def test_valid_proof_verifies(self) -> None:
        vk, proof = _build_toy_setup(public_signal=42)
        assert verify_groth16_proof(vk, proof, ["42"]) is True

    def test_tampered_public_signal_is_rejected(self) -> None:
        vk, proof = _build_toy_setup(public_signal=42)
        assert verify_groth16_proof(vk, proof, ["43"]) is False

    def test_tampered_proof_point_is_rejected(self) -> None:
        from py_ecc.bn128 import G1, multiply

        vk, proof = _build_toy_setup(public_signal=42)
        tampered_pi_a = _g1_to_json(multiply(G1, 999))
        tampered = proof.model_copy(update={"pi_a": tampered_pi_a})
        assert verify_groth16_proof(vk, tampered, ["42"]) is False

    def test_wrong_public_signal_count_raises(self) -> None:
        vk, proof = _build_toy_setup(public_signal=42)
        with pytest.raises(ProofVerificationError):
            verify_groth16_proof(vk, proof, ["42", "1"])

    def test_malformed_g1_point_raises(self) -> None:
        vk, proof = _build_toy_setup(public_signal=42)
        malformed = proof.model_copy(update={"pi_a": ["not-a-number", "2", "1"]})
        with pytest.raises(ProofVerificationError):
            verify_groth16_proof(vk, malformed, ["42"])


class TestProofHashing:
    def test_hash_is_deterministic(self) -> None:
        _, proof = _build_toy_setup(public_signal=42)
        h1 = compute_proof_hash("margin_compliance_v1", proof, ["42"])
        h2 = compute_proof_hash("margin_compliance_v1", proof, ["42"])
        assert h1 == h2
        assert len(h1) == 64

    def test_hash_changes_with_public_signals(self) -> None:
        _, proof = _build_toy_setup(public_signal=42)
        h1 = compute_proof_hash("margin_compliance_v1", proof, ["42"])
        h2 = compute_proof_hash("margin_compliance_v1", proof, ["43"])
        assert h1 != h2


class TestVerificationKeyRegistry:
    def test_loads_and_caches_vk_from_configured_path(self, tmp_path: Path) -> None:
        vk, _ = _build_toy_setup(public_signal=42)
        vk_path = tmp_path / "vk.json"
        vk_path.write_text(vk.model_dump_json(), encoding="utf-8")
        _load_verification_key.cache_clear()

        settings = Settings(zkp_verification_keys={"toy_circuit": str(vk_path)})
        loaded = get_verification_key(settings, "toy_circuit")
        assert loaded.nPublic == 1

    def test_unknown_circuit_id_raises(self) -> None:
        settings = Settings(zkp_verification_keys={})
        with pytest.raises(UnknownCircuitError):
            get_verification_key(settings, "does_not_exist")

    def test_configured_but_missing_file_raises_unknown_circuit(self, tmp_path: Path) -> None:
        _load_verification_key.cache_clear()
        settings = Settings(zkp_verification_keys={"toy_circuit": str(tmp_path / "missing.json")})
        with pytest.raises(UnknownCircuitError):
            get_verification_key(settings, "toy_circuit")


@pytest_asyncio.fixture
async def engine():
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as conn:
        await conn.run_sync(compliance_audit_ledger.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest.mark.asyncio
class TestVerificationService:
    async def test_valid_proof_writes_pass_entry_without_facts(self, engine, tmp_path: Path) -> None:
        vk, proof = _build_toy_setup(public_signal=42)
        vk_path = tmp_path / "vk.json"
        vk_path.write_text(vk.model_dump_json(), encoding="utf-8")
        _load_verification_key.cache_clear()
        settings = Settings(zkp_verification_keys={"margin_compliance_v1": str(vk_path)})

        submission = MarginComplianceProofSubmission(
            proof=proof,
            public_signals=["42"],
            broker_id="BRK0001",
            transaction_id="TXN0001",
            circular_id="SEBI/HO/MIRSD/2026/01",
            clause_hash="a" * 64,
            section_reference="3.2.1",
            rule_id="a" * 64 + ":3.2.1",
        )
        service = LedgerService(engine)

        result = await verify_and_log_proof(service, settings, submission)

        assert result.verified is True
        assert result.ledger_sequence_num == 0
        assert result.reason is None

        async with engine.connect() as conn:
            row = (await conn.execute(compliance_audit_ledger.select())).first()
        assert row.evaluation_result == EvaluationOutcome.PASS.value
        assert row.details["zk_proof"]["proof_hash"] == result.proof_hash
        assert "facts" not in row.details
        assert "entity_type" not in row.details

    async def test_invalid_proof_does_not_write_to_ledger(self, engine, tmp_path: Path) -> None:
        vk, proof = _build_toy_setup(public_signal=42)
        vk_path = tmp_path / "vk.json"
        vk_path.write_text(vk.model_dump_json(), encoding="utf-8")
        _load_verification_key.cache_clear()
        settings = Settings(zkp_verification_keys={"margin_compliance_v1": str(vk_path)})

        submission = MarginComplianceProofSubmission(
            proof=proof,
            public_signals=["43"],  # tampered -- doesn't match the proof
            broker_id="BRK0001",
            transaction_id="TXN0001",
            circular_id="SEBI/HO/MIRSD/2026/01",
            clause_hash="a" * 64,
            section_reference="3.2.1",
            rule_id="a" * 64 + ":3.2.1",
        )
        service = LedgerService(engine)

        result = await verify_and_log_proof(service, settings, submission)

        assert result.verified is False
        assert result.ledger_sequence_num is None
        assert result.reason is not None

        async with engine.connect() as conn:
            rows = (await conn.execute(compliance_audit_ledger.select())).all()
        assert rows == []

    async def test_unknown_circuit_id_does_not_write_to_ledger(self, engine) -> None:
        settings = Settings(zkp_verification_keys={})
        _, proof = _build_toy_setup(public_signal=42)
        submission = MarginComplianceProofSubmission(
            circuit_id="nonexistent",
            proof=proof,
            public_signals=["42"],
            broker_id="BRK0001",
            transaction_id="TXN0001",
            circular_id="SEBI/HO/MIRSD/2026/01",
            clause_hash="a" * 64,
            section_reference="3.2.1",
            rule_id="a" * 64 + ":3.2.1",
        )
        service = LedgerService(engine)

        result = await verify_and_log_proof(service, settings, submission)
        assert result.verified is False
        assert "nonexistent" in result.reason
