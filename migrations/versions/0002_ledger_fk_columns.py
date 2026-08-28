"""Add nullable referential-integrity FK columns to compliance_audit_ledger
(the audit_logs table), pointing back at circulars/clauses/compiled_rules/
hitl_reviews for fast joins/reporting.

Assumes `compliance_audit_ledger` already exists -- it is provisioned by
`sql/ledger_schema.sql` (its append-only triggers and least-privilege role
grants live there, deliberately outside Alembic's purview; see that file's
header comment). Run `psql -f sql/ledger_schema.sql` before this migration
on a fresh database.

These columns are additive and nullable by design: the hash chain in
payload_digest/current_hash is computed only over the pre-existing business
columns, so adding columns here can never invalidate an already-appended
row's hash, and a ledger write is never blocked on these lookups
succeeding (see app/ledger/models.py's comment on the same columns).

Revision ID: 0002_ledger_fk_columns
Revises: 0001_initial_schema
Create Date: 2026-08-28
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0002_ledger_fk_columns"
down_revision: Union[str, None] = "0001_initial_schema"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "compliance_audit_ledger"


def upgrade() -> None:
    op.add_column(_TABLE, sa.Column("circular_ref_id", sa.BigInteger(), nullable=True))
    op.add_column(_TABLE, sa.Column("clause_ref_id", sa.BigInteger(), nullable=True))
    op.add_column(_TABLE, sa.Column("compiled_rule_ref_id", sa.BigInteger(), nullable=True))
    op.add_column(_TABLE, sa.Column("hitl_review_ref_id", sa.BigInteger(), nullable=True))

    op.create_foreign_key(
        "fk_compliance_audit_ledger_circular_ref_id_circulars",
        _TABLE, "circulars", ["circular_ref_id"], ["id"], ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_compliance_audit_ledger_clause_ref_id_clauses",
        _TABLE, "clauses", ["clause_ref_id"], ["id"], ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_compliance_audit_ledger_compiled_rule_ref_id_compiled_rules",
        _TABLE, "compiled_rules", ["compiled_rule_ref_id"], ["id"], ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_compliance_audit_ledger_hitl_review_ref_id_hitl_reviews",
        _TABLE, "hitl_reviews", ["hitl_review_ref_id"], ["id"], ondelete="SET NULL",
    )

    op.create_index("ix_compliance_audit_ledger_circular_ref_id", _TABLE, ["circular_ref_id"])
    op.create_index("ix_compliance_audit_ledger_clause_ref_id", _TABLE, ["clause_ref_id"])
    op.create_index("ix_compliance_audit_ledger_compiled_rule_ref_id", _TABLE, ["compiled_rule_ref_id"])
    op.create_index("ix_compliance_audit_ledger_hitl_review_ref_id", _TABLE, ["hitl_review_ref_id"])


def downgrade() -> None:
    op.drop_index("ix_compliance_audit_ledger_hitl_review_ref_id", table_name=_TABLE)
    op.drop_index("ix_compliance_audit_ledger_compiled_rule_ref_id", table_name=_TABLE)
    op.drop_index("ix_compliance_audit_ledger_clause_ref_id", table_name=_TABLE)
    op.drop_index("ix_compliance_audit_ledger_circular_ref_id", table_name=_TABLE)

    op.drop_constraint("fk_compliance_audit_ledger_hitl_review_ref_id_hitl_reviews", _TABLE, type_="foreignkey")
    op.drop_constraint("fk_compliance_audit_ledger_compiled_rule_ref_id_compiled_rules", _TABLE, type_="foreignkey")
    op.drop_constraint("fk_compliance_audit_ledger_clause_ref_id_clauses", _TABLE, type_="foreignkey")
    op.drop_constraint("fk_compliance_audit_ledger_circular_ref_id_circulars", _TABLE, type_="foreignkey")

    op.drop_column(_TABLE, "hitl_review_ref_id")
    op.drop_column(_TABLE, "compiled_rule_ref_id")
    op.drop_column(_TABLE, "clause_ref_id")
    op.drop_column(_TABLE, "circular_ref_id")
