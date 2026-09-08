"""Add processing_state to circulars, processing_status to clauses, and circular_state_transitions table.

Enables resumable, observable circular processing pipeline with transactional checkpoints,
idempotent retries, and an immutable state transition audit log.

Revision ID: 0010_resumable_circular_processing
Revises: 0009_circular_source_metadata
Create Date: 2026-09-08
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0010_resumable_circular_processing"
down_revision: Union[str, None] = "0009_circular_source_metadata"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_JSON_TYPE = sa.JSON().with_variant(JSONB, "postgresql")
_ID_TYPE = sa.BigInteger().with_variant(sa.Integer(), "sqlite")

_CIRCULAR_PROCESSING_STATES = (
    "INGESTED",
    "EXTRACTING",
    "EXTRACTED",
    "COMPILING",
    "AWAITING_HITL",
    "APPROVED",
    "DEPLOYED",
    "FAILED",
)
_CLAUSE_PROCESSING_STATUSES = (
    "PENDING",
    "EXTRACTING",
    "EXTRACTED",
    "COMPILING",
    "COMPILED",
    "FAILED",
)


def upgrade() -> None:
    # 1. Add processing_state and error_message to circulars
    op.add_column(
        "circulars",
        sa.Column(
            "processing_state",
            sa.String(32),
            nullable=False,
            server_default="INGESTED",
        ),
    )
    op.add_column(
        "circulars",
        sa.Column("error_message", sa.Text(), nullable=True),
    )
    op.create_index(
        "ix_circulars_processing_state",
        "circulars",
        ["processing_state"],
    )

    # 2. Add processing_status and error_message to clauses
    op.add_column(
        "clauses",
        sa.Column(
            "processing_status",
            sa.String(32),
            nullable=False,
            server_default="PENDING",
        ),
    )
    op.add_column(
        "clauses",
        sa.Column("error_message", sa.Text(), nullable=True),
    )
    op.create_index(
        "ix_clauses_processing_status",
        "clauses",
        ["processing_status"],
    )

    # 3. Create circular_state_transitions table
    op.create_table(
        "circular_state_transitions",
        sa.Column("id", _ID_TYPE, primary_key=True, autoincrement=True),
        sa.Column(
            "circular_id",
            _ID_TYPE,
            sa.ForeignKey("circulars.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("from_state", sa.String(32), nullable=True),
        sa.Column("to_state", sa.String(32), nullable=False),
        sa.Column(
            "triggered_by",
            sa.String(128),
            nullable=False,
            server_default="orchestrator",
        ),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("details", _JSON_TYPE, nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_circular_state_transitions_circular_id",
        "circular_state_transitions",
        ["circular_id"],
    )
    op.create_index(
        "ix_circular_state_transitions_created_at",
        "circular_state_transitions",
        ["created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_circular_state_transitions_created_at",
        table_name="circular_state_transitions",
    )
    op.drop_index(
        "ix_circular_state_transitions_circular_id",
        table_name="circular_state_transitions",
    )
    op.drop_table("circular_state_transitions")

    op.drop_index("ix_clauses_processing_status", table_name="clauses")
    op.drop_column("clauses", "error_message")
    op.drop_column("clauses", "processing_status")

    op.drop_index("ix_circulars_processing_state", table_name="circulars")
    op.drop_column("circulars", "error_message")
    op.drop_column("circulars", "processing_state")
