"""Audit service: append-only, SHA-256 hash-chained compliance ledger
on PostgreSQL. This file is a scaffold -- wire it up to the real ledger
service (append/verify) before deploying."""
from __future__ import annotations

import os

from fastapi import FastAPI

app = FastAPI(title="RegEngine AI - Audit Service", version="0.1.0")


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok", "service": "audit"}


@app.get("/readyz")
async def readyz() -> dict[str, str]:
    return {
        "status": "ok",
        "ledger_database_configured": str(bool(os.environ.get("LEDGER_DATABASE_URL"))),
    }
