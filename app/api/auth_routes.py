"""OAuth2 token issuance for Broker_API_Client tenants, and a token
introspection endpoint. Compliance_Officer / System_Admin users do NOT
authenticate here -- they hold tokens issued by the enterprise SSO
provider, verified via JWKS (see app.security.jwt's module docstring);
this service is only ever the identity source for machine (broker) clients.
"""
from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException, status

from app.config import Settings, get_settings
from app.security.dependencies import get_current_principal
from app.security.jwt import create_access_token
from app.security.models import ClientCredentialsRequest, Principal, Role, TokenResponse
from app.security.secrets import resolve_secret
from app.security.tenant_store import TenantClientStore

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/auth", tags=["auth"])


def get_tenant_client_store(settings: Settings = Depends(get_settings)) -> TenantClientStore:
    from app.execution.dependencies import get_redis_pool  # reuse the shared process-wide Redis pool

    return TenantClientStore(redis_client=get_redis_pool(), key_prefix=settings.tenant_client_key_prefix)


async def _signing_key(settings: Settings) -> str:
    secret_name = "jwt_secret_key" if settings.jwt_algorithm.startswith("HS") else "jwt_private_key_pem"
    return await asyncio.to_thread(resolve_secret, secret_name, settings=settings)


@router.post("/token", response_model=TokenResponse, status_code=status.HTTP_200_OK)
async def issue_token(
    request: ClientCredentialsRequest,
    settings: Settings = Depends(get_settings),
    tenant_clients: TenantClientStore = Depends(get_tenant_client_store),
) -> TokenResponse:
    """RFC 6749 §4.4 client_credentials grant. A broker's system
    authenticates as its registered `client_id`/`client_secret` (issued
    out-of-band during onboarding -- see app.security.tenant_store's
    module docstring: there is deliberately no public self-service
    signup) and receives a short-lived Broker_API_Client access token
    scoped to its own tenant_id."""
    client = await tenant_clients.authenticate(request.client_id, request.client_secret)
    if client is None:
        # Identical response whether client_id is unknown or the secret is
        # wrong -- see TenantClientStore.authenticate's docstring on why.
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid client credentials.")

    signing_key = await _signing_key(settings)
    access_token, payload = create_access_token(
        subject=client.client_id,
        roles=client.roles,
        settings=settings,
        signing_key=signing_key,
        tenant_id=client.tenant_id,
        scope=["execution:read", "execution:write"],
    )
    logger.info("Issued access token for tenant '%s' (client_id=%s, jti=%s)", client.tenant_id, client.client_id, payload.jti)

    return TokenResponse(
        access_token=access_token,
        expires_in=settings.jwt_access_token_ttl_seconds,
        scope=" ".join(payload.scope),
    )


@router.get("/me", response_model=Principal)
async def introspect_self(principal: Principal = Depends(get_current_principal)) -> Principal:
    """Returns the caller's own resolved identity -- useful for the HITL
    review portal frontend to confirm "am I logged in as a
    Compliance_Officer" without decoding the JWT client-side, and for
    debugging a broker integration's token."""
    return principal
