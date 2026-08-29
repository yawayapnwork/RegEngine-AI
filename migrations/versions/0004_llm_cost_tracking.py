"""Add llm_usage_events: durable per-invocation record for the LLM cost
optimization layer (app.llm_ops) -- semantic cache hits, model-tier
routing decisions, token counts, and estimated USD cost -- feeding the
per-tenant cost dashboard (app.llm_ops.aggregator, GET /v1/llm-cost/*).

tenant_id uses ON DELETE SET NULL (not CASCADE, unlike the compliance
tables in 0003_tenant_partitioning): a tenant being deregistered should
never silently delete historical cost/spend records that finance or an
auditor may still need to reconcile against past invoices.

Revision ID: 0004_llm_cost_tracking
Revises: 0003_tenant_partitioning
Create Date: 2026-08-29
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0004_llm_cost_tracking"
down_revision: Union[str, None] = "0003_tenant_partitioning"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "llm_usage_events"


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("id", sa.BigInteger(), nullable=False, autoincrement=True),
        sa.Column("event_id", sa.String(length=64), nullable=False),
        sa.Column(
            "tenant_id",
            sa.Text(),
            sa.ForeignKey("tenants.tenant_id", ondelete="SET NULL", name="fk_llm_usage_events_tenant_id_tenants"),
            nullable=True,
        ),
        sa.Column("task_type", sa.String(length=32), nullable=False),
        sa.Column("model_tier", sa.String(length=16), nullable=False),
        sa.Column("model_name", sa.String(length=128), nullable=True),
        sa.Column("cache_layer", sa.String(length=16), nullable=False, server_default="none"),
        sa.Column("complexity", sa.String(length=16), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("output_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cost_usd", sa.Numeric(12, 6), nullable=False, server_default="0"),
        sa.Column("latency_ms", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("escalated_from_cheap", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("clause_sha256", sa.String(length=64), nullable=True),
        sa.Column("details", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("id", name="pk_llm_usage_events"),
        sa.UniqueConstraint("event_id", name="uq_llm_usage_events_event_id"),
        sa.CheckConstraint(
            "model_tier IN ('cache_hit','cheap_local','frontier')", name="model_tier",
        ),
        sa.CheckConstraint(
            "cache_layer IN ('none','exact','semantic')", name="cache_layer",
        ),
    )
    op.create_index("ix_llm_usage_events_tenant_created", _TABLE, ["tenant_id", "created_at"])
    op.create_index("ix_llm_usage_events_model_tier", _TABLE, ["model_tier"])
    op.create_index("ix_llm_usage_events_created_at", _TABLE, ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_llm_usage_events_created_at", table_name=_TABLE)
    op.drop_index("ix_llm_usage_events_model_tier", table_name=_TABLE)
    op.drop_index("ix_llm_usage_events_tenant_created", table_name=_TABLE)
    op.drop_table(_TABLE)
