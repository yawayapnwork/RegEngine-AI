"""FastAPI application entrypoint."""
from __future__ import annotations

import logging

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from starlette.requests import Request

from app.api.auth_routes import router as auth_router
from app.api.execution_routes import router as execution_router
from app.api.hitl_review_routes import router as hitl_review_router
from app.api.ingestion_routes import router as ingestion_router
from app.api.routes import router
from app.config import get_settings
from app.execution.dependencies import get_redis_pool
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

app = FastAPI(
    title="SEBI Master Circular Parsing Service",
    description="Layout-aware, clause-hashed PDF parsing and Qdrant indexing for SEBI regulatory circulars.",
    version="1.0.0",
)

# Starlette runs middleware in the REVERSE of add_middleware() call order
# (last added = outermost = runs first on the way in). Added here (bottom
# to top) so the effective request path is:
#   SecurityHeaders -> JWTAuthentication -> TenantRateLimit -> PayloadEncryption -> route
# See app/security/middleware.py's module docstring for why that order.
app.add_middleware(PayloadEncryptionMiddleware, settings=settings)
app.add_middleware(TenantRateLimitMiddleware, settings=settings, redis_client=get_redis_pool())
app.add_middleware(JWTAuthenticationMiddleware, settings=settings)
app.add_middleware(SecurityHeadersMiddleware, settings=settings)

app.include_router(router)
app.include_router(execution_router)
app.include_router(ingestion_router)
app.include_router(auth_router)
app.include_router(hitl_review_router)


@app.exception_handler(ParsingError)
async def parsing_error_handler(_: Request, exc: ParsingError) -> JSONResponse:
    logger.warning("Unhandled ParsingError reached top-level handler: %s", exc)
    return JSONResponse(status_code=500, content={"detail": str(exc)})


@app.get("/")
async def root() -> dict[str, str]:
    return {"service": settings.service_name, "status": "running"}
