"""SAML 2.0 SP endpoints for institutional intermediaries whose IdP speaks
SAML rather than OIDC (common for older enterprise Active Directory
Federation Services / ADFS deployments alongside Okta/Azure AD's more
modern OIDC path).

Uses `python3-saml` (the OneLogin-maintained SAML toolkit) for assertion
parsing and XML-DSig signature validation -- SAML's XML canonicalization
and signature-wrapping attack surface (XXE, XML Signature Wrapping) is
exactly the kind of thing this project deliberately does NOT hand-roll;
see `app.regulatory.taxonomy`'s and `app.ledger.hash_chain`'s docstrings
for the same "delegate to a vetted library rather than write our own
crypto/parsing" principle applied elsewhere in this codebase.

Bridges a validated SAML assertion into this service's OWN self-issued
JWT (via app.security.jwt.create_access_token) rather than inventing a
second, SAML-native session representation -- every other part of the
application (RBAC dependencies, session management, step-up MFA) then
only ever has to understand one token format, regardless of whether the
human authenticated via OIDC (Okta/Azure AD/PingIdentity, see
app.security.jwt's external-issuer path) or SAML.

Gated behind `settings.saml_enabled` (default False): a deployment using
only OIDC IdPs never needs `python3-saml` installed at all, and this
router is not even mounted (see app.main) when the setting is off.
"""
from __future__ import annotations

import asyncio
import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse, Response

from app.config import Settings, get_settings
from app.security.directory_sync import resolve_roles_from_groups
from app.security.jwt import create_access_token
from app.security.models import Role, TokenResponse
from app.security.secrets import resolve_secret

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/auth/saml", tags=["auth-saml"])


def _saml_settings_dict(settings: Settings) -> dict:
    return {
        "strict": True,  # rejects assertions with anything less than a fully valid signature/timing/audience -- never relax this
        "debug": False,
        "sp": {
            "entityId": settings.saml_sp_entity_id,
            "assertionConsumerService": {
                "url": settings.saml_sp_acs_url,
                "binding": "urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST",
            },
            "NameIDFormat": "urn:oasis:names:tc:SAML:1.1:nameid-format:emailAddress",
            "x509cert": settings.saml_sp_x509_cert or "",
            "privateKey": settings.saml_sp_private_key or "",
        },
        "idp": {
            "entityId": settings.saml_idp_entity_id,
            "singleSignOnService": {
                "url": settings.saml_idp_sso_url,
                "binding": "urn:oasis:names:tc:SAML:2.0:bindings:HTTP-Redirect",
            },
            "x509cert": settings.saml_idp_x509_cert,
        },
    }


async def _fastapi_request_to_saml_request(request: Request) -> dict:
    """Adapts a FastAPI/Starlette `Request` into the plain dict
    `OneLogin_Saml2_Auth` expects (it was designed for Flask/Django's
    request objects, so no FastAPI-native support exists)."""
    form = await request.form() if request.method == "POST" else {}
    return {
        "https": "on" if request.url.scheme == "https" else "off",
        "http_host": request.url.hostname,
        "server_port": request.url.port or (443 if request.url.scheme == "https" else 80),
        "script_name": request.url.path,
        "get_data": dict(request.query_params),
        "post_data": dict(form),
    }


def _require_saml_enabled(settings: Settings) -> None:
    if not settings.saml_enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="SAML SSO is not enabled on this deployment.")


@router.get("/login")
async def saml_login(request: Request, settings: Settings = Depends(get_settings)) -> RedirectResponse:
    """Redirects the browser to the IdP's Single Sign-On URL with a
    signed AuthnRequest. The institutional intermediary's browser
    completes authentication (including any MFA challenge the IdP itself
    enforces) entirely at the IdP; this service never sees credentials."""
    _require_saml_enabled(settings)
    from onelogin.saml2.auth import OneLogin_Saml2_Auth  # deferred: only import when SAML is actually enabled

    saml_request = await _fastapi_request_to_saml_request(request)
    auth = OneLogin_Saml2_Auth(saml_request, _saml_settings_dict(settings))
    return RedirectResponse(url=auth.login())


@router.post("/acs")
async def saml_acs(request: Request, settings: Settings = Depends(get_settings)) -> TokenResponse:
    """Assertion Consumer Service: the IdP POSTs the signed SAML Response
    here after successful authentication. Validates it (signature,
    timing, audience -- all via python3-saml, `strict: True`), maps the
    asserted group attribute to internal RBAC roles (Automated Directory
    Sync, same app.security.directory_sync module the OIDC path uses),
    and mints a self-issued access token so the rest of the application
    never has to know this session originated from SAML rather than OIDC.
    """
    _require_saml_enabled(settings)
    from onelogin.saml2.auth import OneLogin_Saml2_Auth  # deferred: see saml_login

    saml_request = await _fastapi_request_to_saml_request(request)
    auth = OneLogin_Saml2_Auth(saml_request, _saml_settings_dict(settings))
    auth.process_response()

    errors = auth.get_errors()
    if errors:
        logger.warning("SAML assertion rejected: %s (%s)", errors, auth.get_last_error_reason())
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="SAML assertion validation failed.")
    if not auth.is_authenticated():
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="SAML authentication was not completed.")

    name_id = auth.get_nameid()
    attributes = auth.get_attributes()
    groups = attributes.get(settings.saml_group_attribute_name, [])
    roles = resolve_roles_from_groups(groups, settings.sso_directory_group_role_map)
    if not roles:
        logger.warning("SAML subject %s has no directory group mapped to a role (groups=%s); rejecting.", name_id, groups)
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="No RegEngine role is mapped for this account's directory groups.")

    secret_name = "jwt_secret_key" if settings.jwt_algorithm.startswith("HS") else "jwt_private_key_pem"
    signing_key = await asyncio.to_thread(resolve_secret, secret_name, settings=settings)

    # `auth_time`/`amr` are set to "now"/"pwd" here as a floor, not a
    # claim of fact about the IdP's own MFA state: SAML 2.0's
    # AuthnStatement DOES carry an AuthnContextClassRef that can indicate
    # MFA was used, but mapping every IdP's particular AuthnContext URI
    # vocabulary to OIDC's standardized `amr` values is IdP-specific
    # configuration this module deliberately leaves as an extension
    # point (see the TODO below) rather than guessing. Until that mapping
    # is configured for a given IdP, a SAML-authenticated session will
    # never satisfy app.security.step_up's MFA check -- fails closed, not
    # open.
    # TODO: parse auth.get_last_assertion_xml() for the AuthnContextClassRef
    # and map it to `amr` per this IdP's documented context class URIs.
    access_token, _ = create_access_token(
        subject=name_id,
        roles=roles,
        settings=settings,
        signing_key=signing_key,
        tenant_id=None,
    )
    logger.info("SAML SSO login succeeded for subject=%s roles=%s", name_id, [r.value for r in roles])
    return TokenResponse(access_token=access_token, expires_in=settings.jwt_access_token_ttl_seconds, scope=None)


@router.get("/metadata")
async def saml_sp_metadata(settings: Settings = Depends(get_settings)) -> Response:
    """This SP's own metadata XML, for the IdP administrator to import
    when configuring the RegEngine AI application on their end."""
    _require_saml_enabled(settings)
    from onelogin.saml2.settings import OneLogin_Saml2_Settings  # deferred: see saml_login

    saml_settings = OneLogin_Saml2_Settings(_saml_settings_dict(settings), sp_validation_only=True)
    errors = saml_settings.check_sp_settings(_saml_settings_dict(settings))
    if errors:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Invalid SP configuration: {errors}")
    return Response(content=saml_settings.get_sp_metadata(), media_type="application/xml")
