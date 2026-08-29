# ADR 003: SHA-256 Append-Only Hash Chaining for Audit Log Immutability

## Status

Accepted

## Context

Every compliance evaluation RegEngine AI performs against a live broker
transaction must be provable, after the fact, to not have been altered
— to a compliance officer, to an internal auditor, and potentially to a
SEBI inspector who has no access to this platform's servers or
database credentials at all. PostgreSQL (the platform's operational
database, `app.db`) has no native immutable-table primitive comparable
to purpose-built ledger databases (e.g. AWS QLDB's system journal).
Table-level protections alone (`REVOKE UPDATE, DELETE`, a
`BEFORE UPDATE/DELETE` trigger that unconditionally raises) protect
against an application-level mistake or a compromised application
credential, but not against a database superuser, a restored backup
with different privileges, or direct DDL — any of which could, in
principle, rewrite a row without those two controls ever noticing.

## Decision

`app.ledger` implements an **append-only, sequentially hash-chained
ledger** (`compliance_audit_ledger`) with three independent, defense-in-depth
layers (documented verbatim in `sql/ledger_schema.sql`'s header):

1. A `BEFORE UPDATE/DELETE` trigger on the table that unconditionally
   raises.
2. Table privileges: the application role
   (`regengine_ledger_writer`) is granted `INSERT` + `SELECT` only —
   `UPDATE`/`DELETE` are never granted to it at all.
3. **The cryptographic layer, which survives (1) and (2) both being
   bypassed**: `sequence_num` is a strictly monotonic,
   application-assigned integer (not the `BIGSERIAL id`, which
   Postgres can gap on a rolled-back transaction), and every row's
   `current_hash` is a function of its own content AND its
   predecessor's `current_hash`:

   ```
   payload_digest = SHA256(canonical_json({broker_id, transaction_id,
       evaluated_at, circular_id, clause_hash, section_reference,
       rule_id, evaluation_result, hitl_review_id, details},
       sort_keys=True))

   current_hash = SHA256(f"{previous_hash}|{payload_digest}|
       {sequence_num}|{evaluated_at}")
   ```

   (`app.ledger.hash_chain.compute_payload_digest` /
   `compute_block_hash`). `sequence_num = 0`'s `previous_hash` is a
   fixed genesis value (`"0" * 64`). Altering ANY historical row's
   content, position, or timestamp — however it was done, including by
   a superuser bypassing every table-level protection — changes that
   row's own recomputed hash and therefore every subsequent row's
   `current_hash`, since each is chained to the one before it. This is
   independently re-derivable by anyone with just the rows themselves
   (`app.ledger.verifier.verify_chain`), with no dependency on trusting
   that (1) or (2) were never bypassed.

This is deliberately the same conceptual pattern AWS QLDB uses
internally ("journal block → hash → chained digest") with
`sequence_num` standing in for QLDB's block address — reimplemented
directly on ordinary PostgreSQL rather than adopting QLDB itself (see
Alternatives).

Two further layers build on this base, both delivered earlier in this
platform's history:

- **Independent third-party verifiability without database access**:
  `app.reporting.signing` RSA-PSS-signs an exported audit binder's
  file manifest, and a standalone, zero-server-dependency browser tool
  (`verification-portal/custody-chain.html`, "Custody Chain") ports
  this exact hash-chain formula to a hand-written, independently-tested
  WebAssembly SHA-256 implementation, so a SEBI inspector can
  re-verify a package's chain integrity and signature entirely offline,
  trusting neither RegEngine's servers nor its database.
- **Per-transaction, on-demand proof extraction**
  (`app.grievance_escalation.ledger_evidence`): fetching and
  re-verifying one specific entry's chain linkage on demand (e.g. as
  evidence attached to an automated SCORES grievance filing), without
  needing a whole-range verification pass.

## Alternatives Considered

- **Adopt a purpose-built ledger database (AWS QLDB, or an equivalent
  managed journal) instead of hash-chaining rows in ordinary
  PostgreSQL.** Rejected: QLDB (or similar) is a specific cloud
  vendor's managed service; RegEngine AI's deployment target is not
  locked to one cloud provider, and the entire value this ADR needs —
  a verifiable, sequentially-chained, tamper-evident log — is fully
  achievable on any PostgreSQL instance the platform already depends on
  for its operational schema, with no new infrastructure dependency.
  The QLDB *pattern* (chained block hashing) is adopted; the QLDB
  *product* is not.
- **A real distributed ledger / blockchain (e.g. Hyperledger Fabric).**
  Rejected: a distributed ledger's core value proposition —
  Byzantine-fault-tolerant consensus among multiple mutually
  distrusting writers — does not apply here. RegEngine AI is the sole
  writer of its own compliance audit trail; there is no second party
  whose independent agreement on each new block is required for the
  chain to be trustworthy. Adopting DLT infrastructure for a
  single-writer append log would add substantial operational
  complexity (a consensus network, node management, a second query
  language/API surface) for a property (tamper-evidence) a simple hash
  chain already provides in this single-writer setting.
- **Periodic Merkle-tree snapshotting (hash a batch of rows into one
  root, published/anchored periodically) instead of chaining every
  row sequentially.** Rejected: this platform's writes are already
  strictly, individually ordered (`sequence_num`) inside single
  transactions — a Merkle tree's main advantage (efficient proof that
  ONE leaf among many is included in a large, unordered/batched set)
  isn't needed when proving "row N is exactly where it claims to be,
  relative to row N-1" is already what a sequential chain gives for
  free, and more simply: a linear chain is trivial to explain,
  re-derive, and verify from scratch (as the standalone browser
  verifier in `verification-portal/` demonstrates) — a Merkle-batch
  scheme's periodic-root-publication step would also introduce a
  window between writes and the next published root during which
  tampering in that window has no independent external anchor yet.
- **Rely on table-level immutability (trigger + revoked privileges)
  alone, with no cryptographic chain.** Rejected as the sole mechanism
  (though retained as layers 1 and 2 above, for defense in depth): both
  protections are enforced entirely inside PostgreSQL's own permission
  model, and a party with superuser access, direct disk/WAL access, or
  a restored backup with different grants could bypass them without
  the tampering being detectable by inspecting the row alone. Only a
  content-derived, chained hash makes tampering detectable independent
  of which access-control layer was bypassed to perform it.

## Consequences

- Verifying a range of the ledger is inherently sequential
  (`verify_chain` walks forward from a genesis or trusted anchor row) —
  there is no parallel/random-access verification of an arbitrary
  single row without first establishing its predecessor's hash is
  itself correct back to a trusted anchor point. `app.grievance_escalation.ledger_evidence`'s
  single-entry proof mitigates this for the common "prove just this one
  transaction" case by fetching exactly the one predecessor row needed,
  not the whole history.
- A single corrupted or lost row invalidates the verifiability of
  every row after it in sequence (by design — this is the entire
  tamper-evidence property) — an operational incident that damages
  `compliance_audit_ledger` (e.g. a botched migration) is therefore a
  more serious event for this table than for an ordinary application
  table, and `app.ledger.models.ChainBreak` exists specifically to
  surface exactly where a chain stopped verifying.
- `payload_digest`'s field set is now a durable interface: adding or
  removing a field from what's hashed changes every future row's digest
  shape and must never be done silently — any such change is a
  breaking change to the chain's own verification formula, not merely
  a schema migration.
- The exported audit-binder format's `AuditTrailEntry` view
  (`app.analytics.models`) deliberately omits `clause_hash`/`details`
  (fields that DO feed `payload_digest`) — an external verifier working
  only from an export (like the offline "Custody Chain" browser tool)
  can therefore verify block-hash LINKAGE and re-derive `current_hash`
  from the stated `payload_digest`, but cannot independently rebuild
  `payload_digest` itself from raw fields without a richer export
  schema. This is a known, documented scope boundary of the offline
  verification story, not a defect in the chain formula itself.
