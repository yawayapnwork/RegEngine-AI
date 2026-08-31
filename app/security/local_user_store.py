"""Redis-backed registry of locally-provisioned human accounts --
Compliance_Officer/System_Admin users who authenticate directly against
this service (email + password) rather than via an external SSO IdP.

Mirrors app.security.tenant_store's shape exactly (bcrypt-hashed secret,
constant-shape response on a lookup miss so an unknown email doesn't
respond measurably faster than a wrong-password one) but keyed by email
for human principals instead of client_id for machine ones.
"""
from __future__ import annotations

import bcrypt
import redis.asyncio as redis

from app.security.models import LocalUser, Role

# A fixed, never-matching bcrypt hash verified against on a lookup miss --
# see TenantClientStore's identical convention in app.security.tenant_store.
_DUMMY_HASH = bcrypt.hashpw(b"dummy-constant-time-comparison", bcrypt.gensalt())


def _hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("ascii")


def _verify_password(password: str, hashed: bytes | str) -> bool:
    hashed_bytes = hashed.encode("ascii") if isinstance(hashed, str) else hashed
    return bcrypt.checkpw(password.encode("utf-8"), hashed_bytes)


class LocalUserStore:
    def __init__(self, redis_client: redis.Redis, key_prefix: str) -> None:
        self._redis = redis_client
        self._prefix = key_prefix

    def _key(self, email: str) -> str:
        return f"{self._prefix}:user:{email.strip().lower()}"

    async def register(self, email: str, password: str, roles: list[Role] | None = None) -> None:
        """Creates (or resets the password for) a local account. Called from
        an operator-facing admin route/script -- see app.api.auth_routes'
        POST /v1/auth/users, gated to System_Admin -- not a public
        self-service signup, same "no public signup" convention
        app.security.tenant_store documents for broker clients."""
        user = LocalUser(
            email=email.strip().lower(),
            password_hash=_hash_password(password),
            roles=roles or [Role.COMPLIANCE_OFFICER],
        )
        await self._redis.set(self._key(email), user.model_dump_json())

    async def authenticate(self, email: str, password: str) -> LocalUser | None:
        """Returns the LocalUser iff email exists, is not disabled, and
        password matches its stored hash. Returns None (never raises) on
        any failure -- see app.security.tenant_store.TenantClientStore
        .authenticate's docstring on why this shape matters."""
        raw = await self._redis.get(self._key(email))
        if raw is None:
            _verify_password(password, _DUMMY_HASH)
            return None

        user = LocalUser.model_validate_json(raw)
        if user.disabled or not _verify_password(password, user.password_hash):
            return None
        return user

    async def disable(self, email: str) -> None:
        raw = await self._redis.get(self._key(email))
        if raw is None:
            return
        user = LocalUser.model_validate_json(raw)
        user = user.model_copy(update={"disabled": True})
        await self._redis.set(self._key(email), user.model_dump_json())
