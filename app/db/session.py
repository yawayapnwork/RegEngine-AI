"""Async engine/session factory for the main application schema (circulars,
clauses, compiled_rules, hitl_reviews).

Kept distinct from `app.ledger.db.get_ledger_engine`: the ledger connects
as a least-privilege, INSERT+SELECT-only role (`regengine_ledger_writer`,
see `sql/ledger_schema.sql`), while this engine's role needs ordinary
read/write access to the rest of the schema. Pointing both at the same
Postgres database with different roles is the expected deployment shape.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from functools import lru_cache

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from app.config import get_settings


@lru_cache(maxsize=1)
def get_engine() -> AsyncEngine:
    settings = get_settings()
    return create_async_engine(settings.database_url, pool_size=settings.database_pool_size, pool_pre_ping=True)


@lru_cache(maxsize=1)
def get_session_factory() -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(bind=get_engine(), expire_on_commit=False)


async def get_db_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency: one session per request, committed on clean exit,
    rolled back on exception."""
    session_factory = get_session_factory()
    async with session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
