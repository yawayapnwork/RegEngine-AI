#!/usr/bin/env bash
# One-time (per circuit version) trusted setup for zk/circuits/compliance_collateral.circom.
# Reference: PRD Addendum v2 Section 8.2 (Compliance-as-Collateral)
#
# IMPORTANT TRUSTED SETUP NOTICE:
# Groth16 is NOT a transparent (no-trusted-setup) zero-knowledge proof system.
# It requires a circuit-specific Common Reference String (CRS) generated via
# a two-phase trusted setup ceremony:
#   1. Phase 1: Universal Powers of Tau (reusable across circuits of size <= 2^k).
#   2. Phase 2: Circuit-specific ceremony generating the proving key (zkey) and
#      verification key (verification_key.json).
#
# IN PRODUCTION:
# Phase 2 MUST be executed as a multi-party computation (MPC) ceremony across
# independent participants who securely discard their toxic waste. If all
# participants collude or the secret evaluation trapdoor is exposed, an attacker
# can generate false proofs that pass verification.
#
# Requirements: circom >= 2.1.6, snarkjs, and circomlib in node_modules.
# Run OFF the RegEngine server. Ship verification_key.json to the RegEngine server
# and zkey + wasm witness generator to authorized broker infrastructure.

set -euo pipefail
cd "$(dirname "$0")/.."

CIRCUIT=circuits/compliance_collateral
BUILD=build_collateral
mkdir -p "$BUILD"

echo "== 1. Compile the circuit (R1CS + wasm witness generator) =="
circom "$CIRCUIT.circom" --r1cs --wasm --sym -l node_modules -l . -o "$BUILD"

echo "== 2. Powers of Tau (Phase 1, universal) =="
# 2^14 constraints comfortably covers batch comparators and Poseidon hash trees
snarkjs powersoftau new bn128 14 "$BUILD/pot14_0000.ptau" -v
snarkjs powersoftau contribute "$BUILD/pot14_0000.ptau" "$BUILD/pot14_0001.ptau" \
    --name="RegEngine AI compliance_collateral ceremony participant 1" -v -e="$(head -c64 /dev/urandom | xxd -p)"
snarkjs powersoftau prepare phase2 "$BUILD/pot14_0001.ptau" "$BUILD/pot14_final.ptau" -v

echo "== 3. Phase 2 (circuit-specific) zkey ceremony =="
snarkjs groth16 setup "$BUILD/compliance_collateral.r1cs" "$BUILD/pot14_final.ptau" "$BUILD/compliance_collateral_0000.zkey"
snarkjs zkey contribute "$BUILD/compliance_collateral_0000.zkey" "$BUILD/compliance_collateral_final.zkey" \
    --name="RegEngine AI compliance_collateral contributor 1" -v -e="$(head -c64 /dev/urandom | xxd -p)"

echo "== 4. Export the verification key =="
snarkjs zkey export verificationkey "$BUILD/compliance_collateral_final.zkey" "$BUILD/verification_key.json"

echo "Done. Distribution:"
echo "  - Proving package (zkey + wasm): to authorized brokers for private proof generation"
echo "  - Verification key (verification_key.json): to RegEngine server (app/zkp/verification_key_registry.py)"
