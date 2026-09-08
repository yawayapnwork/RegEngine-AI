"""Add source_filename and source_retrieved_at to circulars.

Separates the external source URL (`source_url`) from the actual local
filename (`source_filename`) and source retrieval timestamp (`source_retrieved_at`).
Never allows a filename to be stored in `source_url`. If a circular was uploaded
locally with no external URL, `source_url` is NULL.

Revision ID: 0009_circular_source_metadata
Revises: 0008_document_hashing
Create Date: 2026-09-08
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0009_circular_source_metadata"
down_revision: Union[str, None] = "0008_document_hashing"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Add source_filename to circulars
    op.add_column(
        "circulars",
        sa.Column("source_filename", sa.Text(), nullable=True),
    )
    op.create_index(
        "ix_circulars_source_filename",
        "circulars",
        ["source_filename"],
    )

    # 2. Add source_retrieved_at to circulars
    op.add_column(
        "circulars",
        sa.Column("source_retrieved_at", sa.DateTime(timezone=True), nullable=True),
    )

    # 3. Backfill: If source_url holds a local filename (not starting with http://, https://, or ftp://),
    # move it to source_filename and set source_url to NULL.
    op.execute(
        sa.text(
            "UPDATE circulars "
            "SET source_filename = source_url, source_url = NULL "
            "WHERE source_url IS NOT NULL "
            "AND source_url NOT LIKE 'http://%' "
            "AND source_url NOT LIKE 'https://%' "
            "AND source_url NOT LIKE 'ftp://%'"
        )
    )


def downgrade() -> None:
    op.drop_column("circulars", "source_retrieved_at")
    op.drop_index("ix_circulars_source_filename", table_name="circulars")
    op.drop_column("circulars", "source_filename")
