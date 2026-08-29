pragma circom 2.1.6;

// SEBI upfront-margin compliance, proved without revealing the trade.
//
// Requirement (this session's Zero-Knowledge Cryptography Architect task):
// a broker must be able to prove `collected_margin >= required_margin`
// for a specific trade WITHOUT disclosing `collected_margin` (a
// proprietary dollar figure) or the client's account identifier to
// RegEngine's server. Only two things become public: the
// `required_margin` threshold RegEngine itself computed from the
// compiled SEBI rule (so the verifier knows what was actually checked,
// not just that *some* check passed), and a Poseidon commitment binding
// the proof to one specific `transaction_id` so a proof for trade A can
// never be replayed as "evidence" for trade B.
//
// circomlib's GreaterEqThan(n) template (comparators.circom) computes
// `in[0] >= in[1]` for n-bit inputs via the standard trick: it forms
// `in[0] - in[1] + 2^n` and inspects bit n of the result -- that bit is
// 1 iff no borrow occurred, i.e. iff in[0] >= in[1]. n=64 bits is enough
// headroom for margin amounts denominated in paise (INR's smallest
// unit): 2^64 paise is ~1.8e11 INR crore, far above any single trade's
// margin requirement, while staying safely under BN254's ~254-bit field
// modulus so the +2^n shift never wraps.
include "circomlib/circuits/comparators.circom";
include "circomlib/circuits/poseidon.circom";

template MarginCompliance() {
    // --- Private inputs (never leave the broker's machine) ---
    signal input collected_margin;   // actual margin collected for this trade, in paise
    signal input client_account_id;  // client's account identifier
    signal input salt;               // randomizes the commitment so client_account_id/transaction_id can't be brute-forced from a public commitment over a small identifier space

    // --- Public inputs (submitted to RegEngine alongside the proof) ---
    signal input required_margin;      // threshold RegEngine's compiled SEBI rule demands for this trade, in paise
    signal input transaction_id_field; // the trade's transaction_id, reduced into a field element (e.g. low 253 bits of its SHA-256), binding this proof to exactly one trade so it cannot be replayed against another
    signal input commitment;           // Poseidon(client_account_id, transaction_id_field, salt) -- lets RegEngine confirm this proof concerns the client/trade pair it already knows about server-side (it never learns client_account_id itself, only that the committed value matches its own record's commitment)

    // --- 1. collected_margin >= required_margin, without revealing collected_margin ---
    component gte = GreaterEqThan(64);
    gte.in[0] <== collected_margin;
    gte.in[1] <== required_margin;
    gte.out === 1;

    // --- 2. Bind the proof to this specific client + trade ---
    component hasher = Poseidon(3);
    hasher.inputs[0] <== client_account_id;
    hasher.inputs[1] <== transaction_id_field;
    hasher.inputs[2] <== salt;
    hasher.out === commitment;
}

// Three public signals: required_margin, transaction_id_field, commitment.
// (collected_margin, client_account_id, salt stay private.)
component main {public [required_margin, transaction_id_field, commitment]} = MarginCompliance();
