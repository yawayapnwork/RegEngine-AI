"""Pydantic I/O models for the zk-SNARK verification pipeline, shaped to
match snarkjs's own JSON exports byte-for-byte (proof.json,
verification_key.json, public.json) so a broker can POST snarkjs's
output directly with no reshaping on either side.
"""
from __future__ import annotations

from pydantic import BaseModel, Field


class Groth16Proof(BaseModel):
    """The exact shape of snarkjs's `proof.json` for protocol="groth16",
    curve="bn128". `pi_a`/`pi_c` are G1 points (3 projective coordinates,
    always affine-normalized by snarkjs so the 3rd is "1"); `pi_b` is a
    G2 point (3 coordinates, each an [x0, x1] Fp2 pair). All coordinates
    are decimal-string-encoded field elements, matching snarkjs exactly
    (JSON numbers cannot losslessly hold a ~254-bit BN254 field element)."""

    pi_a: list[str] = Field(..., min_length=3, max_length=3)
    pi_b: list[list[str]] = Field(..., min_length=3, max_length=3)
    pi_c: list[str] = Field(..., min_length=3, max_length=3)
    protocol: str = "groth16"
    curve: str = "bn128"


class Groth16VerificationKey(BaseModel):
    """The exact shape of snarkjs's `verification_key.json`. `IC` has
    exactly nPublic+1 entries (IC[0] is the constant term; IC[i+1]
    corresponds to public_signals[i]) -- see
    app.zkp.groth16_verifier.verify_groth16_proof's docstring for how
    these combine into vk_x."""

    protocol: str = "groth16"
    curve: str = "bn128"
    nPublic: int
    vk_alpha_1: list[str]
    vk_beta_2: list[list[str]]
    vk_gamma_2: list[list[str]]
    vk_delta_2: list[list[str]]
    IC: list[list[str]]


class MarginComplianceProofSubmission(BaseModel):
    """What a Broker_API_Client POSTs to /v1/zkp/verify -- the proof plus
    everything RegEngine needs to tie it to a specific transaction and
    rule in the audit ledger, but deliberately nothing that would
    reconstruct `collected_margin` or `client_account_id` (those never
    leave the broker's `generate_proof.sh`/`prover_client.py` run)."""

    circuit_id: str = Field("margin_compliance_v1", description="Key into app.zkp.verification_key_registry -- which verification_key.json to check this proof against.")
    proof: Groth16Proof
    public_signals: list[str] = Field(..., description="Decimal-string field elements, in the circuit's declared public-input order: [required_margin, transaction_id_field, commitment].")

    broker_id: str
    transaction_id: str
    circular_id: str
    clause_hash: str
    section_reference: str
    rule_id: str


class ZKProofVerificationResult(BaseModel):
    verified: bool
    circuit_id: str
    proof_hash: str = Field(..., description="SHA-256 over the canonical (proof, public_signals, circuit_id) JSON -- what gets written into the ledger's `details.zk_proof.proof_hash`, not the proof bytes themselves.")
    ledger_sequence_num: int | None = Field(None, description="Set only when verification succeeded and the ledger write completed.")
    reason: str | None = Field(None, description="Set only when verified=False -- why (bad circuit_id, malformed points, or the pairing equation not holding).")
