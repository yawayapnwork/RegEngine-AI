"""JWT issuance and validation.

Two trusted issuers, by design:

  1. Self-issued (`settings.jwt_issuer`) -- tokens this service itself
     mints for Broker_API_Client tenants via POST /v1/auth/token
     (client_credentials grant). Signed with `jwt_algorithm` +
     `jwt_secret_key` (HS256) or `jwt_private_key_pem` (RS256), both
     resolved through `app.security.secrets` (see that module) rather than
     read as plain settings in production.
  2. External SSO (`settings.jwt_external_issuer`) -- tokens issued by the
     enterprise identity provider (Okta/Azure AD/Keycloak/...) for human
     Compliance_Officer / System_Admin users. Verified via that IdP's JWKS
     endpoint (`settings.jwt_jwks_url`), never a locally-held secret --
     this service never sees, and could not forge, a human's credentials.

`decode_access_token` picks the verification path from the token's `iss`
claim (checked BEFORE trusting anything else in it) and rejects any other
issuer outright. A token that claims to be from neither issuer is not
"maybe still valid" -- it is a forgery attempt or a misconfiguration, and
both must fail closed.
"""
from __future__ import annotations

import datetime as dt
import logging
import uuid
from functools import lru_cache

import jwt
from jwt import PyJWKClient

from app.config import Settings
from app.security.models import Role, TokenPayload

logger = logging.getLogger(__name__)


class TokenError(Exception):
    """Base class for every token issuance/validation failure. Callers
    (the auth dependency) map this to HTTP 401 uniformly -- the specific
    reason (expired vs. forged vs. wrong issuer) is logged server-side but
    never distinguished in the client-facing response, so a token-guessing
    attacker learns nothing from the failure mode."""


class TokenExpiredError(TokenError):
    pass


class TokenInvalidError(TokenError):
    pass


@lru_cache(maxsize=4)
def _jwks_client(jwks_url: str) -> PyJWKClient:
    # PyJWKClient caches fetched keys internally (lifespan configurable);
    # this lru_cache just avoids constructing a new client (and therefore
    # a fresh, empty key cache) on every request.
    return PyJWKClient(jwks_url, cache_keys=True, lifespan=3600)


def create_access_token(
    *,
    subject: str,
    roles: list[Role],
    settings: Settings,
    signing_key: str,
    tenant_id: str | None = None,
    scope: list[str] | None = None,
    ttl_seconds: int | None = None,
) -> tuple[str, TokenPayload]:
    """Mints a self-issued access token. `signing_key` is the resolved
    HS256 secret or RS256 private key PEM -- the caller (app.api.auth_routes)
    fetches it via app.security.secrets, never reads settings.jwt_secret_key
    directly, so key material always flows through one auditable path."""
    now = dt.datetime.now(dt.timezone.utc)
    ttl = ttl_seconds or settings.jwt_access_token_ttl_seconds
    payload = TokenPayload(
        sub=subject,
        roles=roles,
        tenant_id=tenant_id,
        scope=scope or [],
        iss=settings.jwt_issuer,
        aud=settings.jwt_audience,
        iat=now,
        exp=now + dt.timedelta(seconds=ttl),
        jti=str(uuid.uuid4()),
    )
    # Built manually rather than payload.model_dump(mode="json"): that mode
    # serializes iat/exp to ISO-8601 strings, but PyJWT's encoder requires
    # raw datetime objects (or POSIX timestamps) for those two claims --
    # it special-cases them internally to produce numeric `iat`/`exp`
    # values per RFC 7519 §4.1.4/§4.1.6. A JSON-string `iat` round-trips
    # through encode() unchanged and then fails PyJWT's own decode-side
    # "claim must be an integer" check.
    claims = {
        "sub": payload.sub,
        "roles": [r.value for r in payload.roles],
        "tenant_id": payload.tenant_id,
        "scope": payload.scope,
        "iss": payload.iss,
        "aud": payload.aud,
        "iat": payload.iat,
        "exp": payload.exp,
        "jti": payload.jti,
    }
    encoded = jwt.encode(claims, signing_key, algorithm=settings.jwt_algorithm)
    return encoded, payload


def decode_access_token(token: str, settings: Settings, *, local_verification_key: str) -> TokenPayload:
    """Validates signature, issuer, audience, and expiry, then parses
    claims through `TokenPayload` (so a structurally-invalid claim set --
    e.g. a Broker_API_Client token missing tenant_id -- is rejected exactly
    like a bad signature, not accepted and left for a route handler to
    discover later). Raises `TokenExpiredError` / `TokenInvalidError` on
    any failure; never returns a partially-trusted payload.
    """
    try:
        unverified = jwt.decode(token, options={"verify_signature": False})
    except jwt.exceptions.DecodeError as exc:
        raise TokenInvalidError("Malformed token.") from exc

    issuer = unverified.get("iss")

    try:
        if issuer == settings.jwt_issuer:
            claims = jwt.decode(
                token,
                local_verification_key,
                algorithms=[settings.jwt_algorithm],
                issuer=settings.jwt_issuer,
                audience=settings.jwt_audience,
            )
        elif settings.jwt_jwks_url and issuer == settings.jwt_external_issuer:
            signing_key = _jwks_client(settings.jwt_jwks_url).get_signing_key_from_jwt(token)
            claims = jwt.decode(
                token,
                signing_key.key,
                algorithms=settings.jwt_external_algorithms,
                issuer=settings.jwt_external_issuer,
                audience=settings.jwt_audience,
            )
        else:
            raise TokenInvalidError(f"Unrecognized token issuer: {issuer!r}")
    except jwt.exceptions.ExpiredSignatureError as exc:
        raise TokenExpiredError("Token has expired.") from exc
    except jwt.exceptions.PyJWKClientError as exc:
        logger.warning("JWKS lookup failed for issuer %r: %s", issuer, exc)
        raise TokenInvalidError("Unable to verify token signature.") from exc
    except jwt.exceptions.InvalidTokenError as exc:
        raise TokenInvalidError(f"Invalid token: {exc}") from exc

    try:
        return TokenPayload.model_validate(claims)
    except Exception as exc:  # noqa: BLE001 - any claim-shape violation is an invalid token, not a 500
        raise TokenInvalidError(f"Token claims failed validation: {exc}") from exc
