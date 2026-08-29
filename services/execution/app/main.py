"""Execution service: evaluates live broker transactions against
compiled OPA policy (server or Wasm-embedded), backed by Redis for the
policy registry and HITL queue. This file is a scaffold -- wire it up
to the real evaluator before deploying."""
from __future__ import annotations

import os

from fastapi import FastAPI

app = FastAPI(title="RegEngine AI - Execution Service", version="0.1.0")


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok", "service": "execution"}


@app.get("/readyz")
async def readyz() -> dict[str, str]:
    return {
        "status": "ok",
        "redis_url": os.environ.get("REDIS_URL", "not set"),
        "opa_server_url": os.environ.get("OPA_SERVER_URL", "not set"),
    }
