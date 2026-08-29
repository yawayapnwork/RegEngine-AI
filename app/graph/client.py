"""Async Neo4j driver wrapper -- one process-wide driver instance
(connection pooling is the driver's own job, same rationale as
app.execution.dependencies.get_redis_pool's `@lru_cache` singleton for
Redis), constructed lazily so importing app.graph never requires a
reachable Neo4j instance unless `settings.neo4j_sync_enabled` is actually
True and something calls `get_neo4j_driver()`.
"""
from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path

from neo4j import AsyncDriver, AsyncGraphDatabase, AsyncSession

from app.config import Settings

logger = logging.getLogger(__name__)

_SCHEMA_CYPHER_PATH = Path(__file__).resolve().parent.parent.parent / "cypher" / "schema.cypher"


@lru_cache(maxsize=1)
def get_neo4j_driver(settings: Settings | None = None) -> AsyncDriver:
    from app.config import get_settings

    settings = settings or get_settings()
    return AsyncGraphDatabase.driver(settings.neo4j_uri, auth=(settings.neo4j_user, settings.neo4j_password))


def session(settings: Settings | None = None) -> AsyncSession:
    from app.config import get_settings

    settings = settings or get_settings()
    driver = get_neo4j_driver(settings)
    return driver.session(database=settings.neo4j_database)


async def apply_schema(settings: Settings | None = None) -> None:
    """Runs every statement in cypher/schema.cypher -- idempotent
    (every constraint/index uses `IF NOT EXISTS`), safe to call on every
    deploy rather than needing a separate one-time migration step, the
    same way app.execution's OPA policies are republished idempotently
    on every hot-reload event rather than requiring "has this already
    been applied" bookkeeping."""
    raw_statements = _SCHEMA_CYPHER_PATH.read_text(encoding="utf-8").split(";")
    statements = [s.strip() for s in raw_statements if any(line.strip() and not line.strip().startswith("//") for line in s.splitlines())]

    async with session(settings) as sess:
        for statement in statements:
            await sess.run(statement)
    logger.info("Applied %d schema statement(s) from %s", len(statements), _SCHEMA_CYPHER_PATH)


async def close_neo4j_driver() -> None:
    """Call on process shutdown (app.main's lifespan) if the driver was
    ever constructed -- a no-op (checked via the lru_cache's own hit
    count) when neo4j_sync_enabled was never on, so shutdown never tries
    to close a connection that was never opened."""
    if get_neo4j_driver.cache_info().currsize:
        await get_neo4j_driver().close()
        get_neo4j_driver.cache_clear()
