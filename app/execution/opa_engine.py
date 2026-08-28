"""Thin async client for a co-located OPA server (`opa run --server`).

Why a server instead of shelling out to `opa eval` per request: the sync
`/v1/execution/transactions/evaluate` endpoint must "return instantly", and
spawning a subprocess (+ re-parsing every .rego module in the bundle) per
call adds tens of milliseconds and doesn't scale under concurrency. A
persistent OPA server keeps compiled policies resident in memory and
answers over a loopback/sidecar HTTP call in low single-digit milliseconds,
and its Policy API supports hot-reloading a single module without
restarting or reloading the rest of the bundle.
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

from app.compiler.models import CompiledRego
from app.observability.metrics import observe_opa_evaluation
from app.observability.tracing import traced_span

logger = logging.getLogger(__name__)


class OPAEngineError(RuntimeError):
    """Raised when OPA is unreachable or returns a non-2xx response."""


class OPAEngine:
    def __init__(self, base_url: str, timeout_seconds: float) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_seconds

    async def publish_policy(self, compiled: CompiledRego) -> None:
        """PUT the Rego source under its rule_id. OPA compiles and hot-swaps
        it atomically; existing in-flight evaluations are unaffected."""
        url = f"{self._base_url}/v1/policies/{compiled.rule_id}"
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.put(url, content=compiled.rego_code.encode("utf-8"), headers={"Content-Type": "text/plain"})
        if resp.status_code >= 300:
            raise OPAEngineError(f"OPA rejected policy '{compiled.rule_id}': {resp.status_code} {resp.text}")

    async def remove_policy(self, rule_id: str) -> None:
        url = f"{self._base_url}/v1/policies/{rule_id}"
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.delete(url)
        if resp.status_code not in (200, 204, 404):
            raise OPAEngineError(f"OPA rejected removal of '{rule_id}': {resp.status_code} {resp.text}")

    async def evaluate(self, package: str, input_doc: dict[str, Any]) -> dict[str, Any] | None:
        """Query `data.<package>.decision`. Returns None when OPA reports the
        result as undefined (e.g. a threshold condition referenced a `facts`
        key that was absent from `input_doc`) — the caller must treat that as
        ambiguous, never as an implicit allow or deny.

        Every call is traced and timed into opa_policy_evaluation_duration_seconds
        (labeled by outcome), regardless of caller -- the single choke point
        both the synchronous /transactions/evaluate path and the Celery
        batch/CDC path evaluate through."""
        outcome: dict[str, str] = {"outcome": "error"}
        with traced_span("opa.evaluate", package=package), observe_opa_evaluation(outcome):
            path = package.replace(".", "/")
            url = f"{self._base_url}/v1/data/{path}/decision"
            try:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    resp = await client.post(url, json={"input": input_doc})
            except httpx.HTTPError as exc:
                raise OPAEngineError(f"OPA request failed for package '{package}': {exc}") from exc

            if resp.status_code != 200:
                raise OPAEngineError(f"OPA returned {resp.status_code} for package '{package}': {resp.text}")

            body = resp.json()
            result = body.get("result")
            outcome["outcome"] = "undefined" if result is None else ("deny" if result.get("violations") else "allow")
            return result
