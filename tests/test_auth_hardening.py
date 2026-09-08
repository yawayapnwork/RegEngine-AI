"""Comprehensive tests for hardened RegEngine authentication and MFA step-up.

Verifies:
1. Environment tier separation: development, explicit demo mode, staging, production.
2. MFA bypass strictly requires DEMO_MODE=true in development.
3. ENVIRONMENT=development alone does NOT grant fake MFA claims.
4. Staging and production categorically reject DEMO_MODE=true (fail closed).
5. Never issue amr=['mfa'] on local login unless demo mode is explicitly active.
6. Demo bypass logs prominent warnings at startup, login token issuance, and step-up checks.
7. HITL approval endpoints remain protected by authorization (require_roles) even in demo mode.
8. Machine credentials and unauthorized roles are rejected even in demo mode.
9. Ambiguous or invalid configuration fails closed with descriptive errors.
"""
from __future__ import annotations

import datetime as dt
import logging
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.api.auth_routes import LoginRequest, login
from app.config import Settings
from app.main import _check_demo_mode_configured
from app.security.dependencies import require_roles
from app.security.jwt import create_access_token, decode_access_token
from app.security.middleware import JWTAuthenticationMiddleware
from app.security.models import Principal, Role
from app.security.step_up import require_step_up_mfa

HS256_SECRET = "test-secret-key-32-chars-long-not-for-prod"


def _make_settings(**overrides) -> Settings:
    base = dict(
        environment="development",
        demo_mode=False,
        jwt_algorithm="HS256",
        jwt_secret_key=HS256_SECRET,
        jwt_issuer="regengine-ai",
        jwt_audience="regengine-ai-api",
        step_up_mfa_max_age_seconds=300,
        step_up_required_amr_values=["mfa", "otp"],
        step_up_mfa_enforce_in_dev=False,
    )
    base.update(overrides)
    return Settings(**base)


def _make_principal(**overrides) -> Principal:
    base = dict(
        subject="officer.jane@regengine.dev",
        roles=[Role.COMPLIANCE_OFFICER],
        tenant_id=None,
        token_id="tok-test-1",
    )
    base.update(overrides)
    return Principal(**base)


# ==============================================================================
# 1. Configuration & Validation (Fail-closed on Ambiguous / Forbidden Config)
# ==============================================================================


class TestEnvironmentAndDemoModeConfigValidation:
    def test_default_settings_is_normal_development_without_demo(self):
        """Default Settings must have environment='development' and demo_mode=False."""
        settings = Settings()
        assert settings.environment == "development"
        assert settings.demo_mode is False

    def test_production_categorically_rejects_demo_mode(self):
        """Production must raise ValidationError when demo_mode=True."""
        with pytest.raises(ValidationError) as exc_info:
            Settings(environment="production", demo_mode=True)
        assert "DEMO_MODE=True is strictly prohibited in environment='production'" in str(exc_info.value)

    def test_staging_categorically_rejects_demo_mode(self):
        """Staging must raise ValidationError when demo_mode=True."""
        with pytest.raises(ValidationError) as exc_info:
            Settings(environment="staging", demo_mode=True)
        assert "DEMO_MODE=True is strictly prohibited in environment='staging'" in str(exc_info.value)

    def test_invalid_environment_fails_closed(self):
        """Unknown or ambiguous environments must raise ValidationError."""
        for bad_env in ["unknown", "prod", "dev", "local", "", "   "]:
            with pytest.raises(ValidationError) as exc_info:
                Settings(environment=bad_env)
            assert "Invalid or ambiguous environment" in str(exc_info.value)

    def test_environment_string_is_normalized(self):
        """Environment names should be trimmed and lowercased cleanly."""
        settings = Settings(environment="  DEVELOPMENT  ")
        assert settings.environment == "development"

        settings_prod = Settings(environment=" PRODUCTION ", jwt_secret_key=HS256_SECRET)
        assert settings_prod.environment == "production"

    def test_conflicting_demo_mode_and_enforce_in_dev_fails_closed(self):
        """Setting demo_mode=True alongside step_up_mfa_enforce_in_dev=True is ambiguous."""
        with pytest.raises(ValidationError) as exc_info:
            Settings(environment="development", demo_mode=True, step_up_mfa_enforce_in_dev=True)
        assert "Ambiguous configuration: demo_mode=True conflicts with step_up_mfa_enforce_in_dev=True" in str(exc_info.value)


# ==============================================================================
# 2. Startup Guard & Warnings
# ==============================================================================


class TestStartupDemoModeCheck:
    def test_startup_passes_in_development_without_demo(self, caplog):
        """In normal development, startup check completes silently without demo warnings."""
        settings = _make_settings(environment="development", demo_mode=False)
        with caplog.at_level(logging.WARNING):
            _check_demo_mode_configured(settings)
        assert "DEMO MODE IS ACTIVE" not in caplog.text

    def test_startup_emits_prominent_warning_in_demo_mode(self, caplog):
        """In explicit demo mode, startup check logs a conspicuous banner."""
        settings = _make_settings(environment="development", demo_mode=True)
        with caplog.at_level(logging.WARNING):
            _check_demo_mode_configured(settings)
        assert "!!! WARNING: DEMO MODE IS ACTIVE (DEMO_MODE=true) !!!" in caplog.text

    def test_startup_check_raises_runtime_error_if_production_bypassed(self):
        """Even if settings validation were somehow bypassed, startup guard raises RuntimeError in prod."""
        settings = _make_settings(environment="development")
        object.__setattr__(settings, "environment", "production")
        object.__setattr__(settings, "demo_mode", True)
        with pytest.raises(RuntimeError) as exc_info:
            _check_demo_mode_configured(settings)
        assert "FATAL: DEMO_MODE is enabled in environment='production'" in str(exc_info.value)

    def test_startup_check_raises_runtime_error_if_staging_bypassed(self):
        """Startup guard raises RuntimeError if demo_mode is True in staging."""
        settings = _make_settings(environment="development")
        object.__setattr__(settings, "environment", "staging")
        object.__setattr__(settings, "demo_mode", True)
        with pytest.raises(RuntimeError) as exc_info:
            _check_demo_mode_configured(settings)
        assert "FATAL: DEMO_MODE is enabled in environment='staging'" in str(exc_info.value)


# ==============================================================================
# 3. Local Login & AMR Claim Issuance
# ==============================================================================


@pytest.mark.asyncio
class TestLocalLoginAMR:
    async def test_development_without_demo_mode_issues_pwd_only(self):
        """In normal development (demo_mode=False), local login issues amr=['pwd'] (no 'mfa')."""
        settings = _make_settings(environment="development", demo_mode=False)
        mock_user = MagicMock(email="officer@example.com", roles=[Role.COMPLIANCE_OFFICER])
        mock_user_store = AsyncMock()
        mock_user_store.authenticate = AsyncMock(return_value=mock_user)

        response = await login(
            request=LoginRequest(email="officer@example.com", password="password123"),
            settings=settings,
            users=mock_user_store,
        )
        payload = decode_access_token(response.access_token, settings, local_verification_key=HS256_SECRET)
        assert payload.amr == ["pwd"]
        assert "mfa" not in payload.amr

    async def test_explicit_demo_mode_issues_pwd_and_mfa_with_warning(self, caplog):
        """In explicit demo mode (demo_mode=True), local login issues amr=['pwd', 'mfa'] and warns."""
        settings = _make_settings(environment="development", demo_mode=True)
        mock_user = MagicMock(email="officer@example.com", roles=[Role.COMPLIANCE_OFFICER])
        mock_user_store = AsyncMock()
        mock_user_store.authenticate = AsyncMock(return_value=mock_user)

        with caplog.at_level(logging.WARNING):
            response = await login(
                request=LoginRequest(email="officer@example.com", password="password123"),
                settings=settings,
                users=mock_user_store,
            )
        assert "DEMO MODE ACTIVE: Issuing synthetic step-up MFA claims" in caplog.text
        payload = decode_access_token(response.access_token, settings, local_verification_key=HS256_SECRET)
        assert "pwd" in payload.amr
        assert "mfa" in payload.amr

    async def test_staging_login_issues_pwd_only(self):
        """In staging, local login only issues single-factor amr=['pwd']."""
        settings = _make_settings(environment="staging", demo_mode=False)
        mock_user = MagicMock(email="officer@example.com", roles=[Role.COMPLIANCE_OFFICER])
        mock_user_store = AsyncMock()
        mock_user_store.authenticate = AsyncMock(return_value=mock_user)

        response = await login(
            request=LoginRequest(email="officer@example.com", password="password123"),
            settings=settings,
            users=mock_user_store,
        )
        payload = decode_access_token(response.access_token, settings, local_verification_key=HS256_SECRET)
        assert payload.amr == ["pwd"]

    async def test_production_login_issues_pwd_only(self):
        """In production, local login only issues single-factor amr=['pwd']."""
        settings = _make_settings(environment="production", demo_mode=False)
        mock_user = MagicMock(email="officer@example.com", roles=[Role.COMPLIANCE_OFFICER])
        mock_user_store = AsyncMock()
        mock_user_store.authenticate = AsyncMock(return_value=mock_user)

        response = await login(
            request=LoginRequest(email="officer@example.com", password="password123"),
            settings=settings,
            users=mock_user_store,
        )
        payload = decode_access_token(response.access_token, settings, local_verification_key=HS256_SECRET)
        assert payload.amr == ["pwd"]


# ==============================================================================
# 4. Step-Up MFA Dependency Enforcement
# ==============================================================================


@pytest.mark.asyncio
class TestStepUpMFAEnforcement:
    async def test_development_without_demo_mode_rejects_single_factor_token(self):
        """In normal development, a token carrying only amr=['pwd'] fails step-up with 401."""
        settings = _make_settings(environment="development", demo_mode=False)
        principal = _make_principal(auth_time=dt.datetime.now(dt.timezone.utc), amr=["pwd"])

        with pytest.raises(HTTPException) as exc_info:
            await require_step_up_mfa(principal=principal, settings=settings)
        assert exc_info.value.status_code == 401
        assert exc_info.value.detail["reason"] == "mfa_not_satisfied"

    async def test_development_without_demo_mode_rejects_missing_auth_time(self):
        """In normal development, missing auth_time fails step-up with 401."""
        settings = _make_settings(environment="development", demo_mode=False)
        principal = _make_principal(auth_time=None, amr=["mfa"])

        with pytest.raises(HTTPException) as exc_info:
            await require_step_up_mfa(principal=principal, settings=settings)
        assert exc_info.value.status_code == 401
        assert exc_info.value.detail["reason"] == "no_auth_time_claim"

    async def test_demo_mode_allows_compliance_officer_without_mfa_with_warning(self, caplog):
        """In explicit demo mode, step-up MFA is bypassed for compliance officers and logs a warning."""
        settings = _make_settings(environment="development", demo_mode=True)
        principal = _make_principal(auth_time=None, amr=[])

        with caplog.at_level(logging.WARNING):
            result = await require_step_up_mfa(principal=principal, settings=settings)
        assert result is principal
        assert "DEMO MODE ACTIVE: Bypassing step-up MFA verification" in caplog.text

    async def test_demo_mode_still_rejects_machine_principal(self):
        """Machine credentials with tenant_id are rejected (403) even when demo_mode=True."""
        settings = _make_settings(environment="development", demo_mode=True)
        machine_principal = _make_principal(roles=[Role.BROKER_API_CLIENT], tenant_id="BRK001")

        with pytest.raises(HTTPException) as exc_info:
            await require_step_up_mfa(principal=machine_principal, settings=settings)
        assert exc_info.value.status_code == 403
        assert "Step-up MFA is not applicable to machine credentials" in exc_info.value.detail

    async def test_staging_and_production_strictly_enforce_step_up(self):
        """Staging and production always reject password-only tokens."""
        for env in ["staging", "production"]:
            settings = _make_settings(environment=env, demo_mode=False)
            principal = _make_principal(auth_time=dt.datetime.now(dt.timezone.utc), amr=["pwd"])

            with pytest.raises(HTTPException) as exc_info:
                await require_step_up_mfa(principal=principal, settings=settings)
            assert exc_info.value.status_code == 401
            assert exc_info.value.detail["reason"] == "mfa_not_satisfied"


# ==============================================================================
# 5. HITL Endpoint Authorization & Role Guard End-to-End
# ==============================================================================


def _build_hitl_test_app(settings: Settings) -> FastAPI:
    from app.config import get_settings

    app = FastAPI()
    app.dependency_overrides[get_settings] = lambda: settings
    app.add_middleware(
        JWTAuthenticationMiddleware,
        settings=settings,
    )

    @app.post("/v1/hitl-reviews/{review_id}/approve")
    async def approve(
        review_id: str,
        principal: Principal = Depends(require_roles(Role.COMPLIANCE_OFFICER)),
        _step_up: Principal = Depends(require_step_up_mfa),
    ):
        return {"status": "approved", "officer": principal.subject}

    return app


class TestHITLEndpointProtection:
    def test_unauthenticated_request_is_rejected_even_in_demo_mode(self):
        """An unauthenticated request is 401 even when demo_mode=True."""
        settings = _make_settings(environment="development", demo_mode=True)
        app = _build_hitl_test_app(settings)
        client = TestClient(app)

        resp = client.post("/v1/hitl-reviews/rev-1/approve")
        assert resp.status_code == 401

    def test_broker_client_is_forbidden_even_in_demo_mode(self):
        """Broker API Client is rejected with 403 by require_roles even in demo mode."""
        settings = _make_settings(environment="development", demo_mode=True)
        token, _ = create_access_token(
            subject="broker-client",
            roles=[Role.BROKER_API_CLIENT],
            settings=settings,
            signing_key=HS256_SECRET,
            tenant_id="BRK001",
        )
        app = _build_hitl_test_app(settings)
        client = TestClient(app)

        resp = client.post(
            "/v1/hitl-reviews/rev-1/approve",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 403

    def test_admin_role_cannot_approve_hitl_even_in_demo_mode(self):
        """System_Admin is rejected with 403 (only Compliance_Officer may approve)."""
        settings = _make_settings(environment="development", demo_mode=True)
        token, _ = create_access_token(
            subject="admin-1",
            roles=[Role.SYSTEM_ADMIN],
            settings=settings,
            signing_key=HS256_SECRET,
        )
        app = _build_hitl_test_app(settings)
        client = TestClient(app)

        resp = client.post(
            "/v1/hitl-reviews/rev-1/approve",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 403

    def test_compliance_officer_without_mfa_rejected_in_normal_development(self):
        """In normal development (demo_mode=False), compliance officer with amr=['pwd'] is rejected (401)."""
        settings = _make_settings(environment="development", demo_mode=False)
        token, _ = create_access_token(
            subject="officer.jane",
            roles=[Role.COMPLIANCE_OFFICER],
            settings=settings,
            signing_key=HS256_SECRET,
            auth_time=dt.datetime.now(dt.timezone.utc),
            amr=["pwd"],
        )
        app = _build_hitl_test_app(settings)
        client = TestClient(app)

        resp = client.post(
            "/v1/hitl-reviews/rev-1/approve",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 401
        assert resp.json()["detail"]["reason"] == "mfa_not_satisfied"

    def test_compliance_officer_succeeds_in_explicit_demo_mode(self):
        """In explicit demo mode (demo_mode=True), compliance officer approval succeeds."""
        settings = _make_settings(environment="development", demo_mode=True)
        token, _ = create_access_token(
            subject="officer.jane",
            roles=[Role.COMPLIANCE_OFFICER],
            settings=settings,
            signing_key=HS256_SECRET,
        )
        app = _build_hitl_test_app(settings)
        client = TestClient(app)

        resp = client.post(
            "/v1/hitl-reviews/rev-1/approve",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "approved"
