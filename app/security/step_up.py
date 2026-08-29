"""Step-up MFA (Requirement 3): a FastAPI dependency that gates a specific
high-privilege operation -- approving a compiled OPA policy in HITL is
the concrete example this system has -- behind a RECENT, MFA-satisfying
authentication event, independent of whether the caller's bearer token
itself is still validly signed and unexpired.

Why this can't just be `require_roles(Role.COMPLIANCE_OFFICER)`: that
only proves the caller currently HOLDS a valid Compliance_Officer token,
which could have been issued hours ago from a single password+MFA prompt
at the start of a long session. Approving a policy that will govern live
production trade evaluation is exactly the kind of action enterprise
security policy expects to require FRESH proof of the human's presence
and a real (not stale) MFA factor -- the same rationale AWS/GCP apply to
their own "MFA required for this action" IAM conditions.

Reads two OIDC claims threaded through from `app.security.jwt` for
external SSO tokens (`Principal.auth_time`, `Principal.amr`):
  - `auth_time`: when the end-user last actively authenticated at the IdP.
  - `amr`: which authentication methods were used for that event.
A token whose authentication event is older than
`settings.step_up_mfa_max_age_seconds`, or whose `amr` contains none of
`settings.step_up_required_amr_values`, fails the check and the caller
must re-authenticate at the IdP (typically with `prompt=login` and/or an
`acr_values` hint requesting a fresh MFA challenge) before retrying.
"""
from __future__ import annotations

import datetime as dt
import logging

from fastapi import Depends, HTTPException, status

from app.config import Settings, get_settings
from app.security.dependencies import get_current_principal
from app.security.models import Principal

logger = logging.getLogger(__name__)


def _step_up_challenge(settings: Settings, reason: str) -> HTTPException:
    detail = {
        "detail": "Step-up authentication required for this operation.",
        "reason": reason,
    }
    if settings.step_up_redirect_base_url:
        detail["step_up_url"] = settings.step_up_redirect_base_url
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": 'Bearer error="step_up_required", error_description="fresh_mfa_required"'},
    )


async def require_step_up_mfa(
    principal: Principal = Depends(get_current_principal),
    settings: Settings = Depends(get_settings),
) -> Principal:
    """Drop-in replacement for `Depends(get_current_principal)` /
    `Depends(require_roles(...))` on a route that needs BOTH a role check
    and step-up MFA -- compose it with `require_roles` at the route level,
    e.g.:

        @router.post("/{review_id}/approve")
        async def approve_review(
            ...,
            principal: Principal = Depends(require_roles(Role.COMPLIANCE_OFFICER)),
            _stepped_up: Principal = Depends(require_step_up_mfa),
        ): ...

    (Two separate Depends rather than one combined dependency so the role
    check's 403 and the step-up check's 401 stay independently
    meaningful -- a caller with the wrong role should never learn whether
    step-up would also have been required.)
    """
    if principal.tenant_id is not None:
        # Machine (Broker_API_Client) principal: no human MFA concept
        # applies. A route requiring step-up MFA should never be
        # reachable by a machine credential in the first place (its
        # require_roles(...) dependency already excludes
        # Broker_API_Client) -- this is a defense-in-depth guard against
        # that dependency ever being misconfigured, not the primary check.
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Step-up MFA is not applicable to machine credentials.")

    if principal.auth_time is None:
        raise _step_up_challenge(settings, "no_auth_time_claim")

    age_seconds = (dt.datetime.now(dt.timezone.utc) - principal.auth_time).total_seconds()
    if age_seconds > settings.step_up_mfa_max_age_seconds:
        raise _step_up_challenge(settings, "auth_event_too_old")

    if not set(principal.amr) & set(settings.step_up_required_amr_values):
        raise _step_up_challenge(settings, "mfa_not_satisfied")

    return principal
