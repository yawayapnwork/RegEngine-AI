"""Twilio SMS client via direct REST calls (no `twilio` SDK dependency --
this is one endpoint, Basic Auth, form-encoded body; pulling in the full
SDK for that is not worth the extra dependency weight).

https://www.twilio.com/docs/sms/api/message-resource#create-a-message-resource
"""
from __future__ import annotations

import logging

import httpx

logger = logging.getLogger(__name__)


class TwilioClientError(RuntimeError):
    pass


class TwilioClient:
    def __init__(self, account_sid: str, auth_token: str, from_number: str, api_base_url: str, timeout_seconds: float = 10.0) -> None:
        self._account_sid = account_sid
        self._auth_token = auth_token
        self._from_number = from_number
        self._base_url = api_base_url.rstrip("/")
        self._timeout = timeout_seconds

    async def send_sms(self, to_number: str, body: str) -> None:
        url = f"{self._base_url}/Accounts/{self._account_sid}/Messages.json"
        try:
            async with httpx.AsyncClient(timeout=self._timeout, auth=(self._account_sid, self._auth_token)) as client:
                resp = await client.post(url, data={"From": self._from_number, "To": to_number, "Body": body})
            if resp.status_code >= 300:
                raise TwilioClientError(f"Twilio API returned {resp.status_code}: {resp.text}")
            logger.info("SMS dispatched to %s via Twilio.", to_number)
        except httpx.HTTPError as exc:
            raise TwilioClientError(f"Twilio API request failed: {exc}") from exc

    async def send_to_oncall(self, oncall_numbers: list[str], body: str) -> dict[str, bool]:
        """Best-effort fan-out: one on-call officer's bad/unreachable
        number must not prevent the SMS reaching the others."""
        results: dict[str, bool] = {}
        for number in oncall_numbers:
            try:
                await self.send_sms(number, body)
                results[number] = True
            except TwilioClientError:
                logger.exception("Failed to SMS on-call number %s.", number)
                results[number] = False
        return results
