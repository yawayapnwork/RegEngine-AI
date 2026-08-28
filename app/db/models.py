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
"""
from __future__ import annotations

import datetime as dt

from sqlalchemy import (
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
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON, BigInteger

from app.db.base import Base

# JSONB on PostgreSQL (indexable, efficient); generic JSON elsewhere (SQLite
# in tests) via SQLAlchemy's standard cross-dialect variant idiom -- mirrors
# app/ledger/models.py's _JSON_TYPE.
_JSON_TYPE = JSON().with_variant(JSONB, "postgresql")

# SQLite's autoincrement rowid alias requires a column typed exactly
# INTEGER; production (PostgreSQL) gets a true BIGINT identity column.
# Mirrors app/ledger/models.py's _ID_TYPE.
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


class Circular(Base):
    """Raw document metadata for one ingested SEBI circular / master circular."""

    __tablename__ = "circulars"

    id: Mapped[int] = mapped_column(_ID_TYPE, primary_key=True, autoincrement=True)

    # Business key: SEBI's own reference, e.g. "SEBI/HO/MRD/DP/CIR/P/2026/45".
    circular_number: Mapped[str] = mapped_column(String(128), nullable=False)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    issue_date: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    department: Mapped[str | None] = mapped_column(String(128), nullable=True)

    # SHA-256 of the full raw parsed text, ahead of chunking. Lets a re-poll
    # of an already-ingested circular short-circuit without re-parsing.
    raw_text_digest: Mapped[str] = mapped_column(String(64), nullable=False)

    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    clauses: Mapped[list["Clause"]] = relationship(back_populates="circular", cascade="all, delete-orphan")

    __table_args__ = (
        UniqueConstraint("circular_number", name="uq_circulars_circular_number"),
        UniqueConstraint("raw_text_digest", name="uq_circulars_raw_text_digest"),
        Index("ix_circulars_issue_date", "issue_date"),
        # NOTE: CheckConstraint names are the input to the "ck" naming
        # convention's %(constraint_name)s token (see app/db/base.py), so
        # these are given WITHOUT the "ck_<table>_" prefix -- the convention
        # adds it. A name that already included the prefix would be doubled.
        CheckConstraint("length(raw_text_digest) = 64", name="raw_text_digest_len"),
    )


class Clause(Base):
    """One layout-aware, semantically chunked clause block parsed from a circular."""

    __tablename__ = "clauses"

    id: Mapped[int] = mapped_column(_ID_TYPE, primary_key=True, autoincrement=True)
    circular_id: Mapped[int] = mapped_column(
        _ID_TYPE, ForeignKey("circulars.id", ondelete="CASCADE"), nullable=False
    )

    clause_number: Mapped[str | None] = mapped_column(String(64), nullable=True)
    section_title: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Layout hierarchy tags: ordered list of ancestor section titles/numbers,
    # e.g. ["Part A", "3", "3.2", "3.2.1"] -- mirrors ClauseChunk.section_path.
    section_path: Mapped[list[str]] = mapped_column(_JSON_TYPE, nullable=False, server_default="[]")
    element_kind: Mapped[str] = mapped_column(String(32), nullable=False, server_default="clause")

    text: Mapped[str] = mapped_column(Text, nullable=False)
    # SHA-256 over the clause's normalized text; the identity a compiled
    # rule and an audit-ledger entry both bind back to as "the exact source
    # text that produced this decision" (ExtractedComplianceRule.source_sha256).
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)

    page_start: Mapped[int | None] = mapped_column(Integer, nullable=True)
    page_end: Mapped[int | None] = mapped_column(Integer, nullable=True)
    contains_table: Mapped[bool] = mapped_column(nullable=False, server_default="false")

    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    circular: Mapped["Circular"] = relationship(back_populates="clauses")
    compiled_rules: Mapped[list["CompiledRule"]] = relationship(back_populates="clause", cascade="all, delete-orphan")
    hitl_reviews: Mapped[list["HITLReview"]] = relationship(back_populates="clause", cascade="all, delete-orphan")

    __table_args__ = (
        # A given clause's exact text should appear at most once per circular
        # (re-parsing an unchanged clause must upsert, not duplicate).
        UniqueConstraint("circular_id", "sha256", name="uq_clauses_circular_id_sha256"),
        Index("ix_clauses_sha256", "sha256"),
        Index("ix_clauses_circular_id_clause_number", "circular_id", "clause_number"),
        CheckConstraint("length(sha256) = 64", name="sha256_len"),
        CheckConstraint(f"element_kind IN {_ELEMENT_KINDS!r}", name="element_kind"),
    )


class CompiledRule(Base):
    """One version of a compiled policy (Rego and/or JSON-Logic) produced
    from a clause's audited, extracted compliance rule."""

    __tablename__ = "compiled_rules"

    id: Mapped[int] = mapped_column(_ID_TYPE, primary_key=True, autoincrement=True)
    clause_id: Mapped[int] = mapped_column(_ID_TYPE, ForeignKey("clauses.id", ondelete="CASCADE"), nullable=False)

    # Business key: stable across re-compilation of the same logical rule;
    # rule_version increments each time the source clause or compiler
    # produces a materially different output (app/ledger's rule_id column
    # references this, not the surrogate id, for that reason).
    rule_id: Mapped[str] = mapped_column(String(256), nullable=False)
    rule_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")

    rego_policy: Mapped[str | None] = mapped_column(Text, nullable=True)
    opa_package_name: Mapped[str | None] = mapped_column(String(256), nullable=True)
    jsonlogic_ast: Mapped[dict | None] = mapped_column(_JSON_TYPE, nullable=True)

    is_compiled: Mapped[bool] = mapped_column(nullable=False, server_default="false")
    # Exactly one version per rule_id may be is_active=true -- see the
    # partial unique index below -- representing "what execution/OPA
    # currently enforces" versus superseded/historical versions.
    is_active: Mapped[bool] = mapped_column(nullable=False, server_default="false")

    # Coarse HITL gate for this compiled version: NONE (clean compile),
    # ADVISORY/BLOCKING (see hitl_reviews for the individual flags),
    # RESOLVED (was BLOCKING, all blocking flags since resolved).
    hitl_status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="NONE")

    compiler_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    clause: Mapped["Clause"] = relationship(back_populates="compiled_rules")
    hitl_reviews: Mapped[list["HITLReview"]] = relationship(back_populates="compiled_rule")

    __table_args__ = (
        UniqueConstraint("rule_id", "rule_version", name="uq_compiled_rules_rule_id_rule_version"),
        Index("ix_compiled_rules_clause_id", "clause_id"),
        Index("ix_compiled_rules_hitl_status", "hitl_status"),
        # Partial unique index: at most one active version per rule_id.
        # Declared here for ORM/metadata awareness; see the Alembic
        # migration for the actual `postgresql_where` DDL (SQLite has no
        # partial-index equivalent used elsewhere in this codebase either).
        Index(
            "uq_compiled_rules_one_active_per_rule_id",
            "rule_id",
            unique=True,
            postgresql_where=text("is_active = true"),
        ),
        CheckConstraint(f"hitl_status IN {_COMPILED_RULE_HITL_STATUSES!r}", name="hitl_status"),
    )


class HITLReview(Base):
    """A human-in-the-loop review case: a clause (and, once compiled, a
    specific compiled-rule version) that could not be resolved
    deterministically and was routed to a compliance officer."""

    __tablename__ = "hitl_reviews"

    id: Mapped[int] = mapped_column(_ID_TYPE, primary_key=True, autoincrement=True)

    # Business key referenced elsewhere (e.g. app.ledger's hitl_review_id
    # column, HITLFlag.flag_id) -- a UUID string, not the surrogate id.
    review_id: Mapped[str] = mapped_column(String(64), nullable=False)

    clause_id: Mapped[int] = mapped_column(_ID_TYPE, ForeignKey("clauses.id", ondelete="CASCADE"), nullable=False)
    compiled_rule_id: Mapped[int | None] = mapped_column(
        _ID_TYPE, ForeignKey("compiled_rules.id", ondelete="SET NULL"), nullable=True
    )

    reason_code: Mapped[str] = mapped_column(String(32), nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    source_excerpt: Mapped[str | None] = mapped_column(Text, nullable=True)
    field_path: Mapped[str | None] = mapped_column(String(256), nullable=True)

    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="PENDING")
    compliance_officer_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    review_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    resolution_notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    flagged_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    resolved_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    clause: Mapped["Clause"] = relationship(back_populates="hitl_reviews")
    compiled_rule: Mapped["CompiledRule | None"] = relationship(back_populates="hitl_reviews")

    __table_args__ = (
        UniqueConstraint("review_id", name="uq_hitl_reviews_review_id"),
        Index("ix_hitl_reviews_status", "status"),
        Index("ix_hitl_reviews_clause_id", "clause_id"),
        Index("ix_hitl_reviews_compiled_rule_id", "compiled_rule_id"),
        Index("ix_hitl_reviews_compliance_officer_id", "compliance_officer_id"),
        CheckConstraint(f"reason_code IN {_HITL_REASON_CODES!r}", name="reason_code"),
        CheckConstraint(f"severity IN {_HITL_SEVERITIES!r}", name="severity"),
        CheckConstraint(f"status IN {_HITL_REVIEW_STATUSES!r}", name="status"),
        CheckConstraint(
            "(status IN ('RESOLVED', 'REJECTED')) = (resolved_at IS NOT NULL)",
            name="resolved_at_consistency",
        ),
    )
