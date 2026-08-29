"""Tests for enterprise SSO: multi-IdP provider registry, Automated
Directory Sync (group -> role mapping), and end-to-end external-token
decoding with a faked JWKS lookup (no real network call to an IdP)."""
from __future__ import annotations

import datetime as dt

import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app.config import Settings
from app.security import jwt as jwt_module
from app.security.directory_sync import resolve_roles_from_groups
from app.security.jwt import TokenInvalidError, decode_access_token
from app.security.models import Role
from app.security.sso_providers import build_sso_provider_registry

DEFAULT_GROUP_ROLE_MAP = {
    "SEBI_Compliance_Team": "Compliance_Officer",
    "IT_Audit_Group": "System_Admin",
}


def _sso_settings(**overrides) -> Settings:
    base = dict(
        jwt_algorithm="HS256",
        jwt_secret_key="test-secret-key-not-for-production",
        jwt_issuer="regengine-ai",
        jwt_audience="regengine-ai-api",
        sso_okta_issuer="https://acme.okta.com/oauth2/default",
        sso_okta_jwks_url="https://acme.okta.com/oauth2/default/v1/keys",
        sso_okta_audience="regengine-ai-api",
        sso_okta_group_claim="groups",
        sso_directory_group_role_map=DEFAULT_GROUP_ROLE_MAP,
    )
    base.update(overrides)
    return Settings(**base)


class TestDirectorySync:
    def test_known_groups_map_to_roles(self) -> None:
        roles = resolve_roles_from_groups(["SEBI_Compliance_Team"], DEFAULT_GROUP_ROLE_MAP)
        assert roles == [Role.COMPLIANCE_OFFICER]

    def test_multiple_groups_map_to_multiple_roles(self) -> None:
        roles = resolve_roles_from_groups(["SEBI_Compliance_Team", "IT_Audit_Group"], DEFAULT_GROUP_ROLE_MAP)
        assert set(roles) == {Role.COMPLIANCE_OFFICER, Role.SYSTEM_ADMIN}

    def test_unmapped_group_is_ignored(self) -> None:
        roles = resolve_roles_from_groups(["Some_Random_AD_Group"], DEFAULT_GROUP_ROLE_MAP)
        assert roles == []

    def test_no_matching_group_yields_empty_roles(self) -> None:
        assert resolve_roles_from_groups([], DEFAULT_GROUP_ROLE_MAP) == []


class TestProviderRegistry:
    def test_okta_provider_registered_by_issuer(self) -> None:
        settings = _sso_settings()
        registry = build_sso_provider_registry(settings)
        assert settings.sso_okta_issuer in registry
        provider = registry[settings.sso_okta_issuer]
        assert provider.name == "okta"
        assert provider.group_claim == "groups"

    def test_multiple_providers_coexist(self) -> None:
        settings = _sso_settings(
            sso_azure_ad_issuer="https://login.microsoftonline.com/tenant-id/v2.0",
            sso_azure_ad_jwks_url="https://login.microsoftonline.com/tenant-id/discovery/v2.0/keys",
            sso_azure_ad_audience="azure-client-id",
        )
        registry = build_sso_provider_registry(settings)
        assert len(registry) == 2
        assert registry["https://login.microsoftonline.com/tenant-id/v2.0"].name == "azure_ad"

    def test_unconfigured_providers_absent(self) -> None:
        settings = _sso_settings()
        registry = build_sso_provider_registry(settings)
        assert not any(p.name == "pingidentity" for p in registry.values())


class _FakeSigningKey:
    def __init__(self, key) -> None:
        self.key = key


class _FakeJWKSClient:
    def __init__(self, public_key) -> None:
        self._public_key = public_key

    def get_signing_key_from_jwt(self, token: str) -> _FakeSigningKey:
        return _FakeSigningKey(self._public_key)


@pytest.fixture()
def rsa_keypair():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM, format=serialization.PublicFormat.SubjectPublicKeyInfo
    )
    return private_key, public_pem


class TestExternalTokenDecoding:
    def _sign_okta_token(self, private_key, settings: Settings, **claim_overrides) -> str:
        now = dt.datetime.now(dt.timezone.utc)
        claims = {
            "sub": "officer.jane@acme.com",
            "iss": settings.sso_okta_issuer,
            "aud": settings.sso_okta_audience,
            "iat": now,
            "exp": now + dt.timedelta(hours=1),
            "groups": ["SEBI_Compliance_Team"],
            "auth_time": int(now.timestamp()),
            "amr": ["pwd", "mfa"],
        }
        claims.update(claim_overrides)
        return pyjwt.encode(claims, private_key, algorithm="RS256")

    def test_okta_token_maps_groups_to_roles(self, rsa_keypair, monkeypatch) -> None:
        private_key, public_pem = rsa_keypair
        settings = _sso_settings()
        token = self._sign_okta_token(private_key, settings)

        monkeypatch.setattr(jwt_module, "_jwks_client", lambda url: _FakeJWKSClient(public_pem))

        payload = decode_access_token(token, settings, local_verification_key=settings.jwt_secret_key)
        assert payload.roles == [Role.COMPLIANCE_OFFICER]
        assert payload.tenant_id is None
        assert payload.sub == "officer.jane@acme.com"
        assert "mfa" in payload.amr
        assert payload.auth_time is not None

    def test_okta_token_with_list_aud_is_normalized(self, rsa_keypair, monkeypatch) -> None:
        private_key, public_pem = rsa_keypair
        settings = _sso_settings()
        token = self._sign_okta_token(private_key, settings, aud=[settings.sso_okta_audience])
        monkeypatch.setattr(jwt_module, "_jwks_client", lambda url: _FakeJWKSClient(public_pem))

        payload = decode_access_token(token, settings, local_verification_key=settings.jwt_secret_key)
        assert payload.roles == [Role.COMPLIANCE_OFFICER]

    def test_okta_token_missing_jti_gets_synthesized(self, rsa_keypair, monkeypatch) -> None:
        private_key, public_pem = rsa_keypair
        settings = _sso_settings()
        token = self._sign_okta_token(private_key, settings)
        monkeypatch.setattr(jwt_module, "_jwks_client", lambda url: _FakeJWKSClient(public_pem))

        payload = decode_access_token(token, settings, local_verification_key=settings.jwt_secret_key)
        assert payload.jti  # non-empty, deterministically synthesized

    def test_okta_token_with_unmapped_group_is_rejected(self, rsa_keypair, monkeypatch) -> None:
        """Requirement 2's fail-closed behavior: a user whose IdP groups
        map to NO internal role must be rejected outright, not silently
        granted some default role."""
        private_key, public_pem = rsa_keypair
        settings = _sso_settings()
        token = self._sign_okta_token(private_key, settings, groups=["Unrelated_Group"])
        monkeypatch.setattr(jwt_module, "_jwks_client", lambda url: _FakeJWKSClient(public_pem))

        with pytest.raises(TokenInvalidError):
            decode_access_token(token, settings, local_verification_key=settings.jwt_secret_key)

    def test_token_from_unregistered_issuer_is_rejected(self, rsa_keypair, monkeypatch) -> None:
        private_key, public_pem = rsa_keypair
        settings = _sso_settings()
        now = dt.datetime.now(dt.timezone.utc)
        token = pyjwt.encode(
            {"sub": "x", "iss": "https://not-configured.example.com", "aud": "x", "iat": now, "exp": now + dt.timedelta(hours=1)},
            private_key, algorithm="RS256",
        )
        monkeypatch.setattr(jwt_module, "_jwks_client", lambda url: _FakeJWKSClient(public_pem))

        with pytest.raises(TokenInvalidError):
            decode_access_token(token, settings, local_verification_key=settings.jwt_secret_key)
