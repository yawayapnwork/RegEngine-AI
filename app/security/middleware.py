"""FastAPI/Starlette middleware stack: security headers + HTTPS enforcement,
opportunistic JWT authentication, session-timeout enforcement, per-tenant
rate limiting, and optional application-layer payload encryption.

Mount order matters -- Starlette runs middleware in the REVERSE of the
order passed to `add_middleware` (last added = outermost = runs first on
the way in). `app/main.py` adds them so the effective request path is:

    SecurityHeaders -> JWTAuthentication -> SessionManagement -> TenantRateLimit -> PayloadEncryption -> route

JWTAuthentication runs before SessionManagement deliberately: session
timeout enforcement only makes sense for an already-authenticated human
principal (`request.state.principal`) -- SessionManagement reads that,
and downgrades it back to None (ending the request's authentication) if
the session's idle or absolute timeout has been exceeded, exactly as if
the token itself were invalid. SessionManagement runs before
TenantRateLimit for the same reason JWTAuthentication does: a
session-timed-out request must be keyed by IP for rate-limiting purposes,
not treated as still-authenticated. PayloadEncryption runs last (closest
to the route) since decrypting the body is only meaningful once a request
has passed authentication, session validation, and rate limiting; it also
needs `request.state.principal.tenant_id` to pick the right decryption key,
which is available by then too. Security headers wrap everything,
including error responses from any of the inner three.
"""
from __future__ import annotations

import logging
import time

import redis.asyncio as redis
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp

from app.config import Settings
from app.security.auth import authenticate_token
from app.security.crypto import PayloadDecryptionError, decrypt_payload, encrypt_payload
from app.security.secrets import resolve_secret
from app.security.session_manager import SessionExpiredError, SessionManager

logger = logging.getLogger(__name__)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Adds standard hardening headers to every response and, when
    `settings.enforce_https` is on, rejects requests that did not arrive
    over TLS. TLS itself is terminated upstream (ingress/load balancer --
    see helm/regengine-ai's nginx-ingress annotations); this checks the
    `X-Forwarded-Proto` header that terminator sets, since Starlette's own
    `request.url.scheme` is always "http" for traffic reaching this
    process directly on its ClusterIP Service."""

    def __init__(self, app: ASGIApp, settings: Settings) -> None:
        super().__init__(app)
        self._settings = settings

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        # Same exemption list as rate limiting: infra/public paths
        # (/healthz especially -- kubelet/Docker probes hit it over plain
        # HTTP even when real traffic requires TLS) never need HTTPS enforced.
        if self._settings.enforce_https and request.url.path not in self._settings.rate_limit_exempt_paths:
            forwarded_proto = request.headers.get("x-forwarded-proto", request.url.scheme)
            if forwarded_proto != "https":
                return JSONResponse(
                    status_code=400,
                    content={"detail": "HTTPS required. This endpoint does not accept plaintext HTTP."},
                )

        response = await call_next(request)
        response.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains; preload"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = response.headers.get("Cache-Control", "no-store")
        return response


class JWTAuthenticationMiddleware(BaseHTTPMiddleware):
    """Authenticates OPPORTUNISTICALLY: attaches `request.state.principal`
    (a Principal or None) for every request, but never itself rejects one.
    Enforcement -- 401 for a missing/invalid token, 403 for a wrong role --
    is `app.security.dependencies.get_current_principal` /
    `require_roles(...)`'s job, applied per-route. Splitting it this way
    is what lets truly public routes (/healthz, /docs, POST /v1/auth/token)
    require no dependency at all while `TenantRateLimitMiddleware`
    (mounted inside this one -- see module docstring) can still key an
    authenticated caller's rate limit by tenant_id instead of IP.
    """

    def __init__(self, app: ASGIApp, settings: Settings) -> None:
        super().__init__(app)
        self._settings = settings

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        request.state.principal = None
        auth_header = request.headers.get("authorization", "")
        if auth_header.lower().startswith("bearer "):
            token = auth_header[len("bearer "):].strip()
            request.state.principal = await authenticate_token(token, self._settings)
        return await call_next(request)


class SessionManagementMiddleware(BaseHTTPMiddleware):
    """Enforces strict idle + absolute session timeouts (Requirement 3)
    for human principals, on top of whatever (possibly long) lifetime the
    IdP issued the underlying token with -- see
    app.security.session_manager's module docstring for the full design.

    Broker_API_Client (machine) principals are exempt: a tenant_id-scoped
    token represents a service credential re-authenticated per
    client_credentials call (see POST /v1/auth/token), not an interactive
    human session with an "idle browser tab" concept -- applying an idle
    timeout to machine-to-machine traffic would just be an arbitrary
    additional token-refresh requirement with no real security benefit
    for that trust boundary (see app.security.models.Role's module
    docstring on the three distinct trust boundaries this system has).

    Runs only when `request.state.principal` is already set (by
    JWTAuthenticationMiddleware, mounted outside this one) -- an
    unauthenticated request has no session to manage and passes through
    untouched, exactly as it does today.
    """

    def __init__(self, app: ASGIApp, settings: Settings, redis_client: redis.Redis) -> None:
        super().__init__(app)
        self._settings = settings
        self._sessions = SessionManager(redis_client, settings.session_key_prefix)

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        principal = getattr(request.state, "principal", None)
        if principal is not None and principal.tenant_id is None:
            try:
                await self._sessions.touch_or_create(principal, self._settings)
            except SessionExpiredError as exc:
                logger.info("Session %s expired (%s) for subject=%s.", principal.token_id, exc.reason, principal.subject)
                request.state.principal = None
                return JSONResponse(
                    status_code=401,
                    content={"detail": "Session expired.", "reason": exc.reason},
                    headers={"WWW-Authenticate": 'Bearer error="invalid_token", error_description="session_expired"'},
                )
        return await call_next(request)


class TenantRateLimitMiddleware(BaseHTTPMiddleware):
    """Redis fixed-window rate limiter, keyed by the caller's identity:
    `tenant:<tenant_id>` for an authenticated Broker_API_Client (so one
    broker's burst never starves another's quota -- the actual requirement
    this middleware exists for), `user:<subject>` for an authenticated
    human, or `ip:<client_ip>` for anyone else (unauthenticated callers,
    including a bad-token request that will 401 downstream -- they still
    consume quota, so this also functions as basic credential-stuffing
    throttling on POST /v1/auth/token).
    """

    def __init__(self, app: ASGIApp, settings: Settings, redis_client: redis.Redis) -> None:
        super().__init__(app)
        self._settings = settings
        self._redis = redis_client

    def _rate_limit_key(self, request: Request) -> str:
        principal = getattr(request.state, "principal", None)
        if principal is not None and principal.tenant_id:
            return f"{self._settings.rate_limit_key_prefix}:tenant:{principal.tenant_id}"
        if principal is not None:
            return f"{self._settings.rate_limit_key_prefix}:user:{principal.subject}"
        client_ip = request.client.host if request.client else "unknown"
        return f"{self._settings.rate_limit_key_prefix}:ip:{client_ip}"

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if request.url.path in self._settings.rate_limit_exempt_paths:
            return await call_next(request)

        key = self._rate_limit_key(request)
        window = self._settings.rate_limit_window_seconds

        # INCR then EXPIRE NX (only sets a TTL if this key had none) is
        # atomic-enough for a fixed window here: the tiny race between the
        # two calls can, at worst, very rarely extend one window's expiry
        # by a few ms -- acceptable for a burst-protection limiter, unlike
        # the audit ledger's hash chain which cannot tolerate ANY race.
        pipe = self._redis.pipeline()
        pipe.incr(key)
        pipe.expire(key, window, nx=True)
        count, _ = await pipe.execute()

        if count > self._settings.rate_limit_requests_per_window:
            ttl = await self._redis.ttl(key)
            retry_after = max(ttl, 1)
            logger.warning("Rate limit exceeded for %s (%d/%d in %ds window)", key, count, self._settings.rate_limit_requests_per_window, window)
            return JSONResponse(
                status_code=429,
                content={"detail": "Rate limit exceeded.", "retry_after_seconds": retry_after},
                headers={"Retry-After": str(retry_after)},
            )

        response = await call_next(request)
        response.headers["X-RateLimit-Limit"] = str(self._settings.rate_limit_requests_per_window)
        response.headers["X-RateLimit-Remaining"] = str(max(0, self._settings.rate_limit_requests_per_window - count))
        return response


class PayloadEncryptionMiddleware(BaseHTTPMiddleware):
    """Optional application-layer AES-256-GCM encryption of request/response
    bodies -- see app.security.crypto's module docstring for why this
    exists alongside (not instead of) TLS. Opt-in per request via an
    `X-Encrypted: true` request header; when present, the response is
    encrypted the same way and tagged with the same header. A tenant with
    no provisioned payload key, or a request from a non-tenant-scoped
    caller, simply cannot use this header -- decryption/re-encryption is
    skipped (body passes through in cleartext) with a logged warning
    rather than a hard failure, since payload encryption is a defense-in-
    depth layer on top of mandatory TLS, not the only thing standing
    between a request and confidentiality.
    """

    def __init__(self, app: ASGIApp, settings: Settings) -> None:
        super().__init__(app)
        self._settings = settings

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if not self._settings.payload_encryption_enabled or request.headers.get("x-encrypted", "").lower() != "true":
            return await call_next(request)

        principal = getattr(request.state, "principal", None)
        tenant_id = principal.tenant_id if principal else None
        if not tenant_id:
            logger.warning("X-Encrypted request with no authenticated tenant; passing through undecrypted.")
            return await call_next(request)

        try:
            key = resolve_secret(f"regengine/tenants/{tenant_id}/payload_key")
        except Exception as exc:  # noqa: BLE001 - no provisioned key -> pass through, do not 500
            logger.warning("No payload encryption key provisioned for tenant '%s': %s", tenant_id, exc)
            return await call_next(request)

        body = await request.body()
        if body:
            try:
                plaintext = decrypt_payload(body.decode("ascii"), key, tenant_id=tenant_id)
            except PayloadDecryptionError as exc:
                logger.warning("Payload decryption failed for tenant '%s': %s", tenant_id, exc)
                return JSONResponse(status_code=400, content={"detail": "Payload decryption failed."})

            async def _replay_body():
                return {"type": "http.request", "body": plaintext, "more_body": False}

            request._receive = _replay_body  # noqa: SLF001 - the documented Starlette pattern for body substitution

        response = await call_next(request)

        response_body = b"".join([chunk async for chunk in response.body_iterator])
        encrypted = encrypt_payload(response_body, key, tenant_id=tenant_id)
        new_headers = dict(response.headers)
        new_headers["x-encrypted"] = "true"
        new_headers.pop("content-length", None)
        return Response(
            content=encrypted,
            status_code=response.status_code,
            headers=new_headers,
            media_type=response.media_type,
        )
