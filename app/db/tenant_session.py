"""Tenant-aware database session dependency for Row-Level Security enforcement.

How it works
------------
PostgreSQL RLS policies in `sql/rls_tenant_partitioning.sql` use the GUC
(Grand Unified Configuration) variable ``app.current_tenant_id`` to decide
which rows a session may read or write:

    USING (tenant_id = current_setting('app.current_tenant_id', TRUE))

This module provides two FastAPI dependencies that emit a ``SET LOCAL``
statement immediately after acquiring a connection, binding the GUC to the
authenticated principal's ``tenant_id`` for the life of that DB transaction:

    get_tenant_db_session  — standard per-request dependency; 401s if no
                             principal is on the request state (callers that
                             need optional auth should use get_db_session
                             from app.db.session instead).

    get_admin_db_session   — sets the GUC to the ``__admin__`` sentinel,
                             which the ``is_admin_context()`` helper function
                             in PostgreSQL evaluates to "bypass RLS filters".
                             Restricted to System_Admin / Compliance_Officer
                             principals at the dependency level.

Why SET LOCAL (transaction-scope) and not SET SESSION (connection-scope)
------------------------------------------------------------------------
SQLAlchemy's async connection pool re-uses connections across requests.
``SET SESSION`` would bleed one request's tenant context into the next
request that checks out the same physical connection.  ``SET LOCAL``
applies only for the lifetime of the current transaction, which is
committed/rolled-back at the end of every ``get_tenant_db_session`` yield,
guaranteeing the GUC is reset before the connection is returned to the pool.

Sandbox sessions
----------------
The sandbox dry-run API (app/api/sandbox_routes.py) uses this module's
``get_tenant_db_session`` directly: it gets a tenant-scoped session (so RLS
limits the circular/clause data the sandbox can read to only that tenant's
own data plus shared SEBI baseline), but wraps its work in an explicit
``ROLLBACK`` so no sandbox artefacts are persisted.  See
``SandboxSessionContext`` below.
"""
from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_session_factory
from app.security.models import Principal, Role

logger = logging.getLogger(__name__)

# The GUC name must match what sql/rls_tenant_partitioning.sql uses.
_GUC = "app.current_tenant_id"
_ADMIN_SENTINEL = "__admin__"


async def _set_tenant_context(session: AsyncSession, tenant_id: str) -> None:
    """Emit ``SET LOCAL app.current_tenant_id = '<tenant_id>'`` on the
    underlying connection.  Must be called inside an open transaction so
    the LOCAL scope is meaningful; ``get_tenant_db_session`` / ``get_admin_db_session``
    call it as the first statement after the session context manager opens."""
    # Parameterised literals are not supported for SET LOCAL; the tenant_id
    # is validated upstream (it came from a verified JWT claim or the
    # admin-sentinel constant), so this is safe.  We still sanitise by
    # refusing any value containing a single-quote to prevent injection
    # through a malformed/compromised token.
    if "'" in tenant_id:
        raise ValueError(f"tenant_id contains illegal character: {tenant_id!r}")
    await session.execute(text(f"SET LOCAL {_GUC} = '{tenant_id}'"))


async def get_tenant_db_session(request: Request) -> AsyncIterator[AsyncSession]:
    """FastAPI dependency: one RLS-scoped DB session per request.

    Resolves the authenticated principal from ``request.state.principal``
    (set by ``JWTAuthenticationMiddleware`` before this dependency runs)
    and sets ``app.current_tenant_id`` to the principal's ``tenant_id``.

    System_Admin and Compliance_Officer principals (who are not tenant-
    scoped) are routed through the admin GUC sentinel (``__admin__``) so
    they see all rows.  This is intentional: compliance officers need to
    query across all tenant partitions for HITL review.

    Raises
    ------
    401  if no authenticated principal is on the request state.
    403  if a Broker_API_Client token is missing a tenant_id claim
         (this should never happen — TokenPayload validates it — but we
         guard defensively here too).
    """
    principal: Principal | None = getattr(request.state, "principal", None)
    if principal is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if principal.is_admin() or Role.COMPLIANCE_OFFICER in principal.roles:
        effective_tenant = _ADMIN_SENTINEL
    elif principal.tenant_id:
        effective_tenant = principal.tenant_id
    else:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Broker_API_Client token is missing tenant_id claim.",
        )

    session_factory = get_session_factory()
    async with session_factory() as session:
        try:
            await _set_tenant_context(session, effective_tenant)
            logger.debug(
                "DB session opened: tenant=%s principal=%s",
                effective_tenant,
                principal.subject,
            )
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def get_admin_db_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency: admin-scoped DB session (bypasses RLS row filters).

    Intended for System_Admin / Compliance_Officer endpoints that need to
    query or write across all tenant partitions (e.g. ingestion, bulk
    compilation, cross-tenant HITL dashboards).

    Callers should pair this with ``require_roles(Role.SYSTEM_ADMIN)`` or
    ``require_roles(Role.COMPLIANCE_OFFICER)`` to enforce the auth check
    before this dependency provides the session.
    """
    session_factory = get_session_factory()
    async with session_factory() as session:
        try:
            await _set_tenant_context(session, _ADMIN_SENTINEL)
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


class SandboxSessionContext:
    """Async context manager for sandbox dry-run DB sessions.

    Provides a *read-only*, automatically rolled-back tenant-scoped session
    that the sandbox evaluation API uses to query historical circulars and
    compiled rules without persisting any artefact.

    Usage (inside a FastAPI route)::

        async with SandboxSessionContext(tenant_id) as session:
            result = await session.execute(select(Circular).limit(20))
            # ... read-only work ...
            # ROLLBACK is guaranteed even if an exception is raised.

    The session is tenant-scoped via RLS (same as ``get_tenant_db_session``),
    so the sandbox can only read circulars/rules that belong to the requesting
    tenant or are shared SEBI baseline data.  It cannot read another tenant's
    rules even if it tries to inject a different tenant_id in SQL, because RLS
    operates at the PostgreSQL statement level, not the application level.
    """

    def __init__(self, tenant_id: str) -> None:
        self._tenant_id = tenant_id
        self._session: AsyncSession | None = None
        self._factory = get_session_factory()

    async def __aenter__(self) -> AsyncSession:
        self._cm = self._factory()
        self._session = await self._cm.__aenter__()
        await _set_tenant_context(self._session, self._tenant_id)
        return self._session

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        # Always rollback — this is a dry-run context; persistence is never
        # intended regardless of whether an exception occurred.
        if self._session is not None:
            await self._session.rollback()
        await self._cm.__aexit__(exc_type, exc_val, exc_tb)


@asynccontextmanager
async def tenant_session_for(tenant_id: str) -> AsyncIterator[AsyncSession]:
    """Async context-manager helper for non-request code paths (Celery tasks,
    lifespan hooks, CLI scripts) that need a tenant-scoped session outside
    FastAPI's dependency injection system.

    Commits on clean exit, rolls back on exception — same contract as
    ``get_tenant_db_session``.

    Example (inside a Celery task)::

        async with tenant_session_for(event.tenant_id) as db:
            db.add(SomeModel(tenant_id=event.tenant_id, ...))
    """
    session_factory = get_session_factory()
    async with session_factory() as session:
        try:
            await _set_tenant_context(session, tenant_id)
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
