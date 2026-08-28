"""Alembic environment, wired to the app's own settings and async engine
rather than a static URL in alembic.ini, so one .env drives both the
running service and its migrations.

Async-aware: `run_migrations_online` opens an `AsyncEngine` and runs the
actual (sync) migration machinery via `AsyncConnection.run_sync`, per
SQLAlchemy's documented pattern for driving Alembic against an asyncpg URL.
"""
from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import Connection, pool
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

# Import order matters: app.db.base.Base.metadata must have every table
# registered on it (circulars, clauses, compiled_rules, hitl_reviews, and
# compliance_audit_ledger) before Alembic autogenerate diffs against it.
from app.config import get_settings
from app.db.base import Base
from app.db import models as _db_models  # noqa: F401
from app.ledger import models as _ledger_models  # noqa: F401

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def get_url() -> str:
    x_args = context.get_x_argument(as_dictionary=True)
    return x_args.get("sqlalchemy_url") or get_settings().database_url


def run_migrations_offline() -> None:
    """Emit SQL to stdout instead of executing against a live DB (`alembic upgrade head --sql`)."""
    context.configure(
        url=get_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def _do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    connectable: AsyncEngine = create_async_engine(get_url(), poolclass=pool.NullPool)

    async with connectable.connect() as connection:
        await connection.run_sync(_do_run_migrations)

    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
