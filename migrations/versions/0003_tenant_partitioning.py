"""Add multi-tenant partitioning: tenants table, tenant_id columns on all
compliance tables, composite indexes, and RLS activation.

This migration covers the *Alembic-managed* DDL side of tenant partitioning:
  - New `tenants` table (the authoritative registry of market intermediaries)
  - `tenant_id` + `is_shared` columns on circulars, clauses, compiled_rules,
    hitl_reviews, and compliance_audit_ledger
  - Composite indexes for tenant-scoped range queries
  - Enabling RLS and creating policies is deliberately kept in
    `sql/rls_tenant_partitioning.sql` (run by an operator with superuser
    rights) rather than here, because:
      a) Alembic's `regengine_admin` role needs BYPASSRLS to run migrations
         without being filtered by the policies it is about to create; doing
         both in the same script creates a chicken-and-egg problem.
      b) CREATE POLICY / ALTER TABLE ... ENABLE ROW LEVEL SECURITY require
         table ownership or superuser; separating them makes the privilege
         requirements explicit and auditable.
  - `sebi_baseline` sentinel tenant seed is also in the SQL script.

Revision ID: 0003_tenant_partitioning
Revises: 0002_ledger_fk_columns
Create Date: 2026-08-28
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0003_tenant_partitioning"
down_revision: Union[str, None] = "0002_ledger_fk_columns"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Tables that receive a tenant_id FK column (main schema)
_PARTITIONED_TABLES = ("circulars", "clauses", "compiled_rules", "hitl_reviews")
# Ledger is handled separately because it has special append-only semantics
_LEDGER_TABLE = "compliance_audit_ledger"
_TENANTS_TABLE = "tenants"


def upgrade() -> None:
    # ------------------------------------------------------------------
    # 1. tenants registry table
    # ------------------------------------------------------------------
    op.create_table(
        _TENANTS_TABLE,
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column(
            "tenant_type",
            sa.Text(),
            nullable=False,
            server_default="stockbroker",
        ),
        sa.Column("sebi_reg_number", sa.Text(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("contact_email", sa.Text(), nullable=True),
        sa.Column("opa_bundle_prefix", sa.Text(), nullable=False),
        sa.Column(
            "risk_overlay",
            sa.JSON(),
            nullable=False,
            server_default="{}",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("tenant_id", name="pk_tenants"),
        sa.UniqueConstraint("sebi_reg_number", name="uq_tenants_sebi_reg_number"),
        sa.CheckConstraint(
            "tenant_type IN ('stockbroker','amc','depository','other')",
            name="tenant_type",
        ),
    )
    op.create_index("ix_tenants_tenant_type", _TENANTS_TABLE, ["tenant_type"])
    op.create_index("ix_tenants_is_active", _TENANTS_TABLE, ["is_active"])

    # ------------------------------------------------------------------
    # 2. Seed the sebi_baseline sentinel tenant
    # ------------------------------------------------------------------
    # Done in the migration (not just the SQL script) so that CI test runs
    # that use Alembic but not the SQL script still have the sentinel row.
    op.execute(
        sa.text(
            """
            INSERT INTO tenants
                (tenant_id, display_name, tenant_type, opa_bundle_prefix, risk_overlay)
            VALUES
                ('sebi_baseline',
                 'SEBI Master Circular Baseline',
                 'other',
                 'tenants/sebi_baseline',
                 '{}'::jsonb)
            ON CONFLICT (tenant_id) DO NOTHING
            """
        )
    )

    # ------------------------------------------------------------------
    # 3. Add tenant_id + is_shared to circulars
    # ------------------------------------------------------------------
    op.add_column(
        "circulars",
        sa.Column(
            "tenant_id",
            sa.Text(),
            sa.ForeignKey("tenants.tenant_id", ondelete="RESTRICT", name="fk_circulars_tenant_id_tenants"),
            nullable=True,  # nullable until backfilled; NOT NULL enforced after
        ),
    )
    op.add_column(
        "circulars",
        sa.Column("is_shared", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    # Backfill existing circulars to the baseline tenant
    op.execute(
        sa.text("UPDATE circulars SET tenant_id = 'sebi_baseline', is_shared = true WHERE tenant_id IS NULL")
    )
    op.alter_column("circulars", "tenant_id", nullable=False)
    op.create_index("ix_circulars_tenant_id", "circulars", ["tenant_id", "issue_date"])
    op.create_index(
        "ix_circulars_tenant_shared",
        "circulars",
        ["is_shared"],
        postgresql_where=sa.text("is_shared = true"),
    )

    # ------------------------------------------------------------------
    # 4. Add tenant_id to clauses
    # ------------------------------------------------------------------
    op.add_column(
        "clauses",
        sa.Column(
            "tenant_id",
            sa.Text(),
            sa.ForeignKey("tenants.tenant_id", ondelete="RESTRICT", name="fk_clauses_tenant_id_tenants"),
            nullable=True,
        ),
    )
    # Backfill: clauses inherit their circular's tenant
    op.execute(
        sa.text(
            """
            UPDATE clauses
            SET    tenant_id = c.tenant_id
            FROM   circulars c
            WHERE  c.id = clauses.circular_id
              AND  clauses.tenant_id IS NULL
            """
        )
    )
    op.alter_column("clauses", "tenant_id", nullable=False)
    op.create_index("ix_clauses_tenant_id", "clauses", ["tenant_id", "circular_id"])

    # ------------------------------------------------------------------
    # 5. Add tenant_id to compiled_rules
    # ------------------------------------------------------------------
    op.add_column(
        "compiled_rules",
        sa.Column(
            "tenant_id",
            sa.Text(),
            sa.ForeignKey("tenants.tenant_id", ondelete="RESTRICT", name="fk_compiled_rules_tenant_id_tenants"),
            nullable=True,
        ),
    )
    # Backfill: compiled_rules inherit from their clause -> circular chain
    op.execute(
        sa.text(
            """
            UPDATE compiled_rules
            SET    tenant_id = cl.tenant_id
            FROM   clauses cl
            WHERE  cl.id = compiled_rules.clause_id
              AND  compiled_rules.tenant_id IS NULL
            """
        )
    )
    op.alter_column("compiled_rules", "tenant_id", nullable=False)
    op.create_index(
        "ix_compiled_rules_tenant_id",
        "compiled_rules",
        ["tenant_id", "is_active"],
    )

    # ------------------------------------------------------------------
    # 6. Add tenant_id to hitl_reviews
    # ------------------------------------------------------------------
    op.add_column(
        "hitl_reviews",
        sa.Column(
            "tenant_id",
            sa.Text(),
            sa.ForeignKey("tenants.tenant_id", ondelete="RESTRICT", name="fk_hitl_reviews_tenant_id_tenants"),
            nullable=True,
        ),
    )
    op.execute(
        sa.text(
            """
            UPDATE hitl_reviews
            SET    tenant_id = cl.tenant_id
            FROM   clauses cl
            WHERE  cl.id = hitl_reviews.clause_id
              AND  hitl_reviews.tenant_id IS NULL
            """
        )
    )
    op.alter_column("hitl_reviews", "tenant_id", nullable=False)
    op.create_index("ix_hitl_reviews_tenant_id", "hitl_reviews", ["tenant_id", "status"])

    # ------------------------------------------------------------------
    # 7. Add tenant_id to compliance_audit_ledger (additive / nullable)
    # ------------------------------------------------------------------
    # Kept nullable (same as the ref-FK columns added in 0002) so adding
    # it never invalidates any row's payload_digest / hash chain. Existing
    # rows will have NULL until an operator backfill script maps broker_id
    # to tenant_id; new rows are always written with the tenant's id by
    # app/ledger/service.py via app/db/tenant_session.py context.
    op.add_column(
        _LEDGER_TABLE,
        sa.Column(
            "tenant_id",
            sa.Text(),
            sa.ForeignKey(
                "tenants.tenant_id",
                ondelete="RESTRICT",
                name="fk_compliance_audit_ledger_tenant_id_tenants",
            ),
            nullable=True,
        ),
    )
    op.create_index("ix_ledger_tenant_id", _LEDGER_TABLE, ["tenant_id", "evaluated_at"])


def downgrade() -> None:
    # Reverse in dependency order (ledger -> hitl_reviews -> ... -> tenants)

    op.drop_index("ix_ledger_tenant_id", table_name=_LEDGER_TABLE)
    op.drop_constraint(
        "fk_compliance_audit_ledger_tenant_id_tenants", _LEDGER_TABLE, type_="foreignkey"
    )
    op.drop_column(_LEDGER_TABLE, "tenant_id")

    op.drop_index("ix_hitl_reviews_tenant_id", table_name="hitl_reviews")
    op.drop_constraint("fk_hitl_reviews_tenant_id_tenants", "hitl_reviews", type_="foreignkey")
    op.drop_column("hitl_reviews", "tenant_id")

    op.drop_index("ix_compiled_rules_tenant_id", table_name="compiled_rules")
    op.drop_constraint("fk_compiled_rules_tenant_id_tenants", "compiled_rules", type_="foreignkey")
    op.drop_column("compiled_rules", "tenant_id")

    op.drop_index("ix_clauses_tenant_id", table_name="clauses")
    op.drop_constraint("fk_clauses_tenant_id_tenants", "clauses", type_="foreignkey")
    op.drop_column("clauses", "tenant_id")

    op.drop_index("ix_circulars_tenant_shared", table_name="circulars")
    op.drop_index("ix_circulars_tenant_id", table_name="circulars")
    op.drop_constraint("fk_circulars_tenant_id_tenants", "circulars", type_="foreignkey")
    op.drop_column("circulars", "is_shared")
    op.drop_column("circulars", "tenant_id")

    op.drop_index("ix_tenants_is_active", table_name=_TENANTS_TABLE)
    op.drop_index("ix_tenants_tenant_type", table_name=_TENANTS_TABLE)
    op.drop_table(_TENANTS_TABLE)
