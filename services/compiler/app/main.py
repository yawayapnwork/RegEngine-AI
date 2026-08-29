"""Compiler service: turns an audited, extracted compliance rule into
an OPA Rego policy module (and/or a JSON-Logic fallback AST). This file
is a scaffold -- wire it up to the real Rego/JSON-Logic compiler before
deploying."""
from __future__ import annotations

from fastapi import FastAPI

app = FastAPI(title="RegEngine AI - Compiler Service", version="0.1.0")


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok", "service": "compiler"}
