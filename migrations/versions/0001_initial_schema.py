"""Initial schema: circulars, clauses, compiled_rules, hitl_reviews.

Revision ID: 0001_initial_schema
Revises:
Create Date: 2026-08-28
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0001_initial_schema"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "circulars",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("circular_number", sa.String(length=128), nullable=False),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("issue_date", sa.Date(), nullable=True),
        sa.Column("source_url", sa.Text(), nullable=True),
        sa.Column("department", sa.String(length=128), nullable=True),
        sa.Column("raw_text_digest", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_circulars"),
        sa.UniqueConstraint("circular_number", name="uq_circulars_circular_number"),
        sa.UniqueConstraint("raw_text_digest", name="uq_circulars_raw_text_digest"),
        # NOTE: names below omit the "ck_<table>_" prefix -- Alembic applies
        # app.db.base's naming convention (via env.py's target_metadata) to
        # op.create_table's CheckConstraints too, and that convention's "ck"
        # template consumes `name` as its %(constraint_name)s token and
        # prepends the prefix itself; including it here would double it.
        sa.CheckConstraint("length(raw_text_digest) = 64", name="raw_text_digest_len"),
    )
    op.create_index("ix_circulars_issue_date", "circulars", ["issue_date"])

    op.create_table(
        "clauses",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("circular_id", sa.BigInteger(), nullable=False),
        sa.Column("clause_number", sa.String(length=64), nullable=True),
        sa.Column("section_title", sa.Text(), nullable=True),
        sa.Column("section_path", sa.JSON(), server_default="[]", nullable=False),
        sa.Column("element_kind", sa.String(length=32), server_default="clause", nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("page_start", sa.Integer(), nullable=True),
        sa.Column("page_end", sa.Integer(), nullable=True),
        sa.Column("contains_table", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["circular_id"], ["circulars.id"], name="fk_clauses_circular_id_circulars", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_clauses"),
        sa.UniqueConstraint("circular_id", "sha256", name="uq_clauses_circular_id_sha256"),
        sa.CheckConstraint("length(sha256) = 64", name="sha256_len"),
        sa.CheckConstraint(
            "element_kind IN ('title','section_header','clause','narrative_text','table','footnote',"
            "'list_item','uncategorized')",
            name="element_kind",
        ),
    )
    op.create_index("ix_clauses_sha256", "clauses", ["sha256"])
    op.create_index("ix_clauses_circular_id_clause_number", "clauses", ["circular_id", "clause_number"])

    op.create_table(
        "compiled_rules",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("clause_id", sa.BigInteger(), nullable=False),
        sa.Column("rule_id", sa.String(length=256), nullable=False),
        sa.Column("rule_version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("rego_policy", sa.Text(), nullable=True),
        sa.Column("opa_package_name", sa.String(length=256), nullable=True),
        sa.Column("jsonlogic_ast", sa.JSON(), nullable=True),
        sa.Column("is_compiled", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("hitl_status", sa.String(length=16), server_default="NONE", nullable=False),
        sa.Column("compiler_version", sa.String(length=32), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["clause_id"], ["clauses.id"], name="fk_compiled_rules_clause_id_clauses", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_compiled_rules"),
        sa.UniqueConstraint("rule_id", "rule_version", name="uq_compiled_rules_rule_id_rule_version"),
        sa.CheckConstraint(
            "hitl_status IN ('NONE','ADVISORY','BLOCKING','RESOLVED')", name="hitl_status"
        ),
    )
    op.create_index("ix_compiled_rules_clause_id", "compiled_rules", ["clause_id"])
    op.create_index("ix_compiled_rules_hitl_status", "compiled_rules", ["hitl_status"])
    # Partial unique index: at most one active (is_active=true) version per
    # rule_id -- PostgreSQL-only syntax (postgresql_where); this is the
    # source of truth for "what OPA currently enforces" for a given rule_id.
    op.create_index(
        "uq_compiled_rules_one_active_per_rule_id",
        "compiled_rules",
        ["rule_id"],
        unique=True,
        postgresql_where=sa.text("is_active = true"),
    )

    op.create_table(
        "hitl_reviews",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("review_id", sa.String(length=64), nullable=False),
        sa.Column("clause_id", sa.BigInteger(), nullable=False),
        sa.Column("compiled_rule_id", sa.BigInteger(), nullable=True),
        sa.Column("reason_code", sa.String(length=32), nullable=False),
        sa.Column("severity", sa.String(length=16), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("source_excerpt", sa.Text(), nullable=True),
        sa.Column("field_path", sa.String(length=256), nullable=True),
        sa.Column("status", sa.String(length=16), server_default="PENDING", nullable=False),
        sa.Column("compliance_officer_id", sa.String(length=128), nullable=True),
        sa.Column("review_notes", sa.Text(), nullable=True),
        sa.Column("resolution_notes", sa.Text(), nullable=True),
        sa.Column("flagged_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["clause_id"], ["clauses.id"], name="fk_hitl_reviews_clause_id_clauses", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["compiled_rule_id"],
            ["compiled_rules.id"],
            name="fk_hitl_reviews_compiled_rule_id_compiled_rules",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_hitl_reviews"),
        sa.UniqueConstraint("review_id", name="uq_hitl_reviews_review_id"),
        sa.CheckConstraint(
            "reason_code IN ('qualitative_directive','ambiguous_span','low_extraction_confidence',"
            "'audit_not_approved','no_deterministic_logic','conflicting_thresholds','unresolved_entity')",
            name="reason_code",
        ),
        sa.CheckConstraint("severity IN ('blocking','advisory')", name="severity"),
        sa.CheckConstraint(
            "status IN ('PENDING','IN_REVIEW','RESOLVED','REJECTED')", name="status"
        ),
        sa.CheckConstraint(
            "(status IN ('RESOLVED', 'REJECTED')) = (resolved_at IS NOT NULL)",
            name="resolved_at_consistency",
        ),
    )
    op.create_index("ix_hitl_reviews_status", "hitl_reviews", ["status"])
    op.create_index("ix_hitl_reviews_clause_id", "hitl_reviews", ["clause_id"])
    op.create_index("ix_hitl_reviews_compiled_rule_id", "hitl_reviews", ["compiled_rule_id"])
    op.create_index("ix_hitl_reviews_compliance_officer_id", "hitl_reviews", ["compliance_officer_id"])


def downgrade() -> None:
    op.drop_table("hitl_reviews")
    op.drop_index("uq_compiled_rules_one_active_per_rule_id", table_name="compiled_rules")
    op.drop_table("compiled_rules")
    op.drop_table("clauses")
    op.drop_table("circulars")
