-- Tamper-evident audit ledger for RegEngine AI compliance evaluations.
--
-- PostgreSQL has no native immutable-table primitive (unlike AWS QLDB's
-- system journal), so immutability here is enforced in three independent
-- layers, on the theory that any one of them being misconfigured should
-- not silently defeat the others:
--   1. A BEFORE UPDATE/DELETE trigger that unconditionally raises.
--   2. Table privileges: the application role is granted INSERT + SELECT
--      only; UPDATE/DELETE are never granted to it (see bottom of file).
--   3. `sequence_num` is a strictly monotonic, gapless, application-assigned
--      integer (not the SERIAL id, which Postgres can gap on rollback) that
--      the hash chain itself binds each row to its predecessor — so even a
--      superuser bypassing (1) and (2) via direct DDL cannot rewrite a row
--      without invalidating every subsequent block's `current_hash`,
--      which `verify_chain` (app/ledger/verifier.py) will detect.
--
-- Mirrors the AWS QLDB pattern of "journal block -> hash -> chained digest"
-- with sequence_num standing in for QLDB's block address and current_hash
-- standing in for QLDB's block hash.

BEGIN;

CREATE TABLE IF NOT EXISTS compliance_audit_ledger (
    id                  BIGSERIAL PRIMARY KEY,

    -- Strictly monotonic chain position, assigned by the application inside
    -- the same transaction as the insert (see app/ledger/service.py). This,
    -- not `id`, is the value the hash chain is built over.
    sequence_num        BIGINT NOT NULL,

    -- --- Transaction Telemetry ---
    broker_id           TEXT NOT NULL,
    transaction_id      TEXT NOT NULL,
    evaluated_at        TIMESTAMPTZ NOT NULL,

    -- --- SEBI Circular Source Mapping ---
    circular_id         TEXT NOT NULL,
    clause_hash         TEXT NOT NULL,   -- ExtractedComplianceRule.source_sha256 (app/agents/schemas.py)
    section_reference   TEXT NOT NULL,   -- exact clause/section path, e.g. "3.2.1" or "Part A > Clause 4(b)"

    -- --- Rule Evaluation Result ---
    rule_id             TEXT NOT NULL,
    evaluation_result   TEXT NOT NULL CHECK (evaluation_result IN ('PASS', 'FAIL', 'HITL_REVIEW')),
    hitl_review_id      TEXT,            -- app.execution.models.HITLCase.case_id; required iff evaluation_result = 'HITL_REVIEW'
    CONSTRAINT hitl_review_id_required_iff_hitl_review CHECK (
        (evaluation_result = 'HITL_REVIEW' AND hitl_review_id IS NOT NULL)
        OR (evaluation_result <> 'HITL_REVIEW' AND hitl_review_id IS NULL)
    ),

    -- --- Arbitrary extra evidence (violation messages, input facts snapshot,
    -- OPA package/decision id, ...). Included in payload_digest, so any
    -- edit here is exactly as detectable as editing a first-class column. ---
    details             JSONB NOT NULL DEFAULT '{}'::jsonb,

    -- --- Hash chain ---
    payload_digest       CHAR(64) NOT NULL,   -- SHA-256(canonical_json(business fields above))
    previous_hash         CHAR(64) NOT NULL,   -- current_hash of sequence_num - 1, or 64 zeros for the genesis block
    current_hash          CHAR(64) NOT NULL,   -- SHA-256(previous_hash || payload_digest || sequence_num || evaluated_at)

    -- Ledger insertion time (server clock), distinct from the business
    -- `evaluated_at` — lets an auditor distinguish "when the compliance
    -- decision happened" from "when it was durably recorded".
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT uq_ledger_sequence UNIQUE (sequence_num)
);

-- Range queries by business time (SEBI audit requests are almost always
-- "show me everything between date X and date Y") and by transaction.
CREATE INDEX IF NOT EXISTS idx_ledger_evaluated_at ON compliance_audit_ledger (evaluated_at);
CREATE INDEX IF NOT EXISTS idx_ledger_broker_id ON compliance_audit_ledger (broker_id, evaluated_at);
CREATE INDEX IF NOT EXISTS idx_ledger_transaction_id ON compliance_audit_ledger (transaction_id);
CREATE INDEX IF NOT EXISTS idx_ledger_circular_id ON compliance_audit_ledger (circular_id);

-- --- Layer 1: reject any attempt to rewrite or remove history ---
CREATE OR REPLACE FUNCTION reject_ledger_mutation() RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION 'compliance_audit_ledger is append-only: % is not permitted (sequence_num=%)',
        TG_OP, COALESCE(OLD.sequence_num, NEW.sequence_num);
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_reject_ledger_update ON compliance_audit_ledger;
CREATE TRIGGER trg_reject_ledger_update
    BEFORE UPDATE ON compliance_audit_ledger
    FOR EACH ROW EXECUTE FUNCTION reject_ledger_mutation();

DROP TRIGGER IF EXISTS trg_reject_ledger_delete ON compliance_audit_ledger;
CREATE TRIGGER trg_reject_ledger_delete
    BEFORE DELETE ON compliance_audit_ledger
    FOR EACH ROW EXECUTE FUNCTION reject_ledger_mutation();

COMMIT;

-- --- Layer 2: least-privilege application role ---
-- Run once per environment by an operator with DDL rights, using the
-- actual role the FastAPI/Celery services connect as. INSERT + SELECT
-- only; UPDATE/DELETE/TRUNCATE are never granted.
--
--   CREATE ROLE regengine_ledger_writer LOGIN PASSWORD '...';
--   GRANT SELECT, INSERT ON compliance_audit_ledger TO regengine_ledger_writer;
--   GRANT USAGE, SELECT ON SEQUENCE compliance_audit_ledger_id_seq TO regengine_ledger_writer;
--   REVOKE UPDATE, DELETE, TRUNCATE ON compliance_audit_ledger FROM regengine_ledger_writer;
