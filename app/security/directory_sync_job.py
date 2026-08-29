"""Automated Directory Sync's continuous half: periodically re-checks
CURRENTLY ACTIVE human sessions' real-time group membership against Okta
/ Microsoft Graph (Azure AD), and writes a Redis override
(`app.security.auth._directory_sync_override` reads it) whenever it has
diverged from what the session's original token claimed.

Why only active sessions, not "every user in the directory": this
service maintains no local Users table for humans (Compliance_Officer /
System_Admin principals are entirely JWT-derived, by design -- see
app.security.models.Role's module docstring on the three trust
boundaries). The set of subjects with an app.security.session_manager
Redis session IS the complete set of humans this system currently has
any live authorization state for; polling anyone else would be checking
group membership for a person who isn't (and can't be) using the system
right now anyway. This keeps the job's cost proportional to concurrent
active users, not total directory size.

Runs as a Celery-beat-scheduled task (see app.execution.celery_app,
`directory_sync_poll_interval_seconds`), never on the request path.
"""
from __future__ import annotations

import asyncio
import logging

import httpx
import redis.asyncio as redis

from app.config import Settings, get_settings
from app.execution.dependencies import get_redis_pool
from app.security.directory_sync import resolve_roles_from_groups
from app.security.models import Role

logger = logging.getLogger(__name__)


async def _active_session_subjects(redis_client: redis.Redis, session_key_prefix: str) -> dict[str, str]:
    """Returns {subject: token_id} for every session currently tracked by
    app.security.session_manager.SessionManager. Session keys are
    `<prefix>:<token_id>` hashes with a `created_at`/`last_activity_at`
    field but no `subject` field today -- rather than change that shape
    just for this job, subjects are instead discovered from whichever
    caller last authenticated them (this function is intentionally a
    narrow, swappable seam: a deployment with a real subject roster --
    e.g. synced from Okta's own user list -- can replace this with that
    roster directly)."""
    keys = [key async for key in redis_client.scan_iter(match=f"{session_key_prefix}:*")]
    subjects: dict[str, str] = {}
    for key in keys:
        token_id = key.split(":")[-1]
        subject = await redis_client.hget(key, "subject")
        if subject:
            subjects[subject] = token_id
    return subjects


async def fetch_okta_user_groups(subject_email: str, settings: Settings) -> list[str] | None:
    """Okta Users API: GET /api/v1/users/{id}/groups. `subject_email` is
    used as the user identifier -- Okta's API accepts either the user id
    or login (email) interchangeably for lookups. Returns None (not an
    empty list) on any failure, so the caller can distinguish "confirmed
    zero groups" from "couldn't check" and correctly choose NOT to write
    an override in the latter case (see docstring on fail-open-to-
    last-known-good below)."""
    if not settings.okta_org_url or not settings.okta_api_token:
        return None
    url = f"{settings.okta_org_url.rstrip('/')}/api/v1/users/{subject_email}/groups"
    headers = {"Authorization": f"SSWS {settings.okta_api_token}"}
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, headers=headers)
        resp.raise_for_status()
        return [group["profile"]["name"] for group in resp.json()]
    except httpx.HTTPError:
        logger.exception("Okta Users API group lookup failed for %s.", subject_email)
        return None


async def _azure_ad_app_token(settings: Settings) -> str | None:
    if not (settings.azure_ad_tenant_id and settings.azure_ad_client_id and settings.azure_ad_client_secret):
        return None
    url = f"https://login.microsoftonline.com/{settings.azure_ad_tenant_id}/oauth2/v2.0/token"
    data = {
        "client_id": settings.azure_ad_client_id,
        "client_secret": settings.azure_ad_client_secret,
        "scope": "https://graph.microsoft.com/.default",
        "grant_type": "client_credentials",
    }
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(url, data=data)
        resp.raise_for_status()
        return resp.json()["access_token"]
    except httpx.HTTPError:
        logger.exception("Failed to acquire a Microsoft Graph app-only token.")
        return None


async def fetch_azure_ad_user_groups(subject_upn: str, settings: Settings) -> list[str] | None:
    """Microsoft Graph: GET /v1.0/users/{upn}/memberOf, filtered to
    security groups' displayName. Requires an app registration with
    `GroupMember.Read.All` (application permission, admin-consented) --
    see scripts/sso/verify_azure_ad_config.py for a setup checklist."""
    token = await _azure_ad_app_token(settings)
    if token is None:
        return None
    url = f"https://graph.microsoft.com/v1.0/users/{subject_upn}/memberOf"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, headers={"Authorization": f"Bearer {token}"})
        resp.raise_for_status()
        return [g["displayName"] for g in resp.json().get("value", []) if g.get("displayName")]
    except httpx.HTTPError:
        logger.exception("Microsoft Graph memberOf lookup failed for %s.", subject_upn)
        return None


async def sync_active_sessions(settings: Settings | None = None) -> dict[str, list[str]]:
    """Re-checks every active session's subject against its IdP (guessed
    from whichever of Okta/Azure AD is configured -- a deployment with
    both would need a per-subject IdP hint this minimal version doesn't
    yet track) and writes a Redis role override for anyone whose CURRENT
    group membership no longer matches. Returns {subject: [role values]}
    for every subject an override was written for, for logging/testing.

    Fail-open-to-last-known-good: if the IdP API call itself fails (rate
    limited, network issue, revoked API token), NO override is written
    for that subject -- their existing token-claims-derived roles remain
    in effect until the next successful poll, rather than this job's own
    unavailability locking someone out.
    """
    settings = settings or get_settings()
    redis_client = get_redis_pool()
    subjects = await _active_session_subjects(redis_client, settings.session_key_prefix)

    written: dict[str, list[str]] = {}
    for subject, _token_id in subjects.items():
        groups = await fetch_okta_user_groups(subject, settings) or await fetch_azure_ad_user_groups(subject, settings)
        if groups is None:
            continue

        roles = resolve_roles_from_groups(groups, settings.sso_directory_group_role_map)
        override_key = f"{settings.directory_sync_override_key_prefix}:{subject}"
        await redis_client.set(
            override_key,
            ",".join(r.value for r in roles),
            ex=settings.directory_sync_poll_interval_seconds * 3,  # bounded staleness if this job itself stops running
        )
        written[subject] = [r.value for r in roles]
        if not roles:
            logger.warning("Directory sync: subject=%s now has NO mapped role (groups=%s); access will be revoked on next request.", subject, groups)

    return written


def run_sync_once() -> dict[str, list[str]]:
    """Synchronous entrypoint for the Celery beat task (see
    app.execution.celery_app's `directory-sync-poll` schedule entry)."""
    return asyncio.run(sync_active_sessions())
