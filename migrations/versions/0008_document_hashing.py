"""Add source_document_sha256 to circulars and ingestion_upload_jobs.

Separates the cryptographic SHA-256 hash of the original uploaded PDF bytes
(`source_document_sha256`) from the normalized extracted text digest
(`raw_text_digest`). Existing historical columns and constraints are preserved
without repurposing.

Revision ID: 0008_document_hashing
Revises: 0007_ingestion_upload_jobs
Create Date: 2026-09-08
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0008_document_hashing"
down_revision: Union[str, None] = "0007_ingestion_upload_jobs"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. circulars: add source_document_sha256
    op.add_column(
        "circulars",
        sa.Column("source_document_sha256", sa.String(length=64), nullable=True),
    )
    op.create_index(
        "ix_circulars_source_document_sha256",
        "circulars",
        ["source_document_sha256"],
    )
    op.create_check_constraint(
        "source_document_sha256_len",
        "circulars",
        "source_document_sha256 IS NULL OR length(source_document_sha256) = 64",
    )

    # 2. ingestion_upload_jobs: add source_document_sha256
    op.add_column(
        "ingestion_upload_jobs",
        sa.Column("source_document_sha256", sa.String(length=64), nullable=True),
    )
    op.create_index(
        "ix_ingestion_upload_jobs_source_doc_sha256",
        "ingestion_upload_jobs",
        ["source_document_sha256"],
    )
    op.create_check_constraint(
        "upload_jobs_source_doc_sha256_len",
        "ingestion_upload_jobs",
        "source_document_sha256 IS NULL OR length(source_document_sha256) = 64",
    )


def downgrade() -> None:
    op.drop_constraint("upload_jobs_source_doc_sha256_len", "ingestion_upload_jobs", type_="check")
    op.drop_index("ix_ingestion_upload_jobs_source_doc_sha256", table_name="ingestion_upload_jobs")
    op.drop_column("ingestion_upload_jobs", "source_document_sha256")

    op.drop_constraint("source_document_sha256_len", "circulars", type_="check")
    op.drop_index("ix_circulars_source_document_sha256", table_name="circulars")
    op.drop_column("circulars", "source_document_sha256")
