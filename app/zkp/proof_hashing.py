"""Canonical SHA-256 hashing of a verified proof for ledger storage.

The ledger stores this hash, never the raw proof bytes: `details` is
already hashed into `payload_digest` (see app.ledger.hash_chain), so the
proof's integrity is already tamper-evident once it's in `details`, but
committing to a single canonical digest of just the proof lets an
auditor recompute and match it independently, and keeps `details` small
relative to a full proof + verification key + public signals blob.
"""
from __future__ import annotations

import hashlib
import json

from app.zkp.models import Groth16Proof


def compute_proof_hash(circuit_id: str, proof: Groth16Proof, public_signals: list[str]) -> str:
    canonical = {
        "circuit_id": circuit_id,
        "proof": proof.model_dump(mode="json"),
        "public_signals": list(public_signals),
    }
    encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
