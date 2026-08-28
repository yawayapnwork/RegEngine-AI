"""Security framework test suite: JWT issuance/validation, RBAC
enforcement, per-tenant rate limiting, payload encryption, and the secrets
provider abstraction.

The end-to-end RBAC/rate-limit tests build a MINIMAL FastAPI app wiring
only app.security's middleware + a couple of dummy protected routes,
rather than importing the full app.main -- app.main's other routers need a
live Postgres/Qdrant/OPA, which this suite deliberately does not require.
Redis is faked (mirrors the _FakeRedis pattern in tests/test_ingestion.py).
"""
from __future__ import annotations

import datetime as dt

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.config import Settings
from app.security.crypto import PayloadDecryptionError, decrypt_payload, encrypt_payload, generate_tenant_key
from app.security.dependencies import get_current_principal, require_roles
from app.security.jwt import TokenExpiredError, TokenInvalidError, create_access_token, decode_access_token
from app.security.middleware import JWTAuthenticationMiddleware, SecurityHeadersMiddleware, TenantRateLimitMiddleware
from app.security.models import Principal, Role, TokenPayload
from app.security.secrets import (
    CachedSecretsProvider,
    EnvSecretsProvider,
    SecretNotFoundError,
    get_secrets_provider,
)
from app.security.tenant_store import TenantClientStore

HS256_SECRET = "test-secret-key-not-for-production"


def _settings(**overrides) -> Settings:
    base = dict(jwt_algorithm="HS256", jwt_secret_key=HS256_SECRET, jwt_issuer="regengine-ai", jwt_audience="regengine-ai-api")
    base.update(overrides)
    return Settings(**base)


# --------------------------------------------------------------------------
# Pydantic auth models
# --------------------------------------------------------------------------


class TestAuthModels:
    def test_broker_role_requires_tenant_id(self):
        with pytest.raises(ValueError):
            TokenPayload(
                sub="client-1", roles=[Role.BROKER_API_CLIENT], tenant_id=None,
                iss="regengine-ai", aud="regengine-ai-api",
                iat=dt.datetime.now(dt.timezone.utc), exp=dt.datetime.now(dt.timezone.utc), jti="t1",
            )

    def test_compliance_officer_does_not_require_tenant_id(self):
        payload = TokenPayload(
            sub="officer-1", roles=[Role.COMPLIANCE_OFFICER], tenant_id=None,
            iss="regengine-ai", aud="regengine-ai-api",
            iat=dt.datetime.now(dt.timezone.utc), exp=dt.datetime.now(dt.timezone.utc), jti="t2",
        )
        assert payload.tenant_id is None

    def test_principal_has_role_and_is_admin(self):
        principal = Principal(subject="u1", roles=[Role.SYSTEM_ADMIN], token_id="t1")
        assert principal.has_role(Role.SYSTEM_ADMIN, Role.COMPLIANCE_OFFICER)
        assert principal.is_admin()
        assert not principal.has_role(Role.BROKER_API_CLIENT)


# --------------------------------------------------------------------------
# JWT issuance / validation
# --------------------------------------------------------------------------


class TestJWT:
    def test_encode_decode_roundtrip(self):
        settings = _settings()
        token, payload = create_access_token(
            subject="brk-client-1", roles=[Role.BROKER_API_CLIENT], settings=settings,
            signing_key=HS256_SECRET, tenant_id="BRK001",
        )
        decoded = decode_access_token(token, settings, local_verification_key=HS256_SECRET)

        assert decoded.sub == "brk-client-1"
        assert decoded.tenant_id == "BRK001"
        assert decoded.roles == [Role.BROKER_API_CLIENT]
        assert decoded.jti == payload.jti

    def test_wrong_signing_key_is_rejected(self):
        settings = _settings()
        token, _ = create_access_token(
            subject="brk-1", roles=[Role.BROKER_API_CLIENT], settings=settings,
            signing_key=HS256_SECRET, tenant_id="BRK001",
        )
        with pytest.raises(TokenInvalidError):
            decode_access_token(token, settings, local_verification_key="a-completely-different-key")

    def test_expired_token_is_rejected(self):
        settings = _settings()
        token, _ = create_access_token(
            subject="brk-1", roles=[Role.BROKER_API_CLIENT], settings=settings,
            signing_key=HS256_SECRET, tenant_id="BRK001", ttl_seconds=-10,
        )
        with pytest.raises(TokenExpiredError):
            decode_access_token(token, settings, local_verification_key=HS256_SECRET)

    def test_unrecognized_issuer_is_rejected(self):
        settings = _settings()
        token, _ = create_access_token(
            subject="brk-1", roles=[Role.BROKER_API_CLIENT], settings=settings,
            signing_key=HS256_SECRET, tenant_id="BRK001",
        )
        other_settings = _settings(jwt_issuer="someone-elses-service")
        with pytest.raises(TokenInvalidError):
            decode_access_token(token, other_settings, local_verification_key=HS256_SECRET)

    def test_malformed_token_is_rejected(self):
        settings = _settings()
        with pytest.raises(TokenInvalidError):
            decode_access_token("not-a-jwt-at-all", settings, local_verification_key=HS256_SECRET)


# --------------------------------------------------------------------------
# Tenant client store (bcrypt-hashed broker credentials)
# --------------------------------------------------------------------------


class _FakeRedisKV:
    """Minimal async stand-in for the redis.asyncio.Redis subset
    TenantClientStore uses."""

    def __init__(self) -> None:
        self._store: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        return self._store.get(key)

    async def set(self, key: str, value: str) -> None:
        self._store[key] = value


@pytest.mark.asyncio
class TestTenantClientStore:
    async def test_register_then_authenticate_succeeds(self):
        store = TenantClientStore(_FakeRedisKV(), key_prefix="test:tenants")
        await store.register("client-1", "s3cr3t", tenant_id="BRK001")

        client = await store.authenticate("client-1", "s3cr3t")

        assert client is not None
        assert client.tenant_id == "BRK001"
        assert Role.BROKER_API_CLIENT in client.roles

    async def test_wrong_secret_fails(self):
        store = TenantClientStore(_FakeRedisKV(), key_prefix="test:tenants")
        await store.register("client-1", "s3cr3t", tenant_id="BRK001")

        assert await store.authenticate("client-1", "wrong") is None

    async def test_unknown_client_id_fails(self):
        store = TenantClientStore(_FakeRedisKV(), key_prefix="test:tenants")
        assert await store.authenticate("nonexistent", "anything") is None

    async def test_disabled_client_cannot_authenticate(self):
        store = TenantClientStore(_FakeRedisKV(), key_prefix="test:tenants")
        await store.register("client-1", "s3cr3t", tenant_id="BRK001")
        await store.disable("client-1")

        assert await store.authenticate("client-1", "s3cr3t") is None


# --------------------------------------------------------------------------
# Secrets provider abstraction
# --------------------------------------------------------------------------


class TestSecretsProvider:
    def test_env_provider_reads_settings_attribute(self):
        settings = _settings()
        provider = EnvSecretsProvider(settings)
        assert provider.get_secret("jwt_secret_key") == HS256_SECRET

    def test_env_provider_missing_attribute_raises(self):
        provider = EnvSecretsProvider(_settings())
        with pytest.raises(SecretNotFoundError):
            provider.get_secret("no_such_setting_field")

    def test_cached_provider_only_calls_inner_once_within_ttl(self):
        calls = []

        class _CountingProvider:
            def get_secret(self, name: str, field: str | None = None) -> str:
                calls.append((name, field))
                return "value"

        cached = CachedSecretsProvider(_CountingProvider(), ttl_seconds=60)
        assert cached.get_secret("k") == "value"
        assert cached.get_secret("k") == "value"
        assert cached.get_secret("k") == "value"
        assert len(calls) == 1  # second/third calls served from cache

    def test_cached_provider_refetches_after_ttl_expiry(self, monkeypatch):
        calls = []

        class _CountingProvider:
            def get_secret(self, name: str, field: str | None = None) -> str:
                calls.append(name)
                return "value"

        cached = CachedSecretsProvider(_CountingProvider(), ttl_seconds=0.01)
        cached.get_secret("k")
        import time

        time.sleep(0.02)
        cached.get_secret("k")
        assert len(calls) == 2

    def test_factory_rejects_unknown_backend(self, monkeypatch):
        from app.config import get_settings

        get_settings.cache_clear()
        get_secrets_provider.cache_clear()
        monkeypatch.setenv("SECRETS_BACKEND", "not-a-real-backend")
        try:
            with pytest.raises(ValueError):
                get_secrets_provider()
        finally:
            monkeypatch.delenv("SECRETS_BACKEND", raising=False)
            get_settings.cache_clear()
            get_secrets_provider.cache_clear()


# --------------------------------------------------------------------------
# Payload encryption
# --------------------------------------------------------------------------


class TestPayloadCrypto:
    def test_roundtrip(self):
        key = generate_tenant_key()
        ciphertext = encrypt_payload(b'{"upfront_margin_pct": 25.0}', key, tenant_id="BRK001")
        plaintext = decrypt_payload(ciphertext, key, tenant_id="BRK001")
        assert plaintext == b'{"upfront_margin_pct": 25.0}'

    def test_wrong_tenant_id_fails_aead_check(self):
        key = generate_tenant_key()
        ciphertext = encrypt_payload(b"secret data", key, tenant_id="BRK001")
        with pytest.raises(PayloadDecryptionError):
            decrypt_payload(ciphertext, key, tenant_id="BRK002")

    def test_tampered_ciphertext_fails(self):
        key = generate_tenant_key()
        ciphertext = encrypt_payload(b"secret data", key, tenant_id="BRK001")
        tampered = ciphertext[:-4] + ("AAAA" if ciphertext[-4:] != "AAAA" else "BBBB")
        with pytest.raises(PayloadDecryptionError):
            decrypt_payload(tampered, key, tenant_id="BRK001")


# --------------------------------------------------------------------------
# End-to-end RBAC + rate limiting over a minimal FastAPI app
# --------------------------------------------------------------------------


class _FakePipeline:
    def __init__(self, redis: "_FakeRateLimitRedis") -> None:
        self._redis = redis
        self._ops: list[tuple[str, tuple]] = []

    def incr(self, key: str):
        self._ops.append(("incr", (key,)))
        return self

    def expire(self, key: str, seconds: int, nx: bool = False):
        self._ops.append(("expire", (key, seconds, nx)))
        return self

    async def execute(self) -> list:
        results = []
        for op, args in self._ops:
            if op == "incr":
                (key,) = args
                self._redis.counts[key] = self._redis.counts.get(key, 0) + 1
                results.append(self._redis.counts[key])
            elif op == "expire":
                key, seconds, nx = args
                if not nx or key not in self._redis.ttls:
                    self._redis.ttls[key] = seconds
                results.append(True)
        return results


class _FakeRateLimitRedis:
    """Minimal async stand-in for the redis.asyncio.Redis subset
    TenantRateLimitMiddleware uses."""

    def __init__(self) -> None:
        self.counts: dict[str, int] = {}
        self.ttls: dict[str, int] = {}

    def pipeline(self):
        return _FakePipeline(self)

    async def ttl(self, key: str) -> int:
        return self.ttls.get(key, 30)


def _build_test_app(settings: Settings, redis_client: _FakeRateLimitRedis) -> FastAPI:
    # Mount order matches app/main.py: last added = outermost = runs first.
    # JWTAuthentication must run (and set request.state.principal) before
    # TenantRateLimit reads it -- see app/security/middleware.py's docstring.
    app = FastAPI()
    app.add_middleware(TenantRateLimitMiddleware, settings=settings, redis_client=redis_client)
    app.add_middleware(JWTAuthenticationMiddleware, settings=settings)
    app.add_middleware(SecurityHeadersMiddleware, settings=settings)

    @app.get("/healthz")
    async def healthz():
        return {"status": "ok"}

    @app.get("/v1/public/ping")
    async def public_ping():
        # Unlike /healthz, deliberately NOT in rate_limit_exempt_paths --
        # used by rate-limit tests that need a route the limiter actually
        # applies to.
        return {"ok": True}

    @app.get("/v1/hitl-reviews/ping")
    async def hitl_ping(_: Principal = Depends(require_roles(Role.COMPLIANCE_OFFICER))):
        return {"ok": True}

    @app.get("/v1/execution/ping")
    async def execution_ping(principal: Principal = Depends(require_roles(Role.BROKER_API_CLIENT, Role.SYSTEM_ADMIN))):
        return {"ok": True, "tenant_id": principal.tenant_id}

    @app.get("/v1/auth/me")
    async def whoami(principal: Principal = Depends(get_current_principal)):
        return {"subject": principal.subject}

    return app


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


class TestRBACEndToEnd:
    def test_public_route_requires_no_auth(self):
        settings = _settings(rate_limit_requests_per_window=100)
        app = _build_test_app(settings, _FakeRateLimitRedis())
        client = TestClient(app)

        response = client.get("/healthz")

        assert response.status_code == 200
        assert response.headers["Strict-Transport-Security"].startswith("max-age=")

    def test_protected_route_without_token_is_401(self):
        settings = _settings(rate_limit_requests_per_window=100)
        app = _build_test_app(settings, _FakeRateLimitRedis())
        client = TestClient(app)

        response = client.get("/v1/hitl-reviews/ping")

        assert response.status_code == 401
        assert response.headers["WWW-Authenticate"] == "Bearer"

    def test_broker_token_rejected_from_compliance_officer_route(self):
        settings = _settings(rate_limit_requests_per_window=100)
        token, _ = create_access_token(
            subject="brk-1", roles=[Role.BROKER_API_CLIENT], settings=settings,
            signing_key=HS256_SECRET, tenant_id="BRK001",
        )
        app = _build_test_app(settings, _FakeRateLimitRedis())
        client = TestClient(app)

        response = client.get("/v1/hitl-reviews/ping", headers=_bearer(token))

        assert response.status_code == 403

    def test_compliance_officer_token_allowed_on_hitl_route(self):
        settings = _settings(rate_limit_requests_per_window=100)
        token, _ = create_access_token(
            subject="officer-1", roles=[Role.COMPLIANCE_OFFICER], settings=settings, signing_key=HS256_SECRET,
        )
        app = _build_test_app(settings, _FakeRateLimitRedis())
        client = TestClient(app)

        response = client.get("/v1/hitl-reviews/ping", headers=_bearer(token))

        assert response.status_code == 200

    def test_broker_token_allowed_on_execution_route_and_carries_tenant(self):
        settings = _settings(rate_limit_requests_per_window=100)
        token, _ = create_access_token(
            subject="brk-1", roles=[Role.BROKER_API_CLIENT], settings=settings,
            signing_key=HS256_SECRET, tenant_id="BRK001",
        )
        app = _build_test_app(settings, _FakeRateLimitRedis())
        client = TestClient(app)

        response = client.get("/v1/execution/ping", headers=_bearer(token))

        assert response.status_code == 200
        assert response.json()["tenant_id"] == "BRK001"

    def test_admin_token_allowed_on_both_broker_and_admin_style_routes(self):
        settings = _settings(rate_limit_requests_per_window=100)
        token, _ = create_access_token(
            subject="admin-1", roles=[Role.SYSTEM_ADMIN], settings=settings, signing_key=HS256_SECRET,
        )
        app = _build_test_app(settings, _FakeRateLimitRedis())
        client = TestClient(app)

        assert client.get("/v1/execution/ping", headers=_bearer(token)).status_code == 200
        # System_Admin is deliberately NOT allowed to approve HITL reviews
        # -- see app.api.hitl_review_routes' module docstring.
        assert client.get("/v1/hitl-reviews/ping", headers=_bearer(token)).status_code == 403

    def test_expired_token_is_401_not_500(self):
        settings = _settings(rate_limit_requests_per_window=100)
        token, _ = create_access_token(
            subject="brk-1", roles=[Role.BROKER_API_CLIENT], settings=settings,
            signing_key=HS256_SECRET, tenant_id="BRK001", ttl_seconds=-10,
        )
        app = _build_test_app(settings, _FakeRateLimitRedis())
        client = TestClient(app)

        response = client.get("/v1/execution/ping", headers=_bearer(token))

        assert response.status_code == 401

    def test_rate_limit_exceeded_returns_429_with_retry_after(self):
        settings = _settings(rate_limit_requests_per_window=2, rate_limit_window_seconds=60)
        app = _build_test_app(settings, _FakeRateLimitRedis())
        client = TestClient(app)

        assert client.get("/v1/public/ping").status_code == 200
        assert client.get("/v1/public/ping").status_code == 200
        third = client.get("/v1/public/ping")

        assert third.status_code == 429
        assert "Retry-After" in third.headers
        assert third.json()["detail"] == "Rate limit exceeded."

    def test_rate_limit_is_isolated_per_tenant(self):
        """The actual requirement this middleware exists for: one broker's
        burst must not consume another broker's quota."""
        settings = _settings(rate_limit_requests_per_window=1, rate_limit_window_seconds=60)
        shared_redis = _FakeRateLimitRedis()
        app = _build_test_app(settings, shared_redis)
        client = TestClient(app)

        token_a, _ = create_access_token(
            subject="brk-a", roles=[Role.BROKER_API_CLIENT], settings=settings,
            signing_key=HS256_SECRET, tenant_id="BRK-A",
        )
        token_b, _ = create_access_token(
            subject="brk-b", roles=[Role.BROKER_API_CLIENT], settings=settings,
            signing_key=HS256_SECRET, tenant_id="BRK-B",
        )

        # Tenant A exhausts its own window...
        assert client.get("/v1/execution/ping", headers=_bearer(token_a)).status_code == 200
        assert client.get("/v1/execution/ping", headers=_bearer(token_a)).status_code == 429
        # ...but tenant B is unaffected.
        assert client.get("/v1/execution/ping", headers=_bearer(token_b)).status_code == 200

    def test_rate_limit_exempt_path_bypasses_limiter(self):
        settings = _settings(rate_limit_requests_per_window=1, rate_limit_window_seconds=60)
        app = _build_test_app(settings, _FakeRateLimitRedis())
        client = TestClient(app)

        for _ in range(5):
            response = client.get("/healthz")
            assert response.status_code == 200  # /healthz is in rate_limit_exempt_paths by default
