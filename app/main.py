"""FastAPI application entrypoint."""
from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator

from fastapi import FastAPI
from fastapi.responses import JSONResponse, Response
from starlette.requests import Request

from app.api.analytics_routes import router as analytics_router
from app.api.auth_routes import router as auth_router
from app.api.dlq_routes import router as dlq_router
from app.api.execution_routes import router as execution_router
from app.api.hitl_review_routes import router as hitl_review_router
from app.api.ingestion_routes import router as ingestion_router
from app.api.routes import router
from app.api.sandbox_routes import router as sandbox_router
from app.config import get_settings
from app.db.session import get_session_factory
from app.execution.dependencies import get_opa_engine, get_policy_cache, get_policy_registry, get_redis_pool
from app.execution.hitl_queue import HITLQueue
from app.execution.policy_hot_reload import PolicyHotReloadSubscriber
from app.observability.metrics import poll_queue_depths, render_latest
from app.observability.tracing import setup_tracing
from app.parsing.exceptions import ParsingError
from app.security.middleware import (
    JWTAuthenticationMiddleware,
    PayloadEncryptionMiddleware,
    SecurityHeadersMiddleware,
    TenantRateLimitMiddleware,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

settings = get_settings()


@contextlib.asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """Starts two background tasks for this process's lifetime:

      * PolicyHotReloadSubscriber -- see that module's docstring for why
        it runs per-process rather than as one centralized service.
      * The hitl_review_queue_depth poller -- see
        app.observability.metrics's module docstring for why that gauge
        is pull-refreshed on an interval rather than maintained
        incrementally at every enqueue/dequeue call site.

    Both are constructed by calling the dependency-provider functions
    directly (not through FastAPI's `Depends`, which only resolves inside
    request handling) -- `get_opa_engine`/`get_policy_registry` take
    `settings` as a plain argument here instead of relying on their
    `Depends(get_settings)` defaults, exactly like any other direct call
    to them outside a route.
    """
    subscriber = PolicyHotReloadSubscriber(
        redis_client=get_redis_pool(),
        opa_engine=get_opa_engine(settings),
        policy_registry=get_policy_registry(settings),
        policy_cache=get_policy_cache(),
    )
    hot_reload_task = asyncio.create_task(subscriber.run(), name="policy-hot-reload-subscriber")

    queue_depth_stop = asyncio.Event()
    hitl_queue = HITLQueue(redis_client=get_redis_pool(), key_prefix=settings.hitl_key_prefix)
    queue_depth_task = asyncio.create_task(
        poll_queue_depths(
            redis_client=get_redis_pool(),
            hitl_pending_set_key=hitl_queue.pending_set_key,
            db_session_factory=get_session_factory(),
            interval_seconds=settings.metrics_queue_depth_poll_interval_seconds,
            stop_event=queue_depth_stop,
        ),
        name="hitl-queue-depth-poller",
    )

    try:
        yield
    finally:
        subscriber.stop()
        hot_reload_task.cancel()
        queue_depth_stop.set()
        queue_depth_task.cancel()
        for task in (hot_reload_task, queue_depth_task):
            with contextlib.suppress(asyncio.CancelledError):
                await task


app = FastAPI(
    title="SEBI Master Circular Parsing Service",
    description="Layout-aware, clause-hashed PDF parsing and Qdrant indexing for SEBI regulatory circulars.",
    version="1.0.0",
    lifespan=lifespan,
)

# Must run before any other instrumentation/middleware touches `app` --
# FastAPIInstrumentor patches the ASGI app's __call__ to wrap every
# request in a root span; everything added after this still nests inside
# it correctly, but instrumenting first is the documented-safe order.
setup_tracing(app, settings)

# Starlette runs middleware in the REVERSE of add_middleware() call order
# (last added = outermost = runs first on the way in). Added here (bottom
# to top) so the effective request path is:
#   SecurityHeaders -> JWTAuthentication -> TenantRateLimit -> PayloadEncryption -> route
# See app/security/middleware.py's module docstring for why that order.
app.add_middleware(PayloadEncryptionMiddleware, settings=settings)
app.add_middleware(TenantRateLimitMiddleware, settings=settings, redis_client=get_redis_pool())
app.add_middleware(JWTAuthenticationMiddleware, settings=settings)
app.add_middleware(SecurityHeadersMiddleware, settings=settings)

from app.api.webhook_routes import router as webhook_router

app.include_router(router)
app.include_router(execution_router)
app.include_router(ingestion_router)
app.include_router(auth_router)
app.include_router(hitl_review_router)
app.include_router(dlq_router)
app.include_router(sandbox_router)
app.include_router(analytics_router)
app.include_router(webhook_router)


@app.exception_handler(ParsingError)
async def parsing_error_handler(_: Request, exc: ParsingError) -> JSONResponse:
    logger.warning("Unhandled ParsingError reached top-level handler: %s", exc)
    return JSONResponse(status_code=500, content={"detail": str(exc)})


@app.get("/")
async def root() -> dict[str, str]:
    return {"service": settings.service_name, "status": "running"}


@app.get("/metrics", include_in_schema=False)
async def metrics() -> Response:
    """Prometheus scrape endpoint. Deliberately outside every security
    middleware's auth requirement (same tier as /healthz) -- a Prometheus
    server has no broker/officer/admin identity to present; restrict
    access at the network layer (Kubernetes NetworkPolicy / ingress
    allowlist) instead, not with an API token a scrape config would need
    to carry."""
    if not settings.metrics_enabled:
        return Response(status_code=404)
    body, content_type = render_latest()
    return Response(content=body, media_type=content_type)
