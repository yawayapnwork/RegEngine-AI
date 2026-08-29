"""Board-level AI governance (app.governance): agent_inventory (Requirement
2's Named Owner & Inventory Registry) and kill_switch_events (Requirement
1's durable kill-switch audit trail, read by Requirement 3's governance
report for drill-test history).

kill_switch_events.tenant_id uses ON DELETE SET NULL, matching
0004_llm_cost_tracking's precedent for a historical audit table: a
tenant being deregistered must never silently delete the permanent
record that a kill switch was activated against it.

Revision ID: 0005_governance_registry
Revises: 0004_llm_cost_tracking
Create Date: 2026-08-29
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0005_governance_registry"
down_revision: Union[str, None] = "0004_llm_cost_tracking"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_AGENT_TABLE = "agent_inventory"
_EVENT_TABLE = "kill_switch_events"


def upgrade() -> None:
    op.create_table(
        _AGENT_TABLE,
        sa.Column("id", sa.BigInteger(), nullable=False, autoincrement=True),
        sa.Column("agent_key", sa.String(length=64), nullable=False),
        sa.Column("display_name", sa.String(length=200), nullable=False),
        sa.Column("model_provider", sa.String(length=64), nullable=False),
        sa.Column("model_weight_version", sa.String(length=128), nullable=False),
        sa.Column("business_domain", sa.Text(), nullable=False),
        sa.Column("is_critical_operation", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("owner_name", sa.String(length=200), nullable=False),
        sa.Column("owner_email", sa.String(length=320), nullable=False),
        sa.Column("owner_role", sa.String(length=100), nullable=False, server_default="Compliance_Officer"),
        sa.Column("deployed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("last_reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_agent_inventory"),
        sa.UniqueConstraint("agent_key", name="uq_agent_inventory_agent_key"),
        sa.CheckConstraint("(retired_at IS NOT NULL) = (is_active = false)", name="ck_agent_inventory_retired_at_consistency"),
    )
    op.create_index("ix_agent_inventory_is_active", _AGENT_TABLE, ["is_active"])
    op.create_index("ix_agent_inventory_owner_email", _AGENT_TABLE, ["owner_email"])

    op.create_table(
        _EVENT_TABLE,
        sa.Column("id", sa.BigInteger(), nullable=False, autoincrement=True),
        sa.Column("event_id", sa.String(length=64), nullable=False),
        sa.Column("scope", sa.String(length=16), nullable=False),
        sa.Column(
            "tenant_id",
            sa.Text(),
            sa.ForeignKey("tenants.tenant_id", ondelete="SET NULL", name="fk_kill_switch_events_tenant_id_tenants"),
            nullable=True,
        ),
        sa.Column("action", sa.String(length=16), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("actor", sa.String(length=200), nullable=False),
        sa.Column("is_drill", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("details", sa.JSON(), nullable=False, server_default="{}"),
        sa.PrimaryKeyConstraint("id", name="pk_kill_switch_events"),
        sa.UniqueConstraint("event_id", name="uq_kill_switch_events_event_id"),
        sa.CheckConstraint("scope IN ('global','tenant')", name="ck_kill_switch_events_scope"),
        sa.CheckConstraint("action IN ('activated','deactivated','drill')", name="ck_kill_switch_events_action"),
        sa.CheckConstraint("(scope = 'global') = (tenant_id IS NULL)", name="ck_kill_switch_events_tenant_id_matches_scope"),
    )
    op.create_index("ix_kill_switch_events_occurred_at", _EVENT_TABLE, ["occurred_at"])
    op.create_index("ix_kill_switch_events_scope_tenant", _EVENT_TABLE, ["scope", "tenant_id"])
    op.create_index("ix_kill_switch_events_is_drill", _EVENT_TABLE, ["is_drill"])


def downgrade() -> None:
    op.drop_index("ix_kill_switch_events_is_drill", table_name=_EVENT_TABLE)
    op.drop_index("ix_kill_switch_events_scope_tenant", table_name=_EVENT_TABLE)
    op.drop_index("ix_kill_switch_events_occurred_at", table_name=_EVENT_TABLE)
    op.drop_table(_EVENT_TABLE)

    op.drop_index("ix_agent_inventory_owner_email", table_name=_AGENT_TABLE)
    op.drop_index("ix_agent_inventory_is_active", table_name=_AGENT_TABLE)
    op.drop_table(_AGENT_TABLE)
