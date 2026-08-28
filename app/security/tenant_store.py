"""Redis-backed registry of broker tenants' OAuth2 client_credentials --
one (client_id, client_secret) pair per broker, used to authenticate the
POST /v1/auth/token request that mints a Broker_API_Client access token.

Redis (not the main Postgres schema) because this mirrors
app.execution.policy_registry / app.execution.hitl_queue's existing
pattern: low-volume, low-latency, operationally simple key-value state
shared across every FastAPI/Celery worker. Client secrets are stored as
salted bcrypt hashes -- never plaintext, even in Redis.
"""
from __future__ import annotations

import bcrypt
import redis.asyncio as redis

from app.security.models import Role, TenantClient

# A fixed, never-matching bcrypt hash verified against on a lookup miss, so
# an unknown client_id takes the same code path (and roughly the same
# wall-clock time) as a wrong-secret hit -- see `authenticate` below.
_DUMMY_HASH = bcrypt.hashpw(b"dummy-constant-time-comparison", bcrypt.gensalt())


def _hash_secret(secret: str) -> str:
    return bcrypt.hashpw(secret.encode("utf-8"), bcrypt.gensalt()).decode("ascii")


def _verify_secret(secret: str, hashed: bytes | str) -> bool:
    hashed_bytes = hashed.encode("ascii") if isinstance(hashed, str) else hashed
    return bcrypt.checkpw(secret.encode("utf-8"), hashed_bytes)


class TenantClientStore:
    def __init__(self, redis_client: redis.Redis, key_prefix: str) -> None:
        self._redis = redis_client
        self._prefix = key_prefix

    def _key(self, client_id: str) -> str:
        return f"{self._prefix}:client:{client_id}"

    async def register(self, client_id: str, client_secret: str, tenant_id: str, roles: list[Role] | None = None) -> None:
        """Registers (or rotates the secret for) a broker tenant's client.
        Called from an operator-facing admin tool/script, never from a
        public endpoint -- there is deliberately no public "sign up a
        tenant" API in a compliance system like this one."""
        client = TenantClient(
            client_id=client_id,
            tenant_id=tenant_id,
            client_secret_hash=_hash_secret(client_secret),
            roles=roles or [Role.BROKER_API_CLIENT],
        )
        await self._redis.set(self._key(client_id), client.model_dump_json())

    async def authenticate(self, client_id: str, client_secret: str) -> TenantClient | None:
        """Returns the TenantClient iff client_id exists, is not disabled,
        and client_secret matches its stored hash. Returns None (never
        raises) on any failure -- constant-shape response so a caller
        cannot distinguish "unknown client_id" from "wrong secret" through
        a different exception path, which would leak which client_ids exist."""
        raw = await self._redis.get(self._key(client_id))
        if raw is None:
            # Still run a bcrypt comparison against the dummy hash so a
            # nonexistent client_id doesn't respond measurably faster than
            # a wrong-secret one (timing side-channel on client_id enumeration).
            _verify_secret(client_secret, _DUMMY_HASH)
            return None

        client = TenantClient.model_validate_json(raw)
        if client.disabled or not _verify_secret(client_secret, client.client_secret_hash):
            return None
        return client

    async def disable(self, client_id: str) -> None:
        raw = await self._redis.get(self._key(client_id))
        if raw is None:
            return
        client = TenantClient.model_validate_json(raw)
        client = client.model_copy(update={"disabled": True})
        await self._redis.set(self._key(client_id), client.model_dump_json())
