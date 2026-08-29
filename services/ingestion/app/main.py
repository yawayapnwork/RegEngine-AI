"""Ingestion service: pulls SEBI circulars, extracts layout-aware text
via Apache Tika, chunks it into clauses, and indexes the resulting
embeddings into Qdrant. This file is a scaffold -- wire it up to the
real extraction/chunking/indexing pipeline before deploying."""
from __future__ import annotations

import os

from fastapi import FastAPI

app = FastAPI(title="RegEngine AI - Ingestion Service", version="0.1.0")


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok", "service": "ingestion"}


@app.get("/readyz")
async def readyz() -> dict[str, str]:
    """Extend this to actually ping Tika and Qdrant before reporting ready."""
    return {
        "status": "ok",
        "tika_server_url": os.environ.get("TIKA_SERVER_URL", "not set"),
        "qdrant_url": os.environ.get("QDRANT_URL", "not set"),
    }
