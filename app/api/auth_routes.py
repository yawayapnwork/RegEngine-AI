"""OAuth2 token issuance for Broker_API_Client tenants, standalone
email/password login for Compliance_Officer/System_Admin users (this
service's own local accounts -- see app.security.local_user_store), and a
token introspection endpoint. Human users MAY alternatively authenticate
via an external SSO IdP (Okta/Azure AD/PingIdentity, or SAML through
app.api.saml_routes) instead of the local login below -- both paths
converge on the same self-issued JWT shape (app.security.jwt), so every
other part of the application only ever has to understand one token format.
"""
from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.db.session import get_db_session
from app.security.dependencies import get_current_principal, require_roles
from app.security.jwt import create_access_token
from app.security.local_user_store import EmailAlreadyRegisteredError, LocalUserStore
from app.security.models import ClientCredentialsRequest, LoginRequest, Principal, Role, SignupResponse, TokenResponse
from app.security.secrets import resolve_secret
from app.security.tenant_store import TenantClientStore

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/auth", tags=["auth"])


def get_tenant_client_store(settings: Settings = Depends(get_settings)) -> TenantClientStore:
    from app.execution.dependencies import get_redis_pool  # reuse the shared process-wide Redis pool

    return TenantClientStore(redis_client=get_redis_pool(), key_prefix=settings.tenant_client_key_prefix)


def get_local_user_store(session: AsyncSession = Depends(get_db_session)) -> LocalUserStore:
    return LocalUserStore(session)


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


@router.post("/signup", response_model=SignupResponse, status_code=status.HTTP_201_CREATED)
async def signup(
    request: LoginRequest,
    users: LocalUserStore = Depends(get_local_user_store),
) -> SignupResponse:
    """Self-service account creation for the custom login page's sign-up
    flow: hashes the password (bcrypt, via LocalUserStore) and persists a
    new `User` row. Always provisions Compliance_Officer -- the least-
    privileged human role; a System_Admin account is never handed out
    through public signup, only via POST /v1/auth/users."""
    try:
        user_id = await users.register(request.email, request.password, roles=[Role.COMPLIANCE_OFFICER])
    except EmailAlreadyRegisteredError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    logger.info("Signup succeeded: user_id=%s", user_id)
    return SignupResponse(user_id=user_id)


@router.post("/login", response_model=TokenResponse, status_code=status.HTTP_200_OK)
async def login(
    request: LoginRequest,
    settings: Settings = Depends(get_settings),
    users: LocalUserStore = Depends(get_local_user_store),
) -> TokenResponse:
    """Standalone email/password login for the custom login page: verifies
    credentials against this service's own local account store (never an
    external IdP) and mints a self-issued access token, the same
    create_access_token path app.api.saml_routes bridges a SAML assertion
    through -- so RBAC/session handling downstream never needs to know
    which login path a given token came from."""
    user = await users.authenticate(request.email, request.password)
    if user is None:
        # Identical response whether the email is unknown or the password is
        # wrong -- see LocalUserStore.authenticate's docstring on why.
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid email or password.")

    signing_key = await _signing_key(settings)
    access_token, payload = create_access_token(
        subject=user.email,
        roles=user.roles,
        settings=settings,
        signing_key=signing_key,
        tenant_id=None,
    )
    logger.info("Local login succeeded for subject=%s roles=%s", user.email, [r.value for r in user.roles])

    return TokenResponse(
        access_token=access_token,
        expires_in=settings.jwt_access_token_ttl_seconds,
        scope=" ".join(payload.scope) or None,
    )


@router.post("/users", status_code=status.HTTP_201_CREATED)
async def create_local_user(
    request: LoginRequest,
    principal: Principal = Depends(require_roles(Role.SYSTEM_ADMIN)),
    users: LocalUserStore = Depends(get_local_user_store),
) -> dict:
    """Provisions a local Compliance_Officer account on an administrator's
    behalf -- the System_Admin-only counterpart to public signup, for
    provisioning an account for someone else (or, unlike POST
    /v1/auth/signup, promoting to System_Admin is only ever done by editing
    a `User` row directly -- there is deliberately no API surface that lets
    even a System_Admin self-elevate a new account straight to
    System_Admin over a network call)."""
    try:
        user_id = await users.register(request.email, request.password, roles=[Role.COMPLIANCE_OFFICER])
    except EmailAlreadyRegisteredError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    logger.info("Local user provisioned by %s: subject=%s", principal.subject, request.email)
    return {"user_id": user_id, "email": request.email.strip().lower(), "roles": [Role.COMPLIANCE_OFFICER.value]}


@router.get("/me", response_model=Principal)
async def introspect_self(principal: Principal = Depends(get_current_principal)) -> Principal:
    """Returns the caller's own resolved identity -- useful for the HITL
    review portal frontend to confirm "am I logged in as a
    Compliance_Officer" without decoding the JWT client-side, and for
    debugging a broker integration's token."""
    return principal
