"""Add ingestion_upload_jobs: tracks a manually-uploaded PDF from
POST /v1/ingestion/uploads through to completion in a Celery worker, so
the upload request can return immediately (202) instead of blocking on
the full parse -> index pipeline inside one HTTP request. See
app.db.models.IngestionUploadJob's docstring.

Revision ID: 0007_ingestion_upload_jobs
Revises: 0006_local_user_accounts
Create Date: 2026-08-31
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0007_ingestion_upload_jobs"
down_revision: Union[str, None] = "0006_local_user_accounts"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "ingestion_upload_jobs"


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("id", sa.BigInteger(), nullable=False, autoincrement=True),
        sa.Column("job_id", sa.String(length=64), nullable=False),
        sa.Column("filename", sa.Text(), nullable=False),
        sa.Column("object_key", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="queued"),
        sa.Column("chunks_indexed", sa.Integer(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_by", sa.String(length=320), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("id", name="pk_ingestion_upload_jobs"),
        sa.UniqueConstraint("job_id", name="uq_ingestion_upload_jobs_job_id"),
        sa.CheckConstraint(
            "status IN ('queued', 'processing', 'completed', 'failed')", name="status"
        ),
    )
    op.create_index("ix_ingestion_upload_jobs_status", _TABLE, ["status"])


def downgrade() -> None:
    op.drop_index("ix_ingestion_upload_jobs_status", table_name=_TABLE)
    op.drop_table(_TABLE)
