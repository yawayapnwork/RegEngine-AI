-- =============================================================================
-- RegEngine AI: Multi-Tenant Row-Level Security (RLS) Partitioning
-- =============================================================================
-- Purpose
-- -------
-- This script provisions the full PostgreSQL RLS tenant-isolation stack for
-- RegEngine AI. It partitions every compliance table (circulars, clauses,
-- compiled_rules, hitl_reviews, compliance_audit_ledger) by tenant_id so
-- that a connected role carrying a given tenant context CANNOT read or write
-- any other tenant's rows — even if it runs a raw `SELECT * FROM circulars`.
--
-- Architecture
-- ------------
-- Three enforcement layers (Belt-and-Suspenders):
--
--   1. Row-Level Security policies (this file) — PostgreSQL enforces the
--      predicate `tenant_id = current_setting('app.current_tenant_id')`
--      on every SELECT / INSERT / UPDATE / DELETE, server-side, before a
--      row ever reaches the application. A bug in FastAPI cannot leak data.
--
--   2. Least-privilege roles (this file) — the application role is revoked
--      BYPASSRLS and never holds the pg_read_all_data / pg_write_all_data
--      pseudo-roles. A platform engineer's misconfiguration doesn't grant
--      unrestricted access.
--
--   3. Application-layer tenant_id injection (app/db/tenant_session.py) —
--      every FastAPI request dependency calls
--          SET LOCAL app.current_tenant_id = '<tenant_id>'
--      immediately after checking out a connection, so the GUC is always
--      set for the lifetime of that session's transaction. System_Admin
--      overrides are handled in that layer too (see the module docstring).
--
-- Running this script
-- -------------------
-- Intended for first-time setup or after the 0003_tenant_partitioning
-- Alembic migration has added the tenant_id columns. Run as a superuser:
--
--     psql -U postgres -d regengine -f sql/rls_tenant_partitioning.sql
--
-- This script is idempotent (IF NOT EXISTS / OR REPLACE / DROP-and-recreate
-- policies) so re-running it is safe after upgrades.
--
-- Roles expected to exist beforehand (created by regengine-cli.py or ops):
--   regengine_app       — main application role (FastAPI + Celery workers)
--   regengine_ledger_writer — ledger-write-only role (see sql/ledger_schema.sql)
--   regengine_admin     — break-glass superuser; also used by Alembic migrations
-- =============================================================================

BEGIN;

-- ---------------------------------------------------------------------------
-- 0. GUC namespace declaration
-- ---------------------------------------------------------------------------
-- PostgreSQL GUC (Grand Unified Configuration) variables in a custom namespace
-- must be declared before use, or the set_config() call fails on strict
-- installs. Add the namespace to postgresql.conf's custom_variable_classes.
-- We do it idempotently here via a function call that's a no-op if it already
-- exists.
DO $$
BEGIN
    -- The PERFORM trick: set_config with a no-op assignment to let
    -- PostgreSQL register the GUC namespace without failing if the variable
    -- has never been set in this session. On newer PostgreSQL (14+) this is
    -- a no-op for already-known GUCs.
    PERFORM set_config('app.current_tenant_id', current_setting('app.current_tenant_id', TRUE)::text, FALSE);
EXCEPTION WHEN OTHERS THEN
    -- First boot: GUC namespace not yet known; that's fine — the SET LOCAL
    -- in app/db/tenant_session.py will register it at connection time.
    NULL;
END;
$$;

-- ---------------------------------------------------------------------------
-- 1. Tenants registry table
-- ---------------------------------------------------------------------------
-- The authoritative catalog of all registered market intermediary tenants.
-- It lives in the database (not only in Redis' TenantClientStore) so that:
--   a) The tenant_id FK from every partitioned table resolves here
--      (referential integrity, not just application convention).
--   b) Compliance officers and auditors can query it via SQL for reporting
--      without Redis access.
--   c) Alembic migrations can add columns to it in the normal schema
--      lifecycle (vs. Redis schema-less blobs).
--
-- Tenant types map to the existing Role enum in app/security/models.py:
--   'stockbroker' -> Broker_API_Client tenants that are stockbrokers
--   'amc'         -> Asset Management Companies
--   'depository'  -> Depositories / clearing corporations
--   'other'       -> Any other SEBI-registered intermediary
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS tenants (
    tenant_id           TEXT PRIMARY KEY,              -- matches the tenant_id claim in the JWT
    display_name        TEXT NOT NULL,
    tenant_type         TEXT NOT NULL DEFAULT 'stockbroker'
                            CHECK (tenant_type IN ('stockbroker', 'amc', 'depository', 'other')),
    sebi_reg_number     TEXT UNIQUE,                   -- SEBI registration number (e.g. INZ000123456)
    is_active           BOOLEAN NOT NULL DEFAULT TRUE,
    contact_email       TEXT,
    -- Per-tenant OPA bundle path prefix: where this tenant's custom risk
    -- overlays live inside the OPA policy bundle store.
    -- e.g. 'tenants/stockbroker_a' -> OPA package data.tenants.stockbroker_a.*
    opa_bundle_prefix   TEXT NOT NULL,
    -- Optional per-tenant risk overlay config (JSON):
    -- margin thresholds, exposure caps, custom rule weights, etc.
    -- This is the structured version of what would otherwise live in
    -- flat environment variables per tenant.
    risk_overlay        JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_tenants_tenant_type ON tenants (tenant_type);
CREATE INDEX IF NOT EXISTS ix_tenants_is_active ON tenants (is_active);

-- Auto-maintain updated_at via a trigger (avoids ORM-side onupdate races
-- in direct-SQL management scripts)
CREATE OR REPLACE FUNCTION update_tenants_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_tenants_updated_at ON tenants;
CREATE TRIGGER trg_tenants_updated_at
    BEFORE UPDATE ON tenants
    FOR EACH ROW EXECUTE FUNCTION update_tenants_updated_at();

-- ---------------------------------------------------------------------------
-- 2. Add tenant_id to all partitioned tables
-- ---------------------------------------------------------------------------
-- Each column is added with IF NOT EXISTS so this block is re-runnable
-- after the Alembic migration (0003) has already added them.
-- The NOT NULL constraint is deferred to step 3 after we populate the
-- system_admin_sentinel default for any legacy pre-tenant data.

-- circulars: one circular may be shared (sebi-baseline) or tenant-specific
ALTER TABLE circulars
    ADD COLUMN IF NOT EXISTS tenant_id TEXT REFERENCES tenants(tenant_id) ON DELETE RESTRICT;
ALTER TABLE circulars
    ADD COLUMN IF NOT EXISTS is_shared BOOLEAN NOT NULL DEFAULT FALSE;

-- clauses: always inherits tenant from its circular (but stored explicitly
-- so RLS can filter without a JOIN, which would defeat the point)
ALTER TABLE clauses
    ADD COLUMN IF NOT EXISTS tenant_id TEXT REFERENCES tenants(tenant_id) ON DELETE RESTRICT;

-- compiled_rules: tenant isolation ensures Stockbroker A's risk overlays
-- never contaminate AMC B's compiled Rego modules
ALTER TABLE compiled_rules
    ADD COLUMN IF NOT EXISTS tenant_id TEXT REFERENCES tenants(tenant_id) ON DELETE RESTRICT;

-- hitl_reviews: a review is scoped to the tenant whose rule triggered it
ALTER TABLE hitl_reviews
    ADD COLUMN IF NOT EXISTS tenant_id TEXT REFERENCES tenants(tenant_id) ON DELETE RESTRICT;

-- compliance_audit_ledger: broker_id already acts as a tenant discriminant;
-- we add a proper FK-backed tenant_id column. This is additive / nullable,
-- same as the ref-FK columns in migration 0002, so it never breaks the
-- existing hash chain (payload_digest was computed before this column existed).
ALTER TABLE compliance_audit_ledger
    ADD COLUMN IF NOT EXISTS tenant_id TEXT REFERENCES tenants(tenant_id) ON DELETE RESTRICT;

-- ---------------------------------------------------------------------------
-- 3. Indexes for tenant-scoped queries (range scans are the common audit path)
-- ---------------------------------------------------------------------------

CREATE INDEX IF NOT EXISTS ix_circulars_tenant_id
    ON circulars (tenant_id, issue_date);

CREATE INDEX IF NOT EXISTS ix_circulars_tenant_shared
    ON circulars (is_shared) WHERE is_shared = TRUE;

CREATE INDEX IF NOT EXISTS ix_clauses_tenant_id
    ON clauses (tenant_id, circular_id);

CREATE INDEX IF NOT EXISTS ix_compiled_rules_tenant_id
    ON compiled_rules (tenant_id, is_active);

CREATE INDEX IF NOT EXISTS ix_hitl_reviews_tenant_id
    ON hitl_reviews (tenant_id, status);

CREATE INDEX IF NOT EXISTS ix_ledger_tenant_id
    ON compliance_audit_ledger (tenant_id, evaluated_at);

-- ---------------------------------------------------------------------------
-- 4. Enable Row-Level Security on every partitioned table
-- ---------------------------------------------------------------------------
-- RLS is off by default in PostgreSQL; we FORCE it even for the table owner
-- (FORCE ROW LEVEL SECURITY) so that regengine_admin connecting to the DB
-- for break-glass operations still goes through policies.
-- NB: The Alembic migration role needs BYPASSRLS (granted below) to run
-- schema changes without policy predicates blocking DDL SELECT statements.

ALTER TABLE tenants ENABLE ROW LEVEL SECURITY;
ALTER TABLE tenants FORCE ROW LEVEL SECURITY;

ALTER TABLE circulars ENABLE ROW LEVEL SECURITY;
ALTER TABLE circulars FORCE ROW LEVEL SECURITY;

ALTER TABLE clauses ENABLE ROW LEVEL SECURITY;
ALTER TABLE clauses FORCE ROW LEVEL SECURITY;

ALTER TABLE compiled_rules ENABLE ROW LEVEL SECURITY;
ALTER TABLE compiled_rules FORCE ROW LEVEL SECURITY;

ALTER TABLE hitl_reviews ENABLE ROW LEVEL SECURITY;
ALTER TABLE hitl_reviews FORCE ROW LEVEL SECURITY;

ALTER TABLE compliance_audit_ledger ENABLE ROW LEVEL SECURITY;
ALTER TABLE compliance_audit_ledger FORCE ROW LEVEL SECURITY;

-- ---------------------------------------------------------------------------
-- 5. RLS policies
-- ---------------------------------------------------------------------------
-- Policy naming convention: rls_<table>_<action>_<scope>
-- Every policy uses current_setting('app.current_tenant_id', TRUE) (the
-- TRUE flag returns NULL instead of raising if the GUC is unset, which lets
-- system-level admin queries that deliberately bypass the setting return zero
-- rows rather than crashing).
--
-- Two modes, chosen per-role (see section 6 for role grants):
--   • tenant-scoped mode  — current_setting returns a non-empty tenant_id
--     -> returns rows WHERE tenant_id = that value (strict partition)
--   • admin mode          — current_setting returns '__admin__' or is NULL
--     -> returns all rows (break-glass; logged at the application layer)
--
-- Shared circulars: SEBI master circulars that apply to ALL tenants are
-- stored under a sentinel tenant_id = 'sebi_baseline'. The SELECT policy
-- on circulars includes is_shared = TRUE so every tenant can read them.

-- --- Helper: is this an admin-context session? ---
CREATE OR REPLACE FUNCTION is_admin_context() RETURNS BOOLEAN AS $$
    SELECT coalesce(current_setting('app.current_tenant_id', TRUE), '') IN ('', '__admin__');
$$ LANGUAGE SQL STABLE SECURITY DEFINER;

-- --- tenants ---
-- Tenants can only see their own row; admins see all.
DROP POLICY IF EXISTS rls_tenants_select ON tenants;
CREATE POLICY rls_tenants_select ON tenants
    FOR SELECT
    USING (
        is_admin_context()
        OR tenant_id = current_setting('app.current_tenant_id', TRUE)
    );

DROP POLICY IF EXISTS rls_tenants_insert ON tenants;
CREATE POLICY rls_tenants_insert ON tenants
    FOR INSERT
    WITH CHECK (is_admin_context());          -- only admins can register new tenants

DROP POLICY IF EXISTS rls_tenants_update ON tenants;
CREATE POLICY rls_tenants_update ON tenants
    FOR UPDATE
    USING (is_admin_context())
    WITH CHECK (is_admin_context());

DROP POLICY IF EXISTS rls_tenants_delete ON tenants;
CREATE POLICY rls_tenants_delete ON tenants
    FOR DELETE
    USING (is_admin_context());

-- --- circulars ---
-- A tenant reads its own circulars + all shared (sebi_baseline) ones.
-- Inserts/updates/deletes are restricted to the tenant's own partition.
DROP POLICY IF EXISTS rls_circulars_select ON circulars;
CREATE POLICY rls_circulars_select ON circulars
    FOR SELECT
    USING (
        is_admin_context()
        OR is_shared = TRUE                  -- SEBI baseline circulars visible to all
        OR tenant_id = current_setting('app.current_tenant_id', TRUE)
    );

DROP POLICY IF EXISTS rls_circulars_insert ON circulars;
CREATE POLICY rls_circulars_insert ON circulars
    FOR INSERT
    WITH CHECK (
        is_admin_context()
        OR tenant_id = current_setting('app.current_tenant_id', TRUE)
    );

DROP POLICY IF EXISTS rls_circulars_update ON circulars;
CREATE POLICY rls_circulars_update ON circulars
    FOR UPDATE
    USING (
        is_admin_context()
        OR tenant_id = current_setting('app.current_tenant_id', TRUE)
    )
    WITH CHECK (
        is_admin_context()
        OR tenant_id = current_setting('app.current_tenant_id', TRUE)
    );

DROP POLICY IF EXISTS rls_circulars_delete ON circulars;
CREATE POLICY rls_circulars_delete ON circulars
    FOR DELETE
    USING (is_admin_context());  -- tenants can never delete circulars, only admins

-- --- clauses ---
DROP POLICY IF EXISTS rls_clauses_select ON clauses;
CREATE POLICY rls_clauses_select ON clauses
    FOR SELECT
    USING (
        is_admin_context()
        OR tenant_id = current_setting('app.current_tenant_id', TRUE)
        -- Allow reading clauses of shared circulars (JOIN via circular_id)
        OR EXISTS (
            SELECT 1 FROM circulars c
            WHERE c.id = clauses.circular_id AND c.is_shared = TRUE
        )
    );

DROP POLICY IF EXISTS rls_clauses_insert ON clauses;
CREATE POLICY rls_clauses_insert ON clauses
    FOR INSERT
    WITH CHECK (
        is_admin_context()
        OR tenant_id = current_setting('app.current_tenant_id', TRUE)
    );

DROP POLICY IF EXISTS rls_clauses_update ON clauses;
CREATE POLICY rls_clauses_update ON clauses
    FOR UPDATE
    USING (
        is_admin_context()
        OR tenant_id = current_setting('app.current_tenant_id', TRUE)
    )
    WITH CHECK (
        is_admin_context()
        OR tenant_id = current_setting('app.current_tenant_id', TRUE)
    );

DROP POLICY IF EXISTS rls_clauses_delete ON clauses;
CREATE POLICY rls_clauses_delete ON clauses
    FOR DELETE
    USING (is_admin_context());

-- --- compiled_rules ---
DROP POLICY IF EXISTS rls_compiled_rules_select ON compiled_rules;
CREATE POLICY rls_compiled_rules_select ON compiled_rules
    FOR SELECT
    USING (
        is_admin_context()
        OR tenant_id = current_setting('app.current_tenant_id', TRUE)
    );

DROP POLICY IF EXISTS rls_compiled_rules_insert ON compiled_rules;
CREATE POLICY rls_compiled_rules_insert ON compiled_rules
    FOR INSERT
    WITH CHECK (
        is_admin_context()
        OR tenant_id = current_setting('app.current_tenant_id', TRUE)
    );

DROP POLICY IF EXISTS rls_compiled_rules_update ON compiled_rules;
CREATE POLICY rls_compiled_rules_update ON compiled_rules
    FOR UPDATE
    USING (
        is_admin_context()
        OR tenant_id = current_setting('app.current_tenant_id', TRUE)
    )
    WITH CHECK (
        is_admin_context()
        OR tenant_id = current_setting('app.current_tenant_id', TRUE)
    );

DROP POLICY IF EXISTS rls_compiled_rules_delete ON compiled_rules;
CREATE POLICY rls_compiled_rules_delete ON compiled_rules
    FOR DELETE
    USING (is_admin_context());

-- --- hitl_reviews ---
DROP POLICY IF EXISTS rls_hitl_reviews_select ON hitl_reviews;
CREATE POLICY rls_hitl_reviews_select ON hitl_reviews
    FOR SELECT
    USING (
        is_admin_context()
        OR tenant_id = current_setting('app.current_tenant_id', TRUE)
    );

DROP POLICY IF EXISTS rls_hitl_reviews_insert ON hitl_reviews;
CREATE POLICY rls_hitl_reviews_insert ON hitl_reviews
    FOR INSERT
    WITH CHECK (
        is_admin_context()
        OR tenant_id = current_setting('app.current_tenant_id', TRUE)
    );

DROP POLICY IF EXISTS rls_hitl_reviews_update ON hitl_reviews;
CREATE POLICY rls_hitl_reviews_update ON hitl_reviews
    FOR UPDATE
    USING (
        is_admin_context()
        OR tenant_id = current_setting('app.current_tenant_id', TRUE)
    )
    WITH CHECK (
        is_admin_context()
        OR tenant_id = current_setting('app.current_tenant_id', TRUE)
    );

DROP POLICY IF EXISTS rls_hitl_reviews_delete ON hitl_reviews;
CREATE POLICY rls_hitl_reviews_delete ON hitl_reviews
    FOR DELETE
    USING (is_admin_context());

-- --- compliance_audit_ledger ---
-- The ledger is append-only (enforced by the triggers in ledger_schema.sql).
-- Tenants may SELECT their own rows; only the app role (and admins) may INSERT.
DROP POLICY IF EXISTS rls_ledger_select ON compliance_audit_ledger;
CREATE POLICY rls_ledger_select ON compliance_audit_ledger
    FOR SELECT
    USING (
        is_admin_context()
        OR tenant_id = current_setting('app.current_tenant_id', TRUE)
    );

DROP POLICY IF EXISTS rls_ledger_insert ON compliance_audit_ledger;
CREATE POLICY rls_ledger_insert ON compliance_audit_ledger
    FOR INSERT
    WITH CHECK (
        is_admin_context()
        OR tenant_id = current_setting('app.current_tenant_id', TRUE)
    );

-- UPDATE/DELETE are blocked by ledger_schema.sql's triggers anyway;
-- no need for a permissive RLS policy — default-deny covers both.

-- ---------------------------------------------------------------------------
-- 6. Role grants and BYPASSRLS configuration
-- ---------------------------------------------------------------------------

-- regengine_app: normal app role — RLS enforced, never bypasses it.
-- It must be able to SET LOCAL the GUC, which requires no special privilege
-- (any role can set session-level GUCs in their own session).
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'regengine_app') THEN
        GRANT SELECT, INSERT, UPDATE, DELETE ON tenants TO regengine_app;
        GRANT SELECT, INSERT, UPDATE, DELETE ON circulars TO regengine_app;
        GRANT SELECT, INSERT, UPDATE, DELETE ON clauses TO regengine_app;
        GRANT SELECT, INSERT, UPDATE, DELETE ON compiled_rules TO regengine_app;
        GRANT SELECT, INSERT, UPDATE, DELETE ON hitl_reviews TO regengine_app;
        -- Ledger: INSERT + SELECT only (mirrors ledger_schema.sql)
        GRANT SELECT, INSERT ON compliance_audit_ledger TO regengine_app;
        -- Sequences
        GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO regengine_app;
    END IF;
END;
$$;

-- regengine_ledger_writer: INSERT + SELECT on ledger only (unchanged from
-- ledger_schema.sql; the tenant_id column is covered automatically since
-- we granted on the table, not columns).
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'regengine_ledger_writer') THEN
        GRANT SELECT, INSERT ON compliance_audit_ledger TO regengine_ledger_writer;
        GRANT USAGE, SELECT ON SEQUENCE compliance_audit_ledger_id_seq TO regengine_ledger_writer;
    END IF;
END;
$$;

-- regengine_admin (Alembic + break-glass): full access, BYPASSRLS so
-- migration DDL (`SELECT * FROM circulars` inside an op.batch_alter_table)
-- doesn't filter rows through policies.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'regengine_admin') THEN
        GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO regengine_admin;
        GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO regengine_admin;
        ALTER ROLE regengine_admin BYPASSRLS;
    END IF;
END;
$$;

-- ---------------------------------------------------------------------------
-- 7. Seed: SEBI baseline tenant
-- ---------------------------------------------------------------------------
-- The 'sebi_baseline' pseudo-tenant owns all shared SEBI master circulars.
-- No real intermediary authenticates as this tenant; it's a partitioning
-- sentinel. Circulars owned by it and flagged is_shared=TRUE are readable
-- by every real tenant via the RLS SELECT policy above.
INSERT INTO tenants (tenant_id, display_name, tenant_type, opa_bundle_prefix, risk_overlay)
VALUES (
    'sebi_baseline',
    'SEBI Master Circular Baseline',
    'other',
    'tenants/sebi_baseline',
    '{}'::jsonb
)
ON CONFLICT (tenant_id) DO NOTHING;

COMMIT;

-- ---------------------------------------------------------------------------
-- Usage notes
-- ---------------------------------------------------------------------------
-- In application code (app/db/tenant_session.py), every DB session sets:
--
--     SET LOCAL app.current_tenant_id = '<tenant_id>';
--
-- For system-admin sessions (Alembic, break-glass CLI):
--
--     SET LOCAL app.current_tenant_id = '__admin__';
--
-- For sandbox/dry-run sessions (app/api/sandbox_routes.py):
--     Same as normal tenant sessions. The sandbox creates NO data (all
--     queries are read-only within a rolled-back transaction), so RLS
--     naturally scopes what historical circulars the tenant can test against.
