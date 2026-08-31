"""Add users: locally-provisioned (email/password) human accounts for
POST /v1/auth/login and /v1/auth/signup (app.api.auth_routes), replacing
app.security.local_user_store's Redis backing with durable Postgres
storage. No tenant_id column -- see app.db.models.User's docstring: human
roles (Compliance_Officer/System_Admin) are never tenant-scoped in this
schema, unlike Broker_API_Client OAuth2 clients.

Revision ID: 0006_local_user_accounts
Revises: 0005_governance_registry
Create Date: 2026-08-31
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0006_local_user_accounts"
down_revision: Union[str, None] = "0005_governance_registry"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "users"


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("id", sa.BigInteger(), nullable=False, autoincrement=True),
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("password_hash", sa.String(length=128), nullable=False),
        sa.Column("roles", sa.JSON(), nullable=False, server_default='["Compliance_Officer"]'),
        sa.Column("disabled", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("id", name="pk_users"),
        sa.UniqueConstraint("user_id", name="uq_users_user_id"),
        sa.UniqueConstraint("email", name="uq_users_email"),
    )
    op.create_index("ix_users_email", _TABLE, ["email"])


def downgrade() -> None:
    op.drop_index("ix_users_email", table_name=_TABLE)
    op.drop_table(_TABLE)
