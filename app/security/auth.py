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
from app.security.models import Principal
from app.security.secrets import resolve_secret

logger = logging.getLogger(__name__)


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

    return Principal(
        subject=payload.sub,
        roles=payload.roles,
        tenant_id=payload.tenant_id,
        token_id=payload.jti,
    )
