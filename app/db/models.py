"""SQLAlchemy 2.0 async ORM models for the RegEngine AI relational schema.

    circulars (1) --> (N) clauses (1) --> (N) compiled_rules (1) --> (N) hitl_reviews
                                  \\_________________________________/
                                   hitl_reviews also FKs directly to the
                                   source clause, since a review can be
                                   opened before any rule has compiled.

`app.ledger.models.compliance_audit_ledger` (the `audit_logs` table) is
defined separately as SQLAlchemy Core -- see that module's docstring for
why -- but binds to this module's `Base.metadata`, and carries nullable FK
columns back to `circulars` / `clauses` / `compiled_rules` / `hitl_reviews`
so the whole schema is one connected graph for reporting joins.

Multi-tenant partitioning
--------------------------
Every compliance table now carries a `tenant_id` foreign key to `Tenant`.
Row-Level Security (RLS) policies in PostgreSQL (see
`sql/rls_tenant_partitioning.sql`) enforce that a connected session can only
read/write rows whose `tenant_id` matches the GUC
`app.current_tenant_id` set by `app.db.tenant_session.get_tenant_db_session`.
The ORM models reflect the schema truthfully; filtering is done at the DB
layer, not by adding `.filter(tenant_id=...)` to every query.
"""
from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text as sa_text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON, BigInteger

from app.db.base import Base

# JSONB on PostgreSQL (indexable, efficient); generic JSON elsewhere (SQLite
# in tests) via SQLAlchemy's standard cross-dialect variant idiom.
_JSON_TYPE = JSON().with_variant(JSONB, "postgresql")

# SQLite's autoincrement rowid alias requires a column typed exactly
# INTEGER; production (PostgreSQL) gets a true BIGINT identity column.
_ID_TYPE = BigInteger().with_variant(Integer, "sqlite")

_ELEMENT_KINDS = (
    "title",
    "section_header",
    "clause",
    "narrative_text",
    "table",
    "footnote",
    "list_item",
    "uncategorized",
)
_HITL_REASON_CODES = (
    "qualitative_directive",
    "ambiguous_span",
    "low_extraction_confidence",
    "audit_not_approved",
    "no_deterministic_logic",
    "conflicting_thresholds",
    "unresolved_entity",
)
_HITL_SEVERITIES = ("blocking", "advisory")
_HITL_REVIEW_STATUSES = ("PENDING", "IN_REVIEW", "RESOLVED", "REJECTED")
_COMPILED_RULE_HITL_STATUSES = ("NONE", "ADVISORY", "BLOCKING", "RESOLVED")
_TENANT_TYPES = ("stockbroker", "amc", "depository", "other")


# ---------------------------------------------------------------------------
# Tenant registry
# ---------------------------------------------------------------------------

class Tenant(Base):
    """Authoritative registry of every SEBI-registered market intermediary
    that is a tenant in this deployment.

    Design notes
    ~~~~~~~~~~~~
    * `tenant_id` is a short, stable business key (e.g. ``"stockbroker_a"``,
      ``"amc_b"``) that matches the ``tenant_id`` claim in the JWT access
      token issued to that intermediary's ``Broker_API_Client`` OAuth2
      client.  It is deliberately *not* a surrogate integer so that audit
      logs and Redis keys remain human-readable.
    * ``opa_bundle_prefix`` is the OPA package namespace for this tenant's
      customised risk-overlay Rego modules, e.g. ``"tenants/stockbroker_a"``.
      The tenant-aware policy registry (app/execution/tenant_policy_registry.py)
      uses this when pushing per-tenant bundles to the OPA server.
    * ``risk_overlay`` stores structured per-tenant overrides (margin
      thresholds, exposure caps, custom rule weights) as a JSONB document.
      The sandbox evaluation endpoint reads this to simulate how a tenant's
      overlay would change a decision before promotion to production.
    * The ``sebi_baseline`` sentinel tenant is seeded by the migration and
      owns all shared SEBI master circulars (``is_shared = True`` on
      ``Circular``).  No real intermediary authenticates as it.
    """

    __tablename__ = "tenants"

    tenant_id: Mapped[str] = mapped_column(Text, primary_key=True)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    tenant_type: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="stockbroker"
    )
    sebi_reg_number: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=sa_text("true")
    )
    contact_email: Mapped[str | None] = mapped_column(Text, nullable=True)

    # OPA bundle namespace for this tenant's custom risk-overlay policies.
    # e.g. "tenants/stockbroker_a"  ->  OPA package data.tenants.stockbroker_a.*
    opa_bundle_prefix: Mapped[str] = mapped_column(Text, nullable=False)

    # Per-tenant risk overlay: margin thresholds, exposure caps, rule weights.
    # Read by the sandbox evaluator; pushed as OPA bundle data by the compiler.
    risk_overlay: Mapped[dict[str, Any]] = mapped_column(
        _JSON_TYPE, nullable=False, server_default=sa_text("'{}'")
    )

    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    # Back-references for convenient ORM traversal (admin/reporting paths only —
    # the hot evaluation path never traverses these).
    circulars: Mapped[list["Circular"]] = relationship(back_populates="tenant")
    clauses: Mapped[list["Clause"]] = relationship(back_populates="tenant")
    compiled_rules: Mapped[list["CompiledRule"]] = relationship(back_populates="tenant")
    hitl_reviews: Mapped[list["HITLReview"]] = relationship(back_populates="tenant")

    __table_args__ = (
        UniqueConstraint("sebi_reg_number", name="uq_tenants_sebi_reg_number"),
        Index("ix_tenants_tenant_type", "tenant_type"),
        Index("ix_tenants_is_active", "is_active"),
        CheckConstraint(
            f"tenant_type IN {_TENANT_TYPES!r}", name="tenant_type"
        ),
    )


# ---------------------------------------------------------------------------
# Circular
# ---------------------------------------------------------------------------

class Circular(Base):
    """Raw document metadata for one ingested SEBI circular / master circular.

    Multi-tenant notes
    ~~~~~~~~~~~~~~~~~~
    * ``tenant_id`` references ``Tenant.tenant_id``.  Shared SEBI master
      circulars are stored under the ``sebi_baseline`` sentinel tenant with
      ``is_shared = True``; the RLS SELECT policy lets every real tenant read
      them without the application explicitly joining on ``tenant_id``.
    * Tenant-specific supplementary circulars (e.g. a broker's own
      interpretive note) are stored under that tenant's ``tenant_id`` and
      are *not* visible to other tenants.
    """

    __tablename__ = "circulars"

    id: Mapped[int] = mapped_column(_ID_TYPE, primary_key=True, autoincrement=True)

    # Tenant partitioning
    tenant_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("tenants.tenant_id", ondelete="RESTRICT"),
        nullable=False,
        server_default="sebi_baseline",
    )
    # True for SEBI baseline circulars that are readable by every tenant.
    is_shared: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=sa_text("false")
    )

    # Business key: SEBI's own reference, e.g. "SEBI/HO/MRD/DP/CIR/P/2026/45".
    circular_number: Mapped[str] = mapped_column(String(128), nullable=False)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    issue_date: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    department: Mapped[str | None] = mapped_column(String(128), nullable=True)

    # SHA-256 of the full raw parsed text, ahead of chunking. Lets a re-poll
    # of an already-ingested circular short-circuit without re-parsing.
    raw_text_digest: Mapped[str] = mapped_column(String(64), nullable=False)

    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    tenant: Mapped["Tenant"] = relationship(back_populates="circulars")
    clauses: Mapped[list["Clause"]] = relationship(
        back_populates="circular", cascade="all, delete-orphan"
    )

    __table_args__ = (
        UniqueConstraint("circular_number", name="uq_circulars_circular_number"),
        UniqueConstraint("raw_text_digest", name="uq_circulars_raw_text_digest"),
        Index("ix_circulars_issue_date", "issue_date"),
        # Tenant-scoped range scan index (the dominant audit-report query shape).
        Index("ix_circulars_tenant_id", "tenant_id", "issue_date"),
        # Partial index: fast lookup of shared circulars visible to all tenants.
        Index(
            "ix_circulars_tenant_shared",
            "is_shared",
            postgresql_where=sa_text("is_shared = true"),
        ),
        CheckConstraint("length(raw_text_digest) = 64", name="raw_text_digest_len"),
    )


# ---------------------------------------------------------------------------
# Clause
# ---------------------------------------------------------------------------

class Clause(Base):
    """One layout-aware, semantically chunked clause block parsed from a circular."""

    __tablename__ = "clauses"

    id: Mapped[int] = mapped_column(_ID_TYPE, primary_key=True, autoincrement=True)
    circular_id: Mapped[int] = mapped_column(
        _ID_TYPE, ForeignKey("circulars.id", ondelete="CASCADE"), nullable=False
    )

    # Tenant partitioning — denormalised from the parent circular so RLS can
    # filter on clauses without a join (a join inside an RLS USING clause
    # creates a correlated sub-select per row; a direct column predicate is
    # evaluated once per scan node, orders of magnitude cheaper).
    tenant_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("tenants.tenant_id", ondelete="RESTRICT"),
        nullable=False,
    )

    clause_number: Mapped[str | None] = mapped_column(String(64), nullable=True)
    section_title: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Layout hierarchy tags: ordered list of ancestor section titles/numbers,
    # e.g. ["Part A", "3", "3.2", "3.2.1"] -- mirrors ClauseChunk.section_path.
    section_path: Mapped[list[str]] = mapped_column(
        _JSON_TYPE, nullable=False, server_default="[]"
    )
    element_kind: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default="clause"
    )

    text: Mapped[str] = mapped_column(Text, nullable=False)
    # SHA-256 over the clause's normalized text; the identity a compiled
    # rule and an audit-ledger entry both bind back to.
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)

    page_start: Mapped[int | None] = mapped_column(Integer, nullable=True)
    page_end: Mapped[int | None] = mapped_column(Integer, nullable=True)
    contains_table: Mapped[bool] = mapped_column(
        nullable=False, server_default=sa_text("false")
    )

    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    tenant: Mapped["Tenant"] = relationship(back_populates="clauses")
    circular: Mapped["Circular"] = relationship(back_populates="clauses")
    compiled_rules: Mapped[list["CompiledRule"]] = relationship(
        back_populates="clause", cascade="all, delete-orphan"
    )
    hitl_reviews: Mapped[list["HITLReview"]] = relationship(
        back_populates="clause", cascade="all, delete-orphan"
    )

    __table_args__ = (
        UniqueConstraint("circular_id", "sha256", name="uq_clauses_circular_id_sha256"),
        Index("ix_clauses_sha256", "sha256"),
        Index("ix_clauses_circular_id_clause_number", "circular_id", "clause_number"),
        Index("ix_clauses_tenant_id", "tenant_id", "circular_id"),
        CheckConstraint("length(sha256) = 64", name="sha256_len"),
        CheckConstraint(f"element_kind IN {_ELEMENT_KINDS!r}", name="element_kind"),
    )


# ---------------------------------------------------------------------------
# CompiledRule
# ---------------------------------------------------------------------------

class CompiledRule(Base):
    """One version of a compiled policy (Rego and/or JSON-Logic) produced
    from a clause's audited, extracted compliance rule.

    Multi-tenant notes
    ~~~~~~~~~~~~~~~~~~
    A ``tenant_id`` here means this compiled rule version is scoped to that
    tenant's risk overlay.  Two tenants may derive *different* compiled rules
    from the same source clause (different margin thresholds in their
    ``risk_overlay``), so the tuple ``(clause_id, tenant_id, rule_version)``
    is what uniquely identifies a compiled policy version rather than just
    ``(clause_id, rule_version)``.
    """

    __tablename__ = "compiled_rules"

    id: Mapped[int] = mapped_column(_ID_TYPE, primary_key=True, autoincrement=True)
    clause_id: Mapped[int] = mapped_column(
        _ID_TYPE, ForeignKey("clauses.id", ondelete="CASCADE"), nullable=False
    )

    # Tenant partitioning
    tenant_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("tenants.tenant_id", ondelete="RESTRICT"),
        nullable=False,
    )

    # Business key: stable across re-compilation of the same logical rule;
    # rule_version increments each time the source clause or compiler produces
    # a materially different output.
    rule_id: Mapped[str] = mapped_column(String(256), nullable=False)
    rule_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")

    rego_policy: Mapped[str | None] = mapped_column(Text, nullable=True)
    opa_package_name: Mapped[str | None] = mapped_column(String(256), nullable=True)
    jsonlogic_ast: Mapped[dict | None] = mapped_column(_JSON_TYPE, nullable=True)

    is_compiled: Mapped[bool] = mapped_column(nullable=False, server_default=sa_text("false"))
    # Exactly one version per (rule_id, tenant_id) may be is_active=true —
    # see the partial unique index below.
    is_active: Mapped[bool] = mapped_column(nullable=False, server_default=sa_text("false"))

    hitl_status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="NONE"
    )

    compiler_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    tenant: Mapped["Tenant"] = relationship(back_populates="compiled_rules")
    clause: Mapped["Clause"] = relationship(back_populates="compiled_rules")
    hitl_reviews: Mapped[list["HITLReview"]] = relationship(back_populates="compiled_rule")

    __table_args__ = (
        UniqueConstraint("rule_id", "rule_version", name="uq_compiled_rules_rule_id_rule_version"),
        Index("ix_compiled_rules_clause_id", "clause_id"),
        Index("ix_compiled_rules_hitl_status", "hitl_status"),
        Index("ix_compiled_rules_tenant_id", "tenant_id", "is_active"),
        # Partial unique index: at most one active version per (rule_id, tenant_id).
        # Declared here for ORM/metadata awareness; the Alembic migration carries
        # the actual `postgresql_where` DDL.
        Index(
            "uq_compiled_rules_one_active_per_rule_id",
            "rule_id",
            unique=True,
            postgresql_where=sa_text("is_active = true"),
        ),
        CheckConstraint(f"hitl_status IN {_COMPILED_RULE_HITL_STATUSES!r}", name="hitl_status"),
    )


# ---------------------------------------------------------------------------
# HITLReview
# ---------------------------------------------------------------------------

class HITLReview(Base):
    """A human-in-the-loop review case scoped to one tenant's compiled rule."""

    __tablename__ = "hitl_reviews"

    id: Mapped[int] = mapped_column(_ID_TYPE, primary_key=True, autoincrement=True)

    # Business key referenced elsewhere (e.g. app.ledger's hitl_review_id
    # column, HITLFlag.flag_id) -- a UUID string, not the surrogate id.
    review_id: Mapped[str] = mapped_column(String(64), nullable=False)

    clause_id: Mapped[int] = mapped_column(
        _ID_TYPE, ForeignKey("clauses.id", ondelete="CASCADE"), nullable=False
    )
    compiled_rule_id: Mapped[int | None] = mapped_column(
        _ID_TYPE, ForeignKey("compiled_rules.id", ondelete="SET NULL"), nullable=True
    )

    # Tenant partitioning
    tenant_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("tenants.tenant_id", ondelete="RESTRICT"),
        nullable=False,
    )

    reason_code: Mapped[str] = mapped_column(String(32), nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    source_excerpt: Mapped[str | None] = mapped_column(Text, nullable=True)
    field_path: Mapped[str | None] = mapped_column(String(256), nullable=True)

    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="PENDING"
    )
    compliance_officer_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    review_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    resolution_notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    flagged_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    resolved_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    tenant: Mapped["Tenant"] = relationship(back_populates="hitl_reviews")
    clause: Mapped["Clause"] = relationship(back_populates="hitl_reviews")
    compiled_rule: Mapped["CompiledRule | None"] = relationship(back_populates="hitl_reviews")

    __table_args__ = (
        UniqueConstraint("review_id", name="uq_hitl_reviews_review_id"),
        Index("ix_hitl_reviews_status", "status"),
        Index("ix_hitl_reviews_clause_id", "clause_id"),
        Index("ix_hitl_reviews_compiled_rule_id", "compiled_rule_id"),
        Index("ix_hitl_reviews_compliance_officer_id", "compliance_officer_id"),
        Index("ix_hitl_reviews_tenant_id", "tenant_id", "status"),
        CheckConstraint(f"reason_code IN {_HITL_REASON_CODES!r}", name="reason_code"),
        CheckConstraint(f"severity IN {_HITL_SEVERITIES!r}", name="severity"),
        CheckConstraint(f"status IN {_HITL_REVIEW_STATUSES!r}", name="status"),
        CheckConstraint(
            "(status IN ('RESOLVED', 'REJECTED')) = (resolved_at IS NOT NULL)",
            name="resolved_at_consistency",
        ),
    )


# --------------------------------------------------------------------------
# Board-level AI governance (app.governance) -- SEBI AI/ML Framework's
# named-owner, model-inventory, and kill-switch requirements.
# --------------------------------------------------------------------------

_KILL_SWITCH_SCOPES = ("global", "tenant")
_KILL_SWITCH_ACTIONS = ("activated", "deactivated", "drill")


class AgentInventory(Base):
    """Requirement 2's Named Owner & Inventory Registry: one row per
    deployed AI/ML agent this platform runs (the CrewAI Extraction
    Agent, Logic Auditor Agent, Quantitative Parsing Agent, Reference
    Resolution Agent, and the Policy Repair Agent -- see
    app.governance.inventory's seed data for the real, current roster),
    with the SEBI AI/ML Framework's mandated disclosures: which model
    weight version is running, what business function it performs,
    whether it participates in a critical/high-impact operation, and a
    NAMED human compliance officer accountable for it -- never a team
    alias or role name alone."""

    __tablename__ = "agent_inventory"

    id: Mapped[int] = mapped_column(_ID_TYPE, primary_key=True, autoincrement=True)

    agent_key: Mapped[str] = mapped_column(String(64), nullable=False)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    model_provider: Mapped[str] = mapped_column(String(64), nullable=False)
    model_weight_version: Mapped[str] = mapped_column(
        String(128), nullable=False, doc="Exact model identifier in production use, e.g. 'huggingface/Qwen/Qwen2.5-72B-Instruct'."
    )
    business_domain: Mapped[str] = mapped_column(Text, nullable=False)
    is_critical_operation: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=sa_text("false"),
        doc="SEBI AI/ML Framework disclosure: does this agent's output feed a decision with direct client/market impact (e.g. a compliance PASS/FAIL/DENY) without a mandatory human gate?",
    )

    owner_name: Mapped[str] = mapped_column(String(200), nullable=False, doc="Named individual, never a team/role alias -- SEBI's accountability requirement is personal, not organizational.")
    owner_email: Mapped[str] = mapped_column(String(320), nullable=False)
    owner_role: Mapped[str] = mapped_column(String(100), nullable=False, server_default="Compliance_Officer")

    deployed_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    last_reviewed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=sa_text("true"))
    retired_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("agent_key", name="uq_agent_inventory_agent_key"),
        Index("ix_agent_inventory_is_active", "is_active"),
        Index("ix_agent_inventory_owner_email", "owner_email"),
        CheckConstraint("(retired_at IS NOT NULL) = (is_active = false)", name="retired_at_consistency"),
    )


class KillSwitchEvent(Base):
    """Requirement 1's durable audit record: every activation,
    deactivation, and drill test of the kill switch, permanently
    persisted here regardless of what app.governance.kill_switch's
    Redis-backed LIVE state later does (Redis holds the current on/off
    state for fast per-request checks; this table is the permanent,
    queryable record a SEBI governance audit -- Requirement 3 -- reads
    from, and it must survive a Redis restart/flush intact)."""

    __tablename__ = "kill_switch_events"

    id: Mapped[int] = mapped_column(_ID_TYPE, primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(String(64), nullable=False)

    scope: Mapped[str] = mapped_column(String(16), nullable=False)
    tenant_id: Mapped[str | None] = mapped_column(
        Text, ForeignKey("tenants.tenant_id", ondelete="SET NULL"), nullable=True,
        doc="Null for scope='global'; the affected tenant for scope='tenant'.",
    )
    action: Mapped[str] = mapped_column(String(16), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    actor: Mapped[str] = mapped_column(String(200), nullable=False, doc="Principal.subject of the human (or 'system:<detector-name>' for an automated anomaly trigger) who took this action.")
    is_drill: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=sa_text("false"))
    occurred_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    details: Mapped[dict[str, Any]] = mapped_column(_JSON_TYPE, nullable=False, server_default="{}")

    __table_args__ = (
        UniqueConstraint("event_id", name="uq_kill_switch_events_event_id"),
        Index("ix_kill_switch_events_occurred_at", "occurred_at"),
        Index("ix_kill_switch_events_scope_tenant", "scope", "tenant_id"),
        Index("ix_kill_switch_events_is_drill", "is_drill"),
        CheckConstraint(f"scope IN {_KILL_SWITCH_SCOPES!r}", name="scope"),
        CheckConstraint(f"action IN {_KILL_SWITCH_ACTIONS!r}", name="action"),
        CheckConstraint("(scope = 'global') = (tenant_id IS NULL)", name="tenant_id_matches_scope"),
    )


# --------------------------------------------------------------------------
# Local (standalone) auth accounts -- app.security.local_user_store
# --------------------------------------------------------------------------

_USER_ROLES = ("Compliance_Officer", "Broker_API_Client", "System_Admin")


class User(Base):
    """A locally-provisioned human account (Compliance_Officer/System_Admin)
    that authenticates via email + password against POST /v1/auth/login
    (app.api.auth_routes) instead of an external SSO IdP. Deliberately has
    no `tenant_id` column -- app.security.models' Role docstring documents
    that human roles are never tenant-scoped (only Broker_API_Client OAuth2
    clients are, via app.security.tenant_store); a Compliance_Officer or
    System_Admin who happens to work with a specific tenant's data is
    authorized per-request via app.security.dependencies.require_tenant_scope,
    not by a fixed column on their account.

    `password_hash` is a bcrypt digest -- see app.security.local_user_store
    for the hashing/verification helpers; this model never handles a
    plaintext password itself.
    """

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(_ID_TYPE, primary_key=True, autoincrement=True)

    # Business key returned to clients (POST /v1/auth/signup's `user_id`) --
    # a UUID string, not the surrogate id, matching this schema's existing
    # convention (see HITLReview.review_id, AgentInventory.agent_key).
    user_id: Mapped[str] = mapped_column(String(64), nullable=False)

    email: Mapped[str] = mapped_column(String(320), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    roles: Mapped[list[str]] = mapped_column(_JSON_TYPE, nullable=False, default=lambda: ["Compliance_Officer"])
    disabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=sa_text("false"))
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        UniqueConstraint("user_id", name="uq_users_user_id"),
        # Case-insensitive uniqueness is enforced at the application layer
        # (app.security.local_user_store lowercases every email before a
        # read or write), so a plain unique index on the stored (already
        # lowercased) column is sufficient here.
        UniqueConstraint("email", name="uq_users_email"),
        Index("ix_users_email", "email"),
    )


# --------------------------------------------------------------------------
# Async manual-upload ingestion jobs -- app.ingestion.tasks.process_manual_upload_task
# --------------------------------------------------------------------------

_INGESTION_UPLOAD_STATUSES = ("queued", "processing", "completed", "failed")


class IngestionUploadJob(Base):
    """Tracks one manually-uploaded PDF from POST /v1/ingestion/uploads
    through to completion, so the upload request can return immediately
    (202) and the frontend can poll GET /v1/ingestion/uploads/{job_id}
    instead of blocking on one long-lived HTTP request while the actual
    parse -> index pipeline runs in a Celery worker.

    The raw PDF bytes live in object storage (app.storage.object_store),
    not in this table -- `object_key` is just the pointer the worker uses
    to fetch them. No tenant_id column, mirroring User: SEBI circulars are
    shared regulatory baseline data uploaded by a Compliance_Officer/
    System_Admin, not per-tenant content (see app.api.routes'
    `_require_ingestion_role`).
    """

    __tablename__ = "ingestion_upload_jobs"

    id: Mapped[int] = mapped_column(_ID_TYPE, primary_key=True, autoincrement=True)

    # Business key returned to clients (POST /v1/ingestion/uploads' job_id) --
    # a UUID string, not the surrogate id, matching this schema's existing
    # convention (see HITLReview.review_id, User.user_id).
    job_id: Mapped[str] = mapped_column(String(64), nullable=False)

    filename: Mapped[str] = mapped_column(Text, nullable=False)
    object_key: Mapped[str] = mapped_column(Text, nullable=False)

    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="queued")
    chunks_indexed: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_by: Mapped[str | None] = mapped_column(String(320), nullable=True)

    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("job_id", name="uq_ingestion_upload_jobs_job_id"),
        Index("ix_ingestion_upload_jobs_status", "status"),
        CheckConstraint(f"status IN {_INGESTION_UPLOAD_STATUSES!r}", name="status"),
    )
