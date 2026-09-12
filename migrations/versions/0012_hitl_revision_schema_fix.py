"""Fix HITL review schema drift that breaks the revision flow.

Since 0001_initial_schema, app.db.models.HITLReview permited a
"REVISION_REQUIRED" status (17 characters) on a `status` column created as
VARCHAR(16), and extended the `reason_code` set with three compiler codes
(`unknown_fact_metric`, `invalid_metric_unit`, `invalid_threshold_value`)
that the 0001 check constraint never admitted. Any POST
/v1/hitl-reviews/{id}/request-revision therefore 500'd with a
length/check-violation, and a live-LLM extraction emitting one of the new
reason codes failed the whole circular with a CheckViolation on the
compile checkpoint.

This migration widens the column and replaces the three check constraints
to match today's model definitions.

Revision ID: 0012_hitl_revision_schema_fix
Revises: 0011_mna_comparison_jobs
Create Date: 2026-09-12
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0012_hitl_revision_schema_fix"
down_revision: Union[str, None] = "0011_mna_comparison_jobs"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_HITL_REASON_CODES = (
    "qualitative_directive",
    "ambiguous_span",
    "low_extraction_confidence",
    "audit_not_approved",
    "no_deterministic_logic",
    "conflicting_thresholds",
    "unresolved_entity",
    "unknown_fact_metric",
    "invalid_metric_unit",
    "invalid_threshold_value",
)
_HITL_REVIEW_STATUSES = ("PENDING", "IN_REVIEW", "RESOLVED", "REJECTED", "REVISION_REQUIRED")


def upgrade() -> None:
    with op.batch_alter_table("hitl_reviews") as batch_op:
        # "REVISION_REQUIRED" is 17 chars; the 0001 column was VARCHAR(16).
        batch_op.alter_column(
            "status",
            existing_type=sa.String(length=16),
            type_=sa.String(length=24),
            existing_nullable=False,
            existing_server_default="PENDING",
        )
        batch_op.drop_constraint("status", type_="check")
        batch_op.drop_constraint("resolved_at_consistency", type_="check")
        batch_op.drop_constraint("reason_code", type_="check")
        batch_op.create_check_constraint(
            "status", f"status IN {_HITL_REVIEW_STATUSES!r}"
        )
        batch_op.create_check_constraint(
            "resolved_at_consistency",
            "(status IN ('RESOLVED', 'REJECTED', 'REVISION_REQUIRED')) = (resolved_at IS NOT NULL)",
        )
        batch_op.create_check_constraint("reason_code", f"reason_code IN {_HITL_REASON_CODES!r}")


def downgrade() -> None:
    with op.batch_alter_table("hitl_reviews") as batch_op:
        batch_op.drop_constraint("reason_code", type_="check")
        batch_op.drop_constraint("resolved_at_consistency", type_="check")
        batch_op.drop_constraint("status", type_="check")
        batch_op.create_check_constraint(
            "reason_code",
            "reason_code IN ('qualitative_directive','ambiguous_span','low_extraction_confidence',"
            "'audit_not_approved','no_deterministic_logic','conflicting_thresholds','unresolved_entity')",
        )
        batch_op.create_check_constraint(
            "resolved_at_consistency",
            "(status IN ('RESOLVED', 'REJECTED')) = (resolved_at IS NOT NULL)",
        )
        batch_op.create_check_constraint(
            "status", "status IN ('PENDING','IN_REVIEW','RESOLVED','REJECTED')"
        )
        batch_op.alter_column(
            "status",
            existing_type=sa.String(length=24),
            type_=sa.String(length=16),
            existing_nullable=False,
            existing_server_default="PENDING",
        )