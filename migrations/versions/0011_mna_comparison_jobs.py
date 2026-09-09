"""Add mna_comparison_jobs table for tracking M&A compliance due-diligence comparisons.

Enables persistent tracking of due-diligence jobs between two authorized regulated entities,
storing snapshot hashes, progress, risk scores, and the final due-diligence report.

Revision ID: 0011_mna_comparison_jobs
Revises: 0010_resumable_circular_processing
Create Date: 2026-09-09
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0011_mna_comparison_jobs"
down_revision: Union[str, None] = "0010_resumable_circular_processing"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_JSON_TYPE = sa.JSON().with_variant(JSONB, "postgresql")
_ID_TYPE = sa.BigInteger().with_variant(sa.Integer(), "sqlite")

_TABLE = "mna_comparison_jobs"
_MNA_JOB_STATUSES = (
    "QUEUED",
    "SNAPSHOT_ACQUISITION",
    "DETERMINISTIC_DIFFING",
    "SEMANTIC_ANALYSIS",
    "COMPLETED",
    "FAILED",
    "CANCELLED",
)


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("id", _ID_TYPE, primary_key=True, autoincrement=True),
        sa.Column("job_id", sa.String(length=64), nullable=False),
        sa.Column("entity_a_id", sa.Text(), nullable=False),
        sa.Column("entity_b_id", sa.Text(), nullable=False),
        sa.Column("initiator_subject", sa.String(length=320), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="QUEUED"),
        sa.Column("progress_pct", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("current_step", sa.String(length=128), nullable=True),
        sa.Column("entity_a_snapshot_hash", sa.String(length=64), nullable=True),
        sa.Column("entity_b_snapshot_hash", sa.String(length=64), nullable=True),
        sa.Column("overall_risk_score", sa.Float(), nullable=True),
        sa.Column("findings_count", sa.Integer(), nullable=True),
        sa.Column("report_data", _JSON_TYPE, nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_mna_comparison_jobs"),
        sa.UniqueConstraint("job_id", name="uq_mna_comparison_jobs_job_id"),
        sa.CheckConstraint(f"status IN {_MNA_JOB_STATUSES!r}", name="mna_job_status"),
    )
    op.create_index("ix_mna_comparison_jobs_status", _TABLE, ["status"])
    op.create_index("ix_mna_comparison_jobs_entities", _TABLE, ["entity_a_id", "entity_b_id"])
    op.create_index("ix_mna_comparison_jobs_initiator", _TABLE, ["initiator_subject"])


def downgrade() -> None:
    op.drop_index("ix_mna_comparison_jobs_initiator", table_name=_TABLE)
    op.drop_index("ix_mna_comparison_jobs_entities", table_name=_TABLE)
    op.drop_index("ix_mna_comparison_jobs_status", table_name=_TABLE)
    op.drop_table(_TABLE)
