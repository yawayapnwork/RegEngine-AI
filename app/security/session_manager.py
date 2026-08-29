"""Session management: strict idle + absolute timeouts for human
(Compliance_Officer / System_Admin) principals, layered on top of the
existing bearer-JWT auth rather than introducing a second, separate
session-cookie mechanism.

Design: a "session" here is keyed by the authenticated token's own `jti`
(`Principal.token_id`), not a new client-visible session id -- the token
IS the session handle the client already holds and sends on every
request; this module just tracks, server-side in Redis, when that
token's session STARTED and was LAST USED, so it can be force-ended
before the token's own (possibly long, IdP-issued) `exp` if either
timeout is exceeded. This is what "strict session timeouts" means for an
enterprise SSO deployment in practice: the IdP token's lifetime is the
IdP's business, but how long an idle browser tab stays authenticated
against RegEngine specifically is this service's own policy, enforced
independently.

Broker_API_Client machine principals (`tenant_id is not None`) are
deliberately exempt -- see SessionManagementMiddleware's docstring.
"""
from __future__ import annotations

import datetime as dt
import logging

import redis.asyncio as redis

from app.config import Settings
from app.security.models import Principal

logger = logging.getLogger(__name__)


class SessionExpiredError(Exception):
    def __init__(self, reason: str) -> None:
        self.reason = reason  # "idle_timeout" | "absolute_timeout"
        super().__init__(reason)


class SessionManager:
    def __init__(self, redis_client: redis.Redis, key_prefix: str) -> None:
        self._redis = redis_client
        self._prefix = key_prefix

    def _key(self, token_id: str) -> str:
        return f"{self._prefix}:{token_id}"

    async def touch_or_create(self, principal: Principal, settings: Settings) -> None:
        """Called on every authenticated request for a human principal.
        Creates a session record on first use of a given token, or
        validates + refreshes an existing one's idle timer. Raises
        `SessionExpiredError` if either timeout has been exceeded --
        the caller (SessionManagementMiddleware) is responsible for
        turning that into a 401.
        """
        now = dt.datetime.now(dt.timezone.utc)
        key = self._key(principal.token_id)
        raw = await self._redis.hgetall(key)

        if not raw:
            # `subject` is stored (not just created_at/last_activity_at)
            # so app.security.directory_sync_job can discover which
            # subjects currently have a live session without this system
            # needing a separate local Users table -- see that module's
            # docstring.
            await self._redis.hset(
                key, mapping={"subject": principal.subject, "created_at": now.isoformat(), "last_activity_at": now.isoformat()}
            )
            await self._redis.expire(key, settings.session_absolute_timeout_seconds)
            return

        created_at = dt.datetime.fromisoformat(raw["created_at"])
        last_activity_at = dt.datetime.fromisoformat(raw["last_activity_at"])

        if (now - created_at).total_seconds() > settings.session_absolute_timeout_seconds:
            await self._redis.delete(key)
            raise SessionExpiredError("absolute_timeout")
        if (now - last_activity_at).total_seconds() > settings.session_idle_timeout_seconds:
            await self._redis.delete(key)
            raise SessionExpiredError("idle_timeout")

        await self._redis.hset(key, "last_activity_at", now.isoformat())
        # Refresh the Redis TTL to the remaining absolute-timeout budget
        # (not the full window again) -- the key must still expire no
        # later than `created_at + absolute_timeout` even if activity
        # continues right up to that boundary.
        remaining = settings.session_absolute_timeout_seconds - int((now - created_at).total_seconds())
        await self._redis.expire(key, max(1, remaining))

    async def revoke(self, token_id: str) -> None:
        """Explicit logout -- ends the session immediately regardless of
        either timeout. The underlying JWT itself remains cryptographically
        valid until its own `exp` (this service has no token-revocation
        list beyond this session record), so a client MUST discard the
        token client-side on logout too; this only prevents further use
        of it against a server that still enforces session state."""
        await self._redis.delete(self._key(token_id))
