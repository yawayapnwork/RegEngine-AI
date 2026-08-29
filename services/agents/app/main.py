"""Agents service: dual-agent (Extraction + Logic Auditor) compliance
rule extraction via CrewAI, backed by Claude 3.5 Sonnet. This file is a
scaffold -- wire it up to the real CrewAI crew/task definitions before
deploying."""
from __future__ import annotations

import os

from fastapi import FastAPI

app = FastAPI(title="RegEngine AI - Agents Service", version="0.1.0")


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok", "service": "agents"}


@app.get("/readyz")
async def readyz() -> dict[str, str]:
    return {
        "status": "ok",
        "anthropic_api_key_configured": str(bool(os.environ.get("ANTHROPIC_API_KEY"))),
    }
