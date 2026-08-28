"""FastAPI dependencies for authentication (who is this) and authorization
(are they allowed here) -- the enforcement layer. `JWTAuthenticationMiddleware`
(app.security.middleware) authenticates opportunistically on every request
so tenant-aware rate limiting works even on routes that don't require a
principal; these dependencies are what actually returns 401/403.
"""
from __future__ import annotations

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.config import Settings, get_settings
from app.security.auth import authenticate_token
from app.security.models import Principal, Role

# auto_error=False: a missing Authorization header should fall through to
# our own 401 (with a consistent body/WWW-Authenticate header) rather than
# FastAPI's default, and lets genuinely public routes depend on nothing at
# all without ever touching this scheme.
_bearer_scheme = HTTPBearer(auto_error=False, description="Broker/Compliance Officer/Admin access token (JWT).")


async def get_current_principal(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
    settings: Settings = Depends(get_settings),
) -> Principal:
    """Resolves the authenticated Principal for this request, or raises
    401. Reuses `request.state.principal` if `JWTAuthenticationMiddleware`
    already authenticated this request (the common case) instead of
    decoding the token twice."""
    existing = getattr(request.state, "principal", None)
    if existing is not None:
        return existing

    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    principal = await authenticate_token(credentials.credentials, settings)
    if principal is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    request.state.principal = principal
    return principal


def require_roles(*allowed_roles: Role):
    """Dependency factory: 403s unless the principal holds at least one of
    `allowed_roles`. Order every route lists roles in matters for
    readability only, not behavior -- this is an OR, not an AND, across
    `allowed_roles` (a principal with ANY matching role passes)."""

    async def _dependency(principal: Principal = Depends(get_current_principal)) -> Principal:
        if not principal.has_role(*allowed_roles):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Requires one of roles: {[r.value for r in allowed_roles]}.",
            )
        return principal

    return _dependency


def require_tenant_scope(tenant_id: str, principal: Principal = Depends(get_current_principal)) -> Principal:
    """Enforces that a Broker_API_Client principal only ever acts on its
    OWN tenant's data -- e.g. a transaction's broker_id, a batch's
    submitting tenant. System_Admin and Compliance_Officer are exempt
    (they are not tenant-scoped identities); a Broker_API_Client whose
    token tenant_id doesn't match the resource's tenant_id is a
    cross-tenant access attempt, not a routine 403 -- logged as such by
    the caller.

    Usage: pass the resource's tenant/broker id explicitly, e.g.
        require_tenant_scope(transaction.broker_id) as a route-body check,
    or via a small wrapper dependency when the id comes from a path param.
    """
    if principal.is_admin() or Role.COMPLIANCE_OFFICER in principal.roles:
        return principal
    if principal.tenant_id != tenant_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Token tenant_id does not match the requested resource's tenant.",
        )
    return principal
