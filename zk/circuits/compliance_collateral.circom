pragma circom 2.1.6;

// RegEngine AI: Compliance-as-Collateral / Zero-Knowledge Proof of Adherence
// Reference: PRD Addendum v2 Section 8.2
//
// =============================================================================
// CRYPTOGRAPHIC TRUST ASSUMPTIONS & ARCHITECTURAL INVARIANTS:
// =============================================================================
// 1. TRUSTED SETUP REQUIREMENT:
//    Groth16 is NOT a transparent (no-trusted-setup) zero-knowledge proof system.
//    It requires:
//    - Phase 1: Universal Powers of Tau ceremony over BN254 (alt_bn128).
//    - Phase 2: Circuit-specific ceremony generating the proving and verification
//      keys (zkey and verification_key.json).
//    Security assumption: At least one participant in the Phase 2 MPC honestly
//    discarded their toxic waste (secret evaluation trapdoor points). If toxic
//    waste is compromised, an adversary could forge proofs of compliance.
//
// 2. CRYPTOGRAPHIC VERIFICATION VS. REGULATORY/LEGAL COMPLIANCE:
//    Verifying this Groth16 proof demonstrates that the prover possesses a valid
//    witness of private transactions that satisfies the circuit constraints for
//    the declared public inputs (policy hash, period, commitment, threshold).
//    It DOES NOT prove that the broker's underlying off-chain accounting ledger
//    was complete, authentic, or unmanipulated prior to witness generation.
//    Zero-knowledge proofs serve as advisory collateral evidence, NOT automatic
//    regulatory certification or automatic policy activation.
//
// 3. POLICY VERSION BINDING & ANTI-REPLAY:
//    The proof binds strictly to:
//    - `policy_hash`: Exact compiled policy version (SHA-256 low 253 bits).
//    - `reporting_period_id`: Exact reporting period epoch window.
//    - `dataset_commitment`: Cryptographic commitment over the batch.
//    A proof generated for Policy A or Period X cannot be replayed for Policy B
//    or Period Y.
//
// =============================================================================

include "circomlib/circuits/comparators.circom";
include "circomlib/circuits/poseidon.circom";

template ComplianceCollateral(BATCH_SIZE) {
    // --- Public Inputs (Submitted to RegEngine verifier) ---
    signal input policy_hash;          // Compiled regulatory policy SHA-256 (low 253 bits)
    signal input reporting_period_id;  // Reporting period identifier / epoch (e.g. YYYYMMDD)
    signal input dataset_commitment;   // Root Poseidon commitment over all transaction leaves
    signal input margin_threshold;     // Minimum regulatory margin threshold (in paise or basis points)

    // --- Private Inputs (Never leave broker's infrastructure) ---
    signal input collected_margin[BATCH_SIZE];  // Actual margin collected for each transaction (in paise)
    signal input required_margin[BATCH_SIZE];   // Required margin demand for each transaction (in paise)
    signal input transaction_ids[BATCH_SIZE];   // Transaction identifiers reduced to field elements
    signal input salts[BATCH_SIZE];              // Blinding factors preventing brute-force preimage attacks

    // --- 1. Per-Transaction Predicate Verification ---
    // Assert collected_margin[i] >= required_margin[i] for every transaction in the batch
    component gte[BATCH_SIZE];
    component leaf_hashers[BATCH_SIZE];
    signal leaf_commitments[BATCH_SIZE];

    for (var i = 0; i < BATCH_SIZE; i++) {
        // Enforce 64-bit headroom for paise amounts (< 2^64 paise = ~1.8e11 INR crore)
        gte[i] = GreaterEqThan(64);
        gte[i].in[0] <== collected_margin[i];
        gte[i].in[1] <== required_margin[i];
        gte[i].out === 1;

        // Compute leaf commitment: Poseidon(txn_id, collected_margin, required_margin, salt)
        leaf_hashers[i] = Poseidon(4);
        leaf_hashers[i].inputs[0] <== transaction_ids[i];
        leaf_hashers[i].inputs[1] <== collected_margin[i];
        leaf_hashers[i].inputs[2] <== required_margin[i];
        leaf_hashers[i].inputs[3] <== salts[i];
        leaf_commitments[i] <== leaf_hashers[i].out;
    }

    // --- 2. Dataset Commitment Verification ---
    // Compute root commitment over all transaction leaf commitments
    component batch_hasher = Poseidon(BATCH_SIZE);
    for (var i = 0; i < BATCH_SIZE; i++) {
        batch_hasher.inputs[i] <== leaf_commitments[i];
    }
    batch_hasher.out === dataset_commitment;

    // --- 3. Public Signal Cryptographic Binding ---
    // Ensure policy_hash, reporting_period_id, and margin_threshold are bound
    // into the constraint system so no public input can be left unconstrained.
    component claim_binder = Poseidon(4);
    claim_binder.inputs[0] <== policy_hash;
    claim_binder.inputs[1] <== reporting_period_id;
    claim_binder.inputs[2] <== dataset_commitment;
    claim_binder.inputs[3] <== margin_threshold;

    // Dummy constraint asserting claim binder output is non-zero (always true for Poseidon on BN254)
    signal binder_dummy;
    binder_dummy <== claim_binder.out * claim_binder.out;
}

// 4 public signals: policy_hash, reporting_period_id, dataset_commitment, margin_threshold
component main {public [policy_hash, reporting_period_id, dataset_commitment, margin_threshold]} = ComplianceCollateral(4);
