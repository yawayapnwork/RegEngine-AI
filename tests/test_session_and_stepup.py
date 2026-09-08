"""Tests for session-timeout enforcement (app.security.session_manager)
and step-up MFA (app.security.step_up)."""
from __future__ import annotations

import datetime as dt

import pytest
from fastapi import HTTPException

from app.config import Settings
from app.security.models import Principal, Role
from app.security.session_manager import SessionExpiredError, SessionManager
from app.security.step_up import require_step_up_mfa


class _FakeRedis:
    def __init__(self) -> None:
        self.hashes: dict[str, dict[str, str]] = {}
        self.ttls: dict[str, int] = {}

    async def hgetall(self, key: str) -> dict[str, str]:
        return dict(self.hashes.get(key, {}))

    async def hset(self, key: str, field: str | None = None, value: str | None = None, mapping: dict | None = None) -> None:
        self.hashes.setdefault(key, {})
        if mapping:
            self.hashes[key].update(mapping)
        elif field is not None:
            self.hashes[key][field] = value

    async def expire(self, key: str, seconds: int) -> None:
        self.ttls[key] = seconds

    async def delete(self, key: str) -> None:
        self.hashes.pop(key, None)
        self.ttls.pop(key, None)


def _settings(**overrides) -> Settings:
    base = dict(
        environment="production",
        jwt_algorithm="HS256", jwt_secret_key="test-secret-key-not-for-production",
        session_idle_timeout_seconds=900, session_absolute_timeout_seconds=28800,
        step_up_mfa_max_age_seconds=300, step_up_required_amr_values=["mfa", "otp"],
        step_up_mfa_enforce_in_dev=False, demo_mode=False,
    )
    base.update(overrides)
    return Settings(**base)


def _principal(**overrides) -> Principal:
    base = dict(subject="officer.jane", roles=[Role.COMPLIANCE_OFFICER], tenant_id=None, token_id="tok-1")
    base.update(overrides)
    return Principal(**base)


@pytest.mark.asyncio
class TestSessionManager:
    async def test_first_touch_creates_session(self) -> None:
        redis_client = _FakeRedis()
        manager = SessionManager(redis_client, "regengine:sessions")
        await manager.touch_or_create(_principal(), _settings())
        assert "regengine:sessions:tok-1" in redis_client.hashes

    async def test_touch_within_windows_succeeds_and_refreshes_activity(self) -> None:
        redis_client = _FakeRedis()
        manager = SessionManager(redis_client, "regengine:sessions")
        settings = _settings()
        principal = _principal()
        await manager.touch_or_create(principal, settings)
        first_activity = redis_client.hashes["regengine:sessions:tok-1"]["last_activity_at"]

        await manager.touch_or_create(principal, settings)
        second_activity = redis_client.hashes["regengine:sessions:tok-1"]["last_activity_at"]
        assert second_activity >= first_activity  # ISO timestamps sort lexicographically

    async def test_idle_timeout_raises_and_clears_session(self) -> None:
        redis_client = _FakeRedis()
        manager = SessionManager(redis_client, "regengine:sessions")
        settings = _settings(session_idle_timeout_seconds=1)
        principal = _principal()

        stale_time = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=10)).isoformat()
        redis_client.hashes["regengine:sessions:tok-1"] = {"created_at": stale_time, "last_activity_at": stale_time}

        with pytest.raises(SessionExpiredError) as exc_info:
            await manager.touch_or_create(principal, settings)
        assert exc_info.value.reason == "idle_timeout"
        assert "regengine:sessions:tok-1" not in redis_client.hashes

    async def test_absolute_timeout_raises_even_with_recent_activity(self) -> None:
        redis_client = _FakeRedis()
        manager = SessionManager(redis_client, "regengine:sessions")
        settings = _settings(session_idle_timeout_seconds=99999, session_absolute_timeout_seconds=5)
        principal = _principal()

        old_created = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=60)).isoformat()
        recent_activity = dt.datetime.now(dt.timezone.utc).isoformat()
        redis_client.hashes["regengine:sessions:tok-1"] = {"created_at": old_created, "last_activity_at": recent_activity}

        with pytest.raises(SessionExpiredError) as exc_info:
            await manager.touch_or_create(principal, settings)
        assert exc_info.value.reason == "absolute_timeout"

    async def test_revoke_removes_session(self) -> None:
        redis_client = _FakeRedis()
        manager = SessionManager(redis_client, "regengine:sessions")
        await manager.touch_or_create(_principal(), _settings())
        await manager.revoke("tok-1")
        assert "regengine:sessions:tok-1" not in redis_client.hashes


@pytest.mark.asyncio
class TestStepUpMFA:
    async def test_fresh_mfa_auth_passes(self) -> None:
        settings = _settings()
        now = dt.datetime.now(dt.timezone.utc)
        principal = _principal(auth_time=now, amr=["pwd", "mfa"])
        result = await require_step_up_mfa(principal=principal, settings=settings)
        assert result is principal

    async def test_missing_auth_time_is_rejected(self) -> None:
        settings = _settings()
        principal = _principal(auth_time=None, amr=["mfa"])
        with pytest.raises(HTTPException) as exc_info:
            await require_step_up_mfa(principal=principal, settings=settings)
        assert exc_info.value.status_code == 401
        assert exc_info.value.detail["reason"] == "no_auth_time_claim"

    async def test_stale_auth_time_is_rejected(self) -> None:
        settings = _settings(step_up_mfa_max_age_seconds=60)
        old_time = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=30)
        principal = _principal(auth_time=old_time, amr=["mfa"])
        with pytest.raises(HTTPException) as exc_info:
            await require_step_up_mfa(principal=principal, settings=settings)
        assert exc_info.value.detail["reason"] == "auth_event_too_old"

    async def test_password_only_amr_is_rejected(self) -> None:
        settings = _settings()
        principal = _principal(auth_time=dt.datetime.now(dt.timezone.utc), amr=["pwd"])
        with pytest.raises(HTTPException) as exc_info:
            await require_step_up_mfa(principal=principal, settings=settings)
        assert exc_info.value.detail["reason"] == "mfa_not_satisfied"

    async def test_machine_principal_is_forbidden(self) -> None:
        settings = _settings()
        principal = _principal(roles=[Role.BROKER_API_CLIENT], tenant_id="BRK001", auth_time=dt.datetime.now(dt.timezone.utc), amr=["mfa"])
        with pytest.raises(HTTPException) as exc_info:
            await require_step_up_mfa(principal=principal, settings=settings)
        assert exc_info.value.status_code == 403

    async def test_development_without_demo_mode_requires_step_up_mfa(self) -> None:
        """In normal development (demo_mode=False), step-up MFA is strictly enforced."""
        dev_settings = _settings(environment="development", demo_mode=False)
        principal = _principal(auth_time=None, amr=[])
        with pytest.raises(HTTPException) as exc_info:
            await require_step_up_mfa(principal=principal, settings=dev_settings)
        assert exc_info.value.status_code == 401
        assert exc_info.value.detail["reason"] == "no_auth_time_claim"

    async def test_demo_mode_compliance_officer_can_approve(self) -> None:
        """In explicit demo mode (demo_mode=True in dev), a local Compliance Officer can approve."""
        demo_settings = _settings(environment="development", demo_mode=True)
        principal = _principal(auth_time=None, amr=[])
        result = await require_step_up_mfa(principal=principal, settings=demo_settings)
        assert result is principal

    async def test_demo_mode_token_carries_step_up_mfa_claims(self) -> None:
        """create_access_token with demo claims round-trips through JWT decode and satisfies step-up."""
        from app.security.jwt import create_access_token, decode_access_token

        demo_settings = _settings(environment="development", demo_mode=True, jwt_issuer="regengine-ai", jwt_audience="regengine-ai-api")
        now = dt.datetime.now(dt.timezone.utc)
        token, _ = create_access_token(
            subject="officer.jane@regengine.dev",
            roles=[Role.COMPLIANCE_OFFICER],
            settings=demo_settings,
            signing_key=demo_settings.jwt_secret_key,
            auth_time=now,
            amr=["pwd", "mfa"],
        )
        payload = decode_access_token(token, demo_settings, local_verification_key=demo_settings.jwt_secret_key)
        assert payload.auth_time is not None
        assert "mfa" in payload.amr

    async def test_unauthorized_user_cannot_approve(self) -> None:
        """Machine credentials or unauthorized callers are forbidden from approval."""
        from app.security.dependencies import require_roles

        # Machine credential is explicitly rejected by step-up MFA even in demo mode
        demo_settings = _settings(environment="development", demo_mode=True)
        machine_principal = _principal(roles=[Role.BROKER_API_CLIENT], tenant_id="BROKER123")
        with pytest.raises(HTTPException) as exc_info:
            await require_step_up_mfa(principal=machine_principal, settings=demo_settings)
        assert exc_info.value.status_code == 403

        # Non-compliance officer role is rejected by require_roles
        role_check = require_roles(Role.COMPLIANCE_OFFICER)
        admin_principal = _principal(roles=[Role.SYSTEM_ADMIN])
        with pytest.raises(HTTPException) as exc_info:
            await role_check(principal=admin_principal)
        assert exc_info.value.status_code == 403

    async def test_production_still_requires_step_up_mfa(self) -> None:
        """In production, single-factor password-only local login is rejected by step-up MFA."""
        prod_settings = _settings(environment="production")
        now = dt.datetime.now(dt.timezone.utc)
        principal = _principal(auth_time=now, amr=["pwd"])
        with pytest.raises(HTTPException) as exc_info:
            await require_step_up_mfa(principal=principal, settings=prod_settings)
        assert exc_info.value.status_code == 401
        assert exc_info.value.detail["reason"] == "mfa_not_satisfied"

    async def test_forged_or_missing_claims_rejected_in_production(self) -> None:
        """In production, missing auth_time, stale auth_time, and invalid amr are rejected."""
        prod_settings = _settings(environment="production", step_up_mfa_max_age_seconds=60)

        # Missing auth_time
        p_no_auth = _principal(auth_time=None, amr=["mfa"])
        with pytest.raises(HTTPException) as exc:
            await require_step_up_mfa(principal=p_no_auth, settings=prod_settings)
        assert exc.value.status_code == 401
        assert exc.value.detail["reason"] == "no_auth_time_claim"

        # Stale auth_time
        p_stale = _principal(auth_time=dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1), amr=["mfa"])
        with pytest.raises(HTTPException) as exc:
            await require_step_up_mfa(principal=p_stale, settings=prod_settings)
        assert exc.value.status_code == 401
        assert exc.value.detail["reason"] == "auth_event_too_old"

        # Unrecognized / insufficient amr
        p_wrong_amr = _principal(auth_time=dt.datetime.now(dt.timezone.utc), amr=["single_factor", "sms_unverified"])
        with pytest.raises(HTTPException) as exc:
            await require_step_up_mfa(principal=p_wrong_amr, settings=prod_settings)
        assert exc.value.status_code == 401
        assert exc.value.detail["reason"] == "mfa_not_satisfied"

