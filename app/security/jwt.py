"""JWT issuance and validation.

Trusted issuers, by design:

  1. Self-issued (`settings.jwt_issuer`) -- tokens this service itself
     mints for Broker_API_Client tenants via POST /v1/auth/token
     (client_credentials grant), and for human sessions bridged in from a
     SAML assertion (app.api.saml_routes). Signed with `jwt_algorithm` +
     `jwt_secret_key` (HS256) or `jwt_private_key_pem` (RS256), both
     resolved through `app.security.secrets` (see that module) rather than
     read as plain settings in production.
  2. External SSO -- one or more enterprise OIDC identity providers
     (Okta, Azure AD / Microsoft Entra ID, PingIdentity; registry built by
     app.security.sso_providers.build_sso_provider_registry) for human
     Compliance_Officer / System_Admin users. Verified via each IdP's own
     JWKS endpoint, never a locally-held secret -- this service never
     sees, and could not forge, a human's credentials. The IdP's `groups`
     claim is mapped to internal RBAC roles via
     app.security.directory_sync (Automated Directory Sync) BEFORE the
     claims are validated as a `TokenPayload` -- the roles a Principal
     ends up with are this service's own mapping decision, never the raw
     (and vocabulary-incompatible) claim an IdP happens to send.

`decode_access_token` picks the verification path from the token's `iss`
claim (checked BEFORE trusting anything else in it) and rejects any other
issuer outright. A token that claims to be from none of the configured
issuers is not "maybe still valid" -- it is a forgery attempt or a
misconfiguration, and both must fail closed.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import logging
import uuid
from functools import lru_cache

import jwt
from jwt import PyJWKClient

from app.config import Settings
from app.security.directory_sync import resolve_roles_from_groups
from app.security.models import Role, TokenPayload
from app.security.sso_providers import SSOProviderConfig, build_sso_provider_registry

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


def _normalize_external_claims(claims: dict, provider: SSOProviderConfig, settings: Settings) -> dict:
    """Transforms a verified-but-raw external IdP claim set into
    something `TokenPayload` will accept, applying Automated Directory
    Sync (Requirement 2) along the way. This is where an Okta/Azure AD/
    PingIdentity token's IdP-specific shape gets reconciled with this
    service's internal contract:

      - `roles`: computed from the IdP's group claim via
        app.security.directory_sync, NOT read from any `roles` claim the
        IdP might happen to send (an IdP has no concept of this
        application's RBAC vocabulary; trusting a claim named "roles"
        from it would mean trusting whatever arbitrary string a
        misconfigured app registration attached).
      - `aud`: normalized to a single string -- some IdPs (Azure AD v1
        tokens especially) can emit `aud` as a single-element list.
      - `jti`: many IdPs' ID tokens omit a `jti` claim entirely (it is not
        mandated by OIDC Core); synthesized deterministically from
        `sub`+`iat`+`iss` when absent so `Principal.token_id` is always
        populated for session/audit correlation, without ever inventing a
        value that could collide with the IdP's own `jti` if it later
        adds one.
    """
    groups = claims.get(provider.group_claim, []) or []
    if isinstance(groups, str):
        groups = [groups]
    roles = resolve_roles_from_groups(groups, settings.sso_directory_group_role_map)

    aud = claims.get("aud")
    if isinstance(aud, list):
        aud = aud[0] if aud else None

    jti = claims.get("jti")
    if not jti:
        jti = hashlib.sha256(f"{claims.get('iss')}|{claims.get('sub')}|{claims.get('iat')}".encode()).hexdigest()

    return {
        "sub": claims.get("sub"),
        "roles": [r.value for r in roles],
        "tenant_id": None,  # external SSO principals are always human (Compliance_Officer/System_Admin), never tenant-scoped
        "scope": claims.get("scope", "").split() if isinstance(claims.get("scope"), str) else (claims.get("scope") or []),
        "iss": claims.get("iss"),
        "aud": aud,
        "iat": claims.get("iat"),
        "exp": claims.get("exp"),
        "jti": jti,
        # Preserved for app.security.step_up's freshness check -- see
        # that module for why `auth_time`/`amr` (not just token exp) is
        # what step-up MFA actually gates on.
        "auth_time": claims.get("auth_time"),
        "amr": claims.get("amr", []) if isinstance(claims.get("amr"), list) else [],
    }


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
    provider_registry = build_sso_provider_registry(settings)

    try:
        if issuer == settings.jwt_issuer:
            claims = jwt.decode(
                token,
                local_verification_key,
                algorithms=[settings.jwt_algorithm],
                issuer=settings.jwt_issuer,
                audience=settings.jwt_audience,
            )
        elif issuer in provider_registry:
            provider = provider_registry[issuer]
            signing_key = _jwks_client(provider.jwks_url).get_signing_key_from_jwt(token)
            decode_kwargs = {"algorithms": list(provider.algorithms), "issuer": provider.issuer}
            if provider.audience:
                decode_kwargs["audience"] = provider.audience
            else:
                decode_kwargs["options"] = {"verify_aud": False}
            raw_claims = jwt.decode(token, signing_key.key, **decode_kwargs)
            claims = _normalize_external_claims(raw_claims, provider, settings)
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
