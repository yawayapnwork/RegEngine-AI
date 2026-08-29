#!/usr/bin/env bash
# One-time (per circuit version) trusted setup for zk/circuits/margin_compliance.circom.
#
# Run this OFF the RegEngine server, by whichever party the deployment
# trusts to run the ceremony (in production, a real Groth16 deployment
# runs Phase 2 as a multi-party computation across several independent
# participants -- a single-run script like this is the documented
# shape of the commands, not a substitute for that ceremony). Requires
# circom >=2.1.6 and snarkjs on PATH; neither is installed in the
# RegEngine application sandbox, since the server only ever needs the
# exported verification_key.json produced at the end -- see
# app/zkp/verification_key_registry.py.
set -euo pipefail
cd "$(dirname "$0")/.."

CIRCUIT=circuits/margin_compliance
BUILD=build
mkdir -p "$BUILD"

echo "== 1. Compile the circuit (R1CS + wasm witness generator) =="
circom "$CIRCUIT.circom" --r1cs --wasm --sym -l . -o "$BUILD"

echo "== 2. Powers of Tau (Phase 1, universal -- reusable across circuits) =="
# 2^12 constraints is comfortably above this circuit's size (one 64-bit
# comparator + one 3-input Poseidon hash); bump -p if the circuit grows.
snarkjs powersoftau new bn128 12 "$BUILD/pot12_0000.ptau" -v
snarkjs powersoftau contribute "$BUILD/pot12_0000.ptau" "$BUILD/pot12_0001.ptau" \
    --name="RegEngine AI margin_compliance setup" -v -e="$(head -c64 /dev/urandom | xxd -p)"
snarkjs powersoftau prepare phase2 "$BUILD/pot12_0001.ptau" "$BUILD/pot12_final.ptau" -v

echo "== 3. Phase 2 (circuit-specific) zkey =="
snarkjs groth16 setup "$BUILD/margin_compliance.r1cs" "$BUILD/pot12_final.ptau" "$BUILD/margin_compliance_0000.zkey"
snarkjs zkey contribute "$BUILD/margin_compliance_0000.zkey" "$BUILD/margin_compliance_final.zkey" \
    --name="RegEngine AI margin_compliance contributor" -v -e="$(head -c64 /dev/urandom | xxd -p)"

echo "== 4. Export the verification key =="
snarkjs zkey export verificationkey "$BUILD/margin_compliance_final.zkey" "$BUILD/verification_key.json"

echo "Done. Ship these to their respective owners:"
echo "  - $BUILD/margin_compliance_final.zkey + $BUILD/margin_compliance_js/  -> brokers (proof generation, see generate_proof.sh)"
echo "  - $BUILD/verification_key.json                                        -> RegEngine server (app/zkp/verification_key_registry.py)"
