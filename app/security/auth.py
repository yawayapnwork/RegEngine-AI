"""The single, shared token-to-Principal authentication path -- used by
both `JWTAuthenticationMiddleware` (which authenticates opportunistically,
for every request, to support tenant-aware rate limiting even on routes
that don't require auth) and `get_current_principal`
(`app.security.dependencies`, which enforces it). Keeping exactly one
implementation means the two can never quietly drift apart on what counts
as a valid token.
"""
from __future__ import annotations

import asyncio
import logging

from app.config import Settings
from app.security.jwt import TokenError, decode_access_token
from app.security.models import Principal, Role
from app.security.secrets import resolve_secret

logger = logging.getLogger(__name__)


async def _directory_sync_override(subject: str, settings: Settings) -> list[Role] | None:
    """Consults the proactive-revocation cache app.security.directory_sync_job
    writes (Automated Directory Sync's continuous half -- see that
    module's docstring). Returns None (no override, use the token's own
    claims-derived roles unchanged) whenever Redis is unreachable or no
    override is cached for this subject -- a cache-layer outage must
    degrade to "trust the token's claims as issued," never to "deny
    everyone" or block login entirely."""
    from app.execution.dependencies import get_redis_pool  # deferred: avoids a hard app.execution import for callers (e.g. unit tests) that never exercise the SSO/directory-sync path

    try:
        raw = await asyncio.wait_for(get_redis_pool().get(f"{settings.directory_sync_override_key_prefix}:{subject}"), timeout=0.5)
    except Exception:  # noqa: BLE001 - see docstring: a cache outage must not block authentication
        logger.warning("Directory-sync override lookup failed for subject=%s; using token-claimed roles as-is.", subject, exc_info=True)
        return None

    if raw is None:
        return None
    try:
        return [Role(v) for v in raw.split(",") if v]
    except ValueError:
        logger.error("Malformed directory-sync override cached for subject=%s: %r", subject, raw)
        return None


async def _local_verification_key(settings: Settings) -> str:
    """The key this service verifies its OWN self-issued tokens with: the
    HS256 shared secret, or the RS256 public key when jwt_algorithm is
    RS256. Resolved via app.security.secrets (never a raw setting read) so
    swapping SECRETS_BACKEND changes where this comes from with no other
    code change. Secrets-backend calls (AWS/Vault SDKs) are synchronous;
    offloaded to a thread so a cache-miss lookup never blocks the event loop.
    """
    secret_name = "jwt_secret_key" if settings.jwt_algorithm.startswith("HS") else "jwt_public_key_pem"
    return await asyncio.to_thread(resolve_secret, secret_name, settings=settings)


async def authenticate_token(token: str, settings: Settings) -> Principal | None:
    """Validates `token` and returns the resulting Principal, or None if
    the token is missing/invalid/expired. Never raises -- callers decide
    what "no valid principal" means for their context (401 for a
    protected route via `get_current_principal`; "treat as anonymous, rate
    limit by IP" for the middleware)."""
    if not token:
        return None

    try:
        local_key = await _local_verification_key(settings)
        payload = decode_access_token(token, settings, local_verification_key=local_key)
    except TokenError as exc:
        logger.info("Token rejected: %s", exc)
        return None

    roles = payload.roles
    if payload.tenant_id is None:  # only external SSO / human principals go through directory sync
        override = await _directory_sync_override(payload.sub, settings)
        if override is not None:
            if not override:
                logger.warning("Directory-sync override for subject=%s revokes all roles; rejecting token.", payload.sub)
                return None
            roles = override

    return Principal(
        subject=payload.sub,
        roles=roles,
        tenant_id=payload.tenant_id,
        token_id=payload.jti,
        auth_time=payload.auth_time,
        amr=payload.amr,
    )
