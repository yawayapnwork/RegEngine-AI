"""FastAPI application entrypoint."""
from __future__ import annotations

import logging

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from starlette.requests import Request

from app.api.routes import router
from app.config import get_settings
from app.parsing.exceptions import ParsingError

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

settings = get_settings()

app = FastAPI(
    title="SEBI Master Circular Parsing Service",
    description="Layout-aware, clause-hashed PDF parsing and Qdrant indexing for SEBI regulatory circulars.",
    version="1.0.0",
)
app.include_router(router)


@app.exception_handler(ParsingError)
async def parsing_error_handler(_: Request, exc: ParsingError) -> JSONResponse:
    logger.warning("Unhandled ParsingError reached top-level handler: %s", exc)
    return JSONResponse(status_code=500, content={"detail": str(exc)})


@app.get("/")
async def root() -> dict[str, str]:
    return {"service": settings.service_name, "status": "running"}
