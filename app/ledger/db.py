"""Async SQLAlchemy engine factory for the ledger, kept separate from
`app.config` so callers (API, Celery tasks, CLI script) share one
construction path instead of each building `create_async_engine` slightly
differently."""
from __future__ import annotations

from functools import lru_cache

from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.config import get_settings


@lru_cache(maxsize=1)
def get_ledger_engine() -> AsyncEngine:
    settings = get_settings()
    return create_async_engine(settings.ledger_database_url, pool_size=settings.ledger_pool_size, pool_pre_ping=True)
