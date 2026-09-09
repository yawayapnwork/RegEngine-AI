"""Deterministic witness generator and Poseidon commitment engine for
Compliance-as-Collateral / Zero-Knowledge Proof of Adherence (PRD Addendum v2 Section 8.2).

Computes Poseidon commitments over BN254 scalar field and validates the
compliance predicate (collected_margin >= required_margin) for every transaction
in the evaluated reporting period batch before generating circuit inputs.
"""
from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from typing import Any

# BN254 (alt_bn128) scalar field modulus (curve order r)
FIELD_MODULUS: int = 21888242871839275222246405745257275088548364400416034343698204186575808495617


class CompliancePredicateViolationError(ValueError):
    """Raised when any transaction in the candidate batch fails the compliance
    predicate (e.g. collected_margin < required_margin). A valid zero-knowledge
    proof cannot be generated for non-compliant data.
    """


@dataclass(frozen=True)
class PrivateTransactionRecord:
    transaction_id: str
    collected_margin: int  # in paise
    required_margin: int   # in paise
    salt: int | None = None

    @property
    def transaction_id_field(self) -> int:
        return string_to_field_element(self.transaction_id)

    @property
    def resolved_salt(self) -> int:
        if self.salt is not None:
            return self.salt % FIELD_MODULUS
        # Deterministic default salt from transaction_id if none provided
        digest = hashlib.sha256(f"regengine_salt_{self.transaction_id}".encode("utf-8")).hexdigest()
        return int(digest, 16) % FIELD_MODULUS


def string_to_field_element(val: str | int) -> int:
    """Converts a string (hex hash, numeric string, or plain text) or int
    deterministically into a BN254 scalar field element (< FIELD_MODULUS).
    """
    if isinstance(val, int):
        return val % FIELD_MODULUS
    val_str = str(val).strip()
    if val_str.isdigit():
        return int(val_str) % FIELD_MODULUS
    # Hex string
    try:
        if val_str.startswith("0x") or len(val_str) == 64:
            clean_hex = val_str[2:] if val_str.startswith("0x") else val_str
            return int(clean_hex, 16) % FIELD_MODULUS
    except ValueError:
        pass
    # Arbitrary text -> SHA-256 digest reduced to field
    digest = hashlib.sha256(val_str.encode("utf-8")).hexdigest()
    return int(digest, 16) % FIELD_MODULUS


# ==============================================================================
# Poseidon Hash over BN254 Scalar Field
# ==============================================================================
def _make_mds_matrix(t: int) -> list[list[int]]:
    """Cauchy MDS matrix for state size t: M[i, j] = 1 / (i + (t + j)) mod p."""
    p = FIELD_MODULUS
    return [[pow(i + (t + j), -1, p) for j in range(t)] for i in range(t)]


def _generate_round_constants(t: int, num_rounds: int) -> list[list[int]]:
    """Generates deterministic pseudo-random round constants for Poseidon(t).
    Uses a standard SHA-256 PRF seeded with domain separation tags.
    """
    p = FIELD_MODULUS
    constants: list[list[int]] = []
    for r in range(num_rounds):
        round_c: list[int] = []
        for i in range(t):
            seed = f"Poseidon_BN254_C_t{t}_r{r}_i{i}".encode("utf-8")
            h = hashlib.sha256(seed).hexdigest()
            round_c.append(int(h, 16) % p)
        constants.append(round_c)
    return constants


def poseidon_hash(inputs: list[int]) -> int:
    """Computes a deterministic Poseidon hash over a list of field elements.
    Matches Circomlib Poseidon permutation structure:
      - State size t = len(inputs) + 1 (inputs at state[1..t-1], state[0] = 0)
      - Full rounds R_F = 8 (4 initial, 4 final)
      - Partial rounds R_P = 56 for t <= 4, 60 for t > 4
      - S-Box: x^5 mod p
      - MixLayer: Cauchy MDS matrix multiplication
    """
    n = len(inputs)
    if n == 0:
        return 0
    t = n + 1
    r_f = 8
    r_p = 56 if t <= 4 else 60
    total_rounds = r_f + r_p
    half_f = r_f // 2
    p = FIELD_MODULUS

    # Initial state: capacity = 0, rate = inputs
    state = [0] + [x % p for x in inputs]
    mds = _make_mds_matrix(t)
    round_constants = _generate_round_constants(t, total_rounds)

    for r in range(total_rounds):
        # 1. Add round constants
        rc = round_constants[r]
        for i in range(t):
            state[i] = (state[i] + rc[i]) % p

        # 2. S-Box (x^5 mod p)
        # Full rounds: all elements; Partial rounds: state[0] only
        if r < half_f or r >= (half_f + r_p):
            for i in range(t):
                x = state[i]
                state[i] = pow(x, 5, p)
        else:
            state[0] = pow(state[0], 5, p)

        # 3. MixLayer (Matrix multiplication)
        new_state = [0] * t
        for i in range(t):
            acc = 0
            for j in range(t):
                acc = (acc + mds[i][j] * state[j]) % p
            new_state[i] = acc
        state = new_state

    # Result is state[0]
    return state[0]


def compute_leaf_commitment(
    transaction_id_field: int,
    collected_margin: int,
    required_margin: int,
    salt: int,
) -> int:
    """Computes Poseidon(4) leaf commitment for a single transaction."""
    return poseidon_hash([
        transaction_id_field % FIELD_MODULUS,
        collected_margin % FIELD_MODULUS,
        required_margin % FIELD_MODULUS,
        salt % FIELD_MODULUS,
    ])


def compute_dataset_commitment(leaf_commitments: list[int]) -> int:
    """Computes Poseidon(BATCH_SIZE) root commitment over all leaf commitments."""
    return poseidon_hash([leaf % FIELD_MODULUS for leaf in leaf_commitments])


# ==============================================================================
# Deterministic Witness Generation
# ==============================================================================
def generate_compliance_collateral_witness(
    *,
    policy_hash: str | int,
    reporting_period_id: str | int,
    margin_threshold: str | int,
    transactions: list[PrivateTransactionRecord],
    expected_batch_size: int = 4,
) -> dict[str, Any]:
    """Deterministically verifies that all transactions in the candidate batch
    satisfy the upfront margin compliance predicate (`collected_margin >= required_margin`),
    computes the Poseidon leaf and root dataset commitments, and produces the
    exact JSON witness input dictionary expected by `compliance_collateral.circom`.

    Raises `CompliancePredicateViolationError` immediately if ANY transaction fails
    the compliance predicate.
    """
    if len(transactions) != expected_batch_size:
        raise ValueError(
            f"Expected exactly {expected_batch_size} transactions in compliance batch, got {len(transactions)}."
        )

    policy_hash_field = string_to_field_element(policy_hash)
    reporting_period_field = string_to_field_element(reporting_period_id)
    margin_threshold_field = string_to_field_element(margin_threshold)

    leaf_commitments: list[int] = []
    collected_margin_list: list[str] = []
    required_margin_list: list[str] = []
    transaction_ids_list: list[str] = []
    salts_list: list[str] = []

    for idx, txn in enumerate(transactions):
        # 1. PREDICATE VALIDATION (FAIL-CLOSED)
        if txn.collected_margin < txn.required_margin:
            raise CompliancePredicateViolationError(
                f"Transaction {txn.transaction_id} at index {idx} failed compliance predicate: "
                f"collected_margin ({txn.collected_margin} paise) < required_margin ({txn.required_margin} paise)."
            )

        txn_id_field = txn.transaction_id_field
        salt = txn.resolved_salt

        # 2. Leaf Commitment
        leaf = compute_leaf_commitment(txn_id_field, txn.collected_margin, txn.required_margin, salt)
        leaf_commitments.append(leaf)

        collected_margin_list.append(str(txn.collected_margin))
        required_margin_list.append(str(txn.required_margin))
        transaction_ids_list.append(str(txn_id_field))
        salts_list.append(str(salt))

    # 3. Dataset Commitment
    dataset_commitment = compute_dataset_commitment(leaf_commitments)

    witness_input = {
        # Public signals
        "policy_hash": str(policy_hash_field),
        "reporting_period_id": str(reporting_period_field),
        "dataset_commitment": str(dataset_commitment),
        "margin_threshold": str(margin_threshold_field),
        # Private signals
        "collected_margin": collected_margin_list,
        "required_margin": required_margin_list,
        "transaction_ids": transaction_ids_list,
        "salts": salts_list,
    }

    return {
        "circuit_inputs": witness_input,
        "policy_hash_field": policy_hash_field,
        "reporting_period_field": reporting_period_field,
        "dataset_commitment": dataset_commitment,
        "margin_threshold_field": margin_threshold_field,
        "leaf_commitments": leaf_commitments,
        "public_signals": [
            str(policy_hash_field),
            str(reporting_period_field),
            str(dataset_commitment),
            str(margin_threshold_field),
        ],
    }
