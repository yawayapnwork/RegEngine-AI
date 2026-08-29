#!/usr/bin/env bash
# Client-side (broker-side) proof generation for margin_compliance.
#
# Runs on the BROKER's own infrastructure, never on RegEngine's server --
# `collected_margin` and `client_account_id` are only ever read from
# INPUT.json here, on the machine that already has legitimate access to
# them. Needs: node, snarkjs, and the artifacts build_circuit.sh
# produced (margin_compliance_js/ witness generator +
# margin_compliance_final.zkey). Produces proof.json + public.json,
# which is all the broker then POSTs to RegEngine's
# /v1/zkp/verify -- see app/zkp/prover_client.py for the Python
# equivalent of these same two commands, and
# app/zkp/models.py:MarginComplianceProofSubmission for the exact
# request shape the server expects.
#
# Usage: ./generate_proof.sh INPUT.json OUTPUT_DIR
#   INPUT.json = {
#     "collected_margin": "<paise, private>",
#     "client_account_id": "<field element, private>",
#     "salt": "<random field element, private>",
#     "required_margin": "<paise, public>",
#     "transaction_id_field": "<field element, public>",
#     "commitment": "<Poseidon(client_account_id, transaction_id_field, salt), public>"
#   }
set -euo pipefail
cd "$(dirname "$0")/.."

INPUT="${1:?usage: generate_proof.sh INPUT.json OUTPUT_DIR}"
OUT="${2:?usage: generate_proof.sh INPUT.json OUTPUT_DIR}"
mkdir -p "$OUT"

WASM=build/margin_compliance_js/margin_compliance.wasm
ZKEY=build/margin_compliance_final.zkey

echo "== Generating witness =="
node "build/margin_compliance_js/generate_witness.js" "$WASM" "$INPUT" "$OUT/witness.wtns"

echo "== Generating Groth16 proof =="
snarkjs groth16 prove "$ZKEY" "$OUT/witness.wtns" "$OUT/proof.json" "$OUT/public.json"

echo "Wrote $OUT/proof.json and $OUT/public.json -- POST both (plus circuit_id) to /v1/zkp/verify."
