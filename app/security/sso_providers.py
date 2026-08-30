"""Registry of configured enterprise OIDC identity providers (Okta, Azure
AD / Microsoft Entra ID, PingIdentity) -- generalizes
`app.security.jwt`'s original single-external-issuer design into an
issuer-keyed lookup, so a deployment can trust tokens from more than one
institutional intermediary's IdP simultaneously.

Deliberately NOT using `Authlib`'s `OAuth` registry client (which is built
around an interactive authorization-code flow -- redirecting a browser to
the IdP and handling the callback) for this part: this module only needs
to VERIFY an already-issued ID/access token's signature via JWKS, which
`PyJWT` + `PyJWKClient` already does (see app.security.jwt) without
pulling in Authlib's session/state-management machinery for a flow this
service doesn't itself drive. Authlib is used instead in
app.api.sso_login_routes for the actual browser-redirect OIDC login flow,
where its `OAuth` client genuinely earns its place.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.config import Settings


@dataclass(frozen=True)
class SSOProviderConfig:
    name: str  # "okta" | "azure_ad" | "pingidentity" | "generic"
    issuer: str
    jwks_url: str
    audience: str | None
    algorithms: tuple[str, ...]
    group_claim: str


def build_sso_provider_registry(settings: Settings) -> dict[str, SSOProviderConfig]:
    """Keyed by `issuer` (the exact string an ID token's `iss` claim must
    match) -- `app.security.jwt.decode_access_token` looks a token up by
    this key before attempting JWKS verification against it. Only
    providers with BOTH an issuer and a jwks_url configured are included;
    a deployment using only Okta never pays any cost (not even a dict
    entry) for Azure AD/PingIdentity being unconfigured.
    """
    algorithms = tuple(settings.sso_external_algorithms)
    registry: dict[str, SSOProviderConfig] = {}

    if settings.sso_okta_issuer and settings.sso_okta_jwks_url:
        registry[settings.sso_okta_issuer] = SSOProviderConfig(
            name="okta", issuer=settings.sso_okta_issuer, jwks_url=settings.sso_okta_jwks_url,
            audience=settings.sso_okta_audience, algorithms=algorithms, group_claim=settings.sso_okta_group_claim,
        )

    if settings.sso_azure_ad_issuer and settings.sso_azure_ad_jwks_url:
        registry[settings.sso_azure_ad_issuer] = SSOProviderConfig(
            name="azure_ad", issuer=settings.sso_azure_ad_issuer, jwks_url=settings.sso_azure_ad_jwks_url,
            audience=settings.sso_azure_ad_audience, algorithms=algorithms, group_claim=settings.sso_azure_ad_group_claim,
        )

    if settings.sso_pingidentity_issuer and settings.sso_pingidentity_jwks_url:
        registry[settings.sso_pingidentity_issuer] = SSOProviderConfig(
            name="pingidentity", issuer=settings.sso_pingidentity_issuer, jwks_url=settings.sso_pingidentity_jwks_url,
            audience=settings.sso_pingidentity_audience, algorithms=algorithms, group_claim=settings.sso_pingidentity_group_claim,
        )

    if settings.sso_auth0_issuer and settings.sso_auth0_jwks_url:
        registry[settings.sso_auth0_issuer] = SSOProviderConfig(
            name="auth0", issuer=settings.sso_auth0_issuer, jwks_url=settings.sso_auth0_jwks_url,
            audience=settings.sso_auth0_audience, algorithms=algorithms, group_claim=settings.sso_auth0_group_claim,
        )

    # Backward-compatible single-issuer configuration (jwt_external_issuer/
    # jwt_jwks_url) -- only added if that issuer isn't already registered
    # above under one of the named providers, so a deployment migrating
    # from the old single-issuer settings to a named block doesn't end up
    # with the same issuer registered twice with different group claims.
    if settings.jwt_external_issuer and settings.jwt_jwks_url and settings.jwt_external_issuer not in registry:
        registry[settings.jwt_external_issuer] = SSOProviderConfig(
            name="generic", issuer=settings.jwt_external_issuer, jwks_url=settings.jwt_jwks_url,
            audience=settings.jwt_audience, algorithms=tuple(settings.jwt_external_algorithms), group_claim="groups",
        )

    return registry
