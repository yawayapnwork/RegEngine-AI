"""Client-side (broker-side) proof generation -- the Python equivalent
of zk/scripts/generate_proof.sh, for a broker who wants to call this
from their own Python integration rather than shelling out by hand.

This module is NEVER imported by anything under app.api or app.execution
-- it has no business running on RegEngine's server, since its entire
purpose is to touch `collected_margin` and `client_account_id` on
infrastructure the broker controls, before those values are thrown away
and only a proof is sent onward. It is shipped so a broker's own
integration code can `from app.zkp.prover_client import generate_margin_compliance_proof`
if they've pulled in this package, exactly as they would call
generate_proof.sh from a shell pipeline -- both paths invoke the same
two snarkjs commands.
"""
from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

from app.zkp.models import Groth16Proof


class ProofGenerationError(RuntimeError):
    """A `node`/`snarkjs` subprocess exited non-zero -- surfaces its
    stderr so a broker's integration can log/alert on a broken local
    toolchain rather than a silent empty proof."""


def generate_margin_compliance_proof(
    *,
    collected_margin: int,
    client_account_id: int,
    salt: int,
    required_margin: int,
    transaction_id_field: int,
    commitment: int,
    wasm_path: str,
    witness_generator_js_path: str,
    zkey_path: str,
) -> tuple[Groth16Proof, list[str]]:
    """Runs the same two commands as zk/scripts/generate_proof.sh
    (`generate_witness.js` then `snarkjs groth16 prove`) via subprocess,
    on whatever machine this function is called from -- the broker's,
    per this module's docstring. Returns `(proof, public_signals)` ready
    to submit as `MarginComplianceProofSubmission.proof` /
    `.public_signals` to RegEngine's `/v1/zkp/verify`.

    Private inputs (`collected_margin`, `client_account_id`, `salt`) are
    written only to a temp file that is deleted before this function
    returns, and are never logged or included in the return value."""
    circuit_input = {
        "collected_margin": str(collected_margin),
        "client_account_id": str(client_account_id),
        "salt": str(salt),
        "required_margin": str(required_margin),
        "transaction_id_field": str(transaction_id_field),
        "commitment": str(commitment),
    }

    with tempfile.TemporaryDirectory(prefix="zkp_margin_compliance_") as tmpdir:
        tmp = Path(tmpdir)
        input_path = tmp / "input.json"
        witness_path = tmp / "witness.wtns"
        proof_path = tmp / "proof.json"
        public_path = tmp / "public.json"

        input_path.write_text(json.dumps(circuit_input), encoding="utf-8")

        _run(["node", witness_generator_js_path, wasm_path, str(input_path), str(witness_path)])
        _run(["snarkjs", "groth16", "prove", zkey_path, str(witness_path), str(proof_path), str(public_path)])

        proof = Groth16Proof.model_validate(json.loads(proof_path.read_text(encoding="utf-8")))
        public_signals = json.loads(public_path.read_text(encoding="utf-8"))

    return proof, [str(s) for s in public_signals]


def _run(command: list[str]) -> None:
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise ProofGenerationError(f"Command {command!r} failed (exit {result.returncode}): {result.stderr.strip()}")
