// UI-only stand-in for app.ledger.hash_chain (SHA-256). A short, fast,
// deterministic string hash is enough to make the live-feed demo and the
// "verify chain integrity" button internally consistent in the browser;
// production verification is the real SHA-256 chain the backend computes
// in app/ledger/verifier.py, not this function.
export function mockHash(input) {
  let h1 = 0xdeadbeef;
  let h2 = 0x41c6ce57;
  for (let i = 0; i < input.length; i++) {
    const ch = input.charCodeAt(i);
    h1 = Math.imul(h1 ^ ch, 2654435761);
    h2 = Math.imul(h2 ^ ch, 1597334677);
  }
  h1 =
    Math.imul(h1 ^ (h1 >>> 16), 2246822507) ^
    Math.imul(h2 ^ (h2 >>> 13), 3266489909);
  h2 =
    Math.imul(h2 ^ (h2 >>> 16), 2246822507) ^
    Math.imul(h1 ^ (h1 >>> 13), 3266489909);
  return (
    (h1 >>> 0).toString(16).padStart(8, "0") +
    (h2 >>> 0).toString(16).padStart(8, "0")
  );
}

const MOCK_BROKERS = ["BRK-00294", "BRK-00512", "BRK-00107", "BRK-00871"];
const MOCK_CLAUSES = [
  {
    clauseHash: "9f2a6c1e4b7d0853f1a9c6e2b4d7f108",
    section: "3.2.1",
    ruleId: "9f2a6c1e4b7d0853f1a9c6e2b4d7f108:3.2.1",
  },
  {
    clauseHash: "5c7d21f0e8b4913ac0d6f2b8a7c4e905",
    section: "2.4.3",
    ruleId: "5c7d21f0e8b4913ac0d6f2b8a7c4e905:2.4.3",
  },
];
const OUTCOME_WEIGHTS = ["PASS", "PASS", "PASS", "FAIL", "HITL_REVIEW"];

export function generateNextEntry(previous) {
  const clause = MOCK_CLAUSES[Math.floor(Math.random() * MOCK_CLAUSES.length)];
  const evaluationResult =
    OUTCOME_WEIGHTS[Math.floor(Math.random() * OUTCOME_WEIGHTS.length)];
  const sequenceNum = previous.sequenceNum + 1;
  const evaluatedAt = new Date().toISOString();
  const payload = JSON.stringify({
    clause,
    evaluationResult,
    sequenceNum,
    evaluatedAt,
  });
  const previousHash = previous.currentHash;
  const currentHash = mockHash(previousHash + mockHash(payload) + sequenceNum);

  return {
    sequenceNum,
    transactionId: `TXN-${88221190 + sequenceNum}`,
    brokerId: MOCK_BROKERS[Math.floor(Math.random() * MOCK_BROKERS.length)],
    evaluatedAt,
    circularId: "SEBI/HO/MIRSD/2026/01",
    clauseHash: clause.clauseHash,
    sectionReference: clause.section,
    ruleId: clause.ruleId,
    evaluationResult,
    hitlReviewId:
      evaluationResult === "HITL_REVIEW"
        ? `hitl-${7700 + (sequenceNum % 300)}`
        : undefined,
    previousHash,
    currentHash,
    _payload: payload, // retained only so verifyFeed() can recompute; not part of the real ledger row
  };
}

/** Mirrors app.ledger.verifier.verify_chain's shape of result, computed
 * client-side over the in-memory feed (newest-first) for the demo. */
export function verifyFeed(feedNewestFirst) {
  const chronological = [...feedNewestFirst].reverse();
  const breaks = [];

  for (let i = 1; i < chronological.length; i++) {
    // i=0 is skipped deliberately: this in-browser feed is a slice of a much
    // longer chain, not the genesis block, so (as in the real
    // verify_chain) the first visible row's previous_hash is treated as
    // an already-trusted anchor rather than compared against a fabricated
    // "start of history" value.
    const entry = chronological[i];
    const prev = chronological[i - 1];
    if (entry.previousHash !== prev.currentHash) {
      breaks.push({
        sequenceNum: entry.sequenceNum,
        reason: "previous_hash does not match the prior row's current_hash",
      });
    }
    if (entry._payload) {
      const recomputed = mockHash(
        entry.previousHash + mockHash(entry._payload) + entry.sequenceNum,
      );
      if (recomputed !== entry.currentHash) {
        breaks.push({
          sequenceNum: entry.sequenceNum,
          reason: "current_hash does not match recomputed block hash",
        });
      }
    }
  }

  return {
    valid: breaks.length === 0,
    entriesChecked: chronological.length,
    breaks,
  };
}
