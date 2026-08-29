"""Requirement 1's "instantly revokes API access": a Starlette
middleware that rejects every request against an active global or
tenant-specific kill switch with 503, mounted the same way as every
other cross-cutting concern in app/security/middleware.py (see that
module's docstring for the mount-order rationale this middleware slots
into: after JWTAuthentication, since it needs `request.state.principal`
to resolve a tenant-specific switch, and before SessionManagement/
TenantRateLimit/PayloadEncryption, since a killed request should not
consume rate-limit budget or attempt payload decryption at all).

CRITICAL: the governance control routes themselves
(`/v1/governance/kill-switch/*`) are always exempt from this check --
an active kill switch that also blocked the very API used to
DEACTIVATE it would be a self-inflicted lockout, not a safety control.
"""
from __future__ import annotations

import logging

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp

from app.config import Settings
from app.governance.kill_switch import KillSwitchStore

logger = logging.getLogger(__name__)

_ALWAYS_EXEMPT_PREFIXES = ("/v1/governance/kill-switch",)


class KillSwitchMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: ASGIApp, settings: Settings, kill_switch_store: KillSwitchStore) -> None:
        super().__init__(app)
        self._settings = settings
        self._store = kill_switch_store

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        path = request.url.path
        if path in self._settings.rate_limit_exempt_paths or any(path.startswith(p) for p in _ALWAYS_EXEMPT_PREFIXES):
            return await call_next(request)

        principal = getattr(request.state, "principal", None)
        tenant_id = principal.tenant_id if principal is not None else None

        if await self._store.is_active_for(tenant_id):
            logger.info("Request to %s rejected: kill switch active for tenant_id=%s.", path, tenant_id)
            return JSONResponse(
                status_code=503,
                content={
                    "detail": "This system's automated execution is currently halted by an active governance kill switch. "
                    "Submissions are not being processed automatically; contact your compliance officer for manual "
                    "handling.",
                    "kill_switch_active": True,
                },
                headers={"Retry-After": "300"},
            )

        return await call_next(request)
