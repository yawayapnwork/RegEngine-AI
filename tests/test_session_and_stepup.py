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
        jwt_algorithm="HS256", jwt_secret_key="test-secret-key-not-for-production",
        session_idle_timeout_seconds=900, session_absolute_timeout_seconds=28800,
        step_up_mfa_max_age_seconds=300, step_up_required_amr_values=["mfa", "otp"],
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
