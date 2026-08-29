"""PagerDuty Events API v2 client (https://developer.pagerduty.com/api-reference/9d0b4b12e36f9-send-an-event-to-pager-duty).

Uses `dedup_key = event_id` for every call -- trigger/acknowledge/resolve
against the SAME dedup_key are how PagerDuty's own incident lifecycle
stays in sync with app.incident.models.BreachEvent's lifecycle: a
`resolve` call here closes the SAME PagerDuty incident the earlier
`trigger` call opened, rather than creating a new one.
"""
from __future__ import annotations

import logging

import httpx

logger = logging.getLogger(__name__)


class PagerDutyClientError(RuntimeError):
    pass


class PagerDutyClient:
    def __init__(self, routing_key: str, api_base_url: str, timeout_seconds: float = 10.0) -> None:
        self._routing_key = routing_key
        self._base_url = api_base_url.rstrip("/")
        self._timeout = timeout_seconds

    async def trigger(
        self,
        *,
        dedup_key: str,
        summary: str,
        severity: str,  # PagerDuty's own vocabulary: "critical" | "error" | "warning" | "info"
        source: str,
        custom_details: dict | None = None,
    ) -> None:
        payload = {
            "routing_key": self._routing_key,
            "event_action": "trigger",
            "dedup_key": dedup_key,
            "payload": {
                "summary": summary,
                "severity": severity,
                "source": source,
                "custom_details": custom_details or {},
            },
        }
        await self._send(payload)

    async def acknowledge(self, dedup_key: str) -> None:
        await self._send({"routing_key": self._routing_key, "event_action": "acknowledge", "dedup_key": dedup_key})

    async def resolve(self, dedup_key: str) -> None:
        await self._send({"routing_key": self._routing_key, "event_action": "resolve", "dedup_key": dedup_key})

    async def _send(self, payload: dict) -> None:
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(f"{self._base_url}/enqueue", json=payload)
            if resp.status_code >= 300:
                raise PagerDutyClientError(f"PagerDuty Events API returned {resp.status_code}: {resp.text}")
            logger.info("PagerDuty event_action=%s dedup_key=%s accepted.", payload["event_action"], payload["dedup_key"])
        except httpx.HTTPError as exc:
            raise PagerDutyClientError(f"PagerDuty Events API request failed: {exc}") from exc
