"""Pydantic auth models: roles, JWT claim shapes, and the OAuth2 token
issuance/introspection contracts.

Role design
-----------
Three roles, matching the three distinct trust boundaries this system
actually has:

  Compliance_Officer   Human, SSO-authenticated. The only role permitted to
                        resolve a flagged transaction (app.execution) or
                        approve/reject a compiled policy awaiting HITL
                        review (app.db.models.HITLReview) before it goes
                        live. Never machine-to-machine.
  Broker_API_Client     Machine, OAuth2 client_credentials-authenticated.
                        One per broker tenant. Reads/executes compiled
                        policy (POST /v1/execution/transactions/evaluate,
                        /batches, /cdc/events) scoped to its own
                        tenant_id -- never another broker's data.
  System_Admin          Human or a break-glass service account. Manages
                        infrastructure/agents: triggers ingestion polls,
                        (re)parses/indexes circulars, and can act as an
                        operational escape hatch for execution endpoints,
                        but is deliberately EXCLUDED from HITL policy
                        approval -- that authority belongs to
                        Compliance_Officer alone, not to whoever holds
                        infra access (least-privilege separation of duties,
                        the standard SOX/SEBI-audit control this maps to).
"""
from __future__ import annotations

import datetime as dt
from enum import Enum

from pydantic import BaseModel, Field, field_validator


class Role(str, Enum):
    COMPLIANCE_OFFICER = "Compliance_Officer"
    BROKER_API_CLIENT = "Broker_API_Client"
    SYSTEM_ADMIN = "System_Admin"


class TokenPayload(BaseModel):
    """The validated shape of every access token's claims (RFC 7519
    registered claims + this system's custom ones). This is what
    `app.security.jwt.decode_access_token` returns and what
    `Principal` is built from -- never trust a claim that didn't pass
    through this model's validation."""

    sub: str = Field(..., description="Subject: user id (compliance officer/admin) or OAuth2 client_id (broker tenant).")
    roles: list[Role] = Field(..., min_length=1)
    tenant_id: str | None = Field(
        None, description="Required and enforced for Broker_API_Client; None for human roles."
    )
    scope: list[str] = Field(default_factory=list)
    iss: str
    aud: str
    iat: dt.datetime
    exp: dt.datetime
    jti: str = Field(..., description="Unique token id; enables server-side revocation lookups if ever needed.")

    # OIDC step-up MFA support (app.security.step_up) -- populated for
    # external SSO tokens (app.security.jwt._normalize_external_claims);
    # absent/empty for self-issued Broker_API_Client tokens, which never
    # go through step-up (machine credentials have no "MFA prompt" to
    # step up to).
    auth_time: dt.datetime | None = Field(None, description="OIDC `auth_time`: when the end-user last actively authenticated at the IdP.")
    amr: list[str] = Field(default_factory=list, description="OIDC `amr` (RFC 8176): authentication methods used, e.g. ['pwd','otp'].")

    @field_validator("tenant_id")
    @classmethod
    def _tenant_id_required_for_broker(cls, v: str | None, info) -> str | None:
        roles = info.data.get("roles") or []
        if Role.BROKER_API_CLIENT in roles and not v:
            raise ValueError("tenant_id is required when roles include Broker_API_Client")
        return v


class Principal(BaseModel):
    """The authenticated identity attached to `request.state.principal` by
    `JWTAuthenticationMiddleware`, and what every `require_roles(...)`
    dependency and route handler actually reads. Deliberately a narrower,
    request-scoped view of `TokenPayload` -- handlers should never need to
    reach back into raw JWT claims."""

    subject: str
    roles: list[Role]
    tenant_id: str | None = None
    token_id: str
    auth_time: dt.datetime | None = None
    amr: list[str] = Field(default_factory=list)

    def has_role(self, *roles: Role) -> bool:
        return any(r in self.roles for r in roles)

    def is_admin(self) -> bool:
        return Role.SYSTEM_ADMIN in self.roles

    model_config = {"frozen": True}


class TokenResponse(BaseModel):
    """RFC 6749 §5.1 access token response, returned by POST /v1/auth/token."""

    access_token: str
    token_type: str = "bearer"
    expires_in: int = Field(..., description="Seconds until expiry, from issuance.")
    scope: str | None = None


class ClientCredentialsRequest(BaseModel):
    """RFC 6749 §4.4 client_credentials grant -- how a broker's system
    authenticates as its `Broker_API_Client` tenant. Sent as a JSON body
    here rather than the RFC's application/x-www-form-urlencoded for
    consistency with the rest of this API's request bodies; the semantics
    are identical."""

    grant_type: str = Field(..., pattern="^client_credentials$")
    client_id: str
    client_secret: str


class LoginRequest(BaseModel):
    """Standalone email/password login -- POST /v1/auth/login. Verified
    against app.security.local_user_store, entirely independent of any
    external SSO IdP."""

    email: str
    password: str


class LocalUser(BaseModel):
    """A locally-provisioned human account (Compliance_Officer/System_Admin),
    as stored (password hashed, never plaintext) in app.security.local_user_store.
    Not exposed directly in any API response."""

    email: str
    password_hash: str
    roles: list[Role] = Field(default_factory=lambda: [Role.COMPLIANCE_OFFICER])
    disabled: bool = False


class TenantClient(BaseModel):
    """A registered broker tenant's OAuth2 client, as stored (secret
    hashed, never plaintext) in the tenant client store -- see
    app.security.tenant_store. Not exposed directly in any API response."""

    client_id: str
    tenant_id: str
    client_secret_hash: str
    roles: list[Role] = Field(default_factory=lambda: [Role.BROKER_API_CLIENT])
    disabled: bool = False
