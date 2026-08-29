"""Plain SMTP email client for the escalation engine's email stage.

Uses stdlib `smtplib`/`email.message` rather than a third-party mail SDK
-- one outbound transactional message per call, no templates/tracking
needed, and the stdlib client works against any SMTP relay (SES, SendGrid
SMTP, an internal relay) without an extra dependency per provider.
`smtplib` is synchronous, so the actual send is offloaded to a worker
thread (this client is only ever called from Celery task context here
anyway, never from the FastAPI event loop, but `asyncio.to_thread` keeps
it safe either way).
"""
from __future__ import annotations

import asyncio
import logging
import smtplib
from email.message import EmailMessage

logger = logging.getLogger(__name__)


class EmailClientError(RuntimeError):
    pass


class EmailClient:
    def __init__(
        self,
        host: str,
        port: int,
        username: str | None,
        password: str | None,
        use_tls: bool,
        from_address: str,
        timeout_seconds: float = 15.0,
    ) -> None:
        self._host = host
        self._port = port
        self._username = username
        self._password = password
        self._use_tls = use_tls
        self._from_address = from_address
        self._timeout = timeout_seconds

    def _send_sync(self, to_addresses: list[str], subject: str, body: str) -> None:
        message = EmailMessage()
        message["From"] = self._from_address
        message["To"] = ", ".join(to_addresses)
        message["Subject"] = subject
        message.set_content(body)

        with smtplib.SMTP(self._host, self._port, timeout=self._timeout) as smtp:
            if self._use_tls:
                smtp.starttls()
            if self._username and self._password:
                smtp.login(self._username, self._password)
            smtp.send_message(message)

    async def send(self, to_addresses: list[str], subject: str, body: str) -> None:
        if not to_addresses:
            logger.warning("EmailClient.send called with no recipients; skipping (subject=%r).", subject)
            return
        try:
            await asyncio.to_thread(self._send_sync, to_addresses, subject, body)
            logger.info("Escalation email '%s' sent to %s.", subject, to_addresses)
        except (smtplib.SMTPException, OSError) as exc:
            raise EmailClientError(f"SMTP send failed: {exc}") from exc
