"""Outbound webhook delivery to OMS/RMS/broker callback URLs.

Signing lets a receiving OMS/RMS verify a decision notification actually
came from RegEngine AI and was not forged/replayed by a third party sitting
on the same network as the legacy back-office systems this integrates with.
"""
from __future__ import annotations

import hashlib
import hmac
import logging

import httpx

from app.execution.models import WebhookEvent

logger = logging.getLogger(__name__)

SIGNATURE_HEADER = "X-RegEngine-Signature-256"


def sign_payload(body: bytes, secret: str) -> str:
    return "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def send_webhook_sync(url: str, event: WebhookEvent, secret: str | None, timeout_seconds: float) -> httpx.Response:
    """Synchronous send, used from Celery tasks (Celery workers run outside
    an asyncio event loop by default)."""
    body = event.model_dump_json().encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if secret:
        headers[SIGNATURE_HEADER] = sign_payload(body, secret)

    with httpx.Client(timeout=timeout_seconds) as client:
        resp = client.post(url, content=body, headers=headers)
    resp.raise_for_status()
    return resp


async def send_webhook_async(url: str, event: WebhookEvent, secret: str | None, timeout_seconds: float) -> httpx.Response:
    """Async send, used from the FastAPI event loop when the caller cannot
    wait for a Celery round-trip (e.g. dispatch fired inline after a sync
    /evaluate call decides DENY and an OMS wants immediate notification)."""
    body = event.model_dump_json().encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if secret:
        headers[SIGNATURE_HEADER] = sign_payload(body, secret)

    async with httpx.AsyncClient(timeout=timeout_seconds) as client:
        resp = await client.post(url, content=body, headers=headers)
    resp.raise_for_status()
    return resp
