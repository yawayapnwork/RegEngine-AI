"""Automated Directory Sync: maps enterprise identity provider group
membership (Okta/Azure AD/PingIdentity group claims, or Active Directory
groups synced into those IdPs) onto this system's internal RBAC roles.

Two layers, deliberately different latency/authority characteristics:

  1. Claims-based (this module's `resolve_roles_from_groups`) -- read
     directly off the ID/access token's group claim at every request,
     zero extra latency, zero extra infrastructure. This is the primary,
     standard OIDC pattern and correctly reflects group membership AS OF
     THE TOKEN'S ISSUANCE TIME.
  2. Proactive revocation (`app.security.directory_sync_job`) -- a
     periodic job that polls the IdP's own directory API (Okta Users API
     / Microsoft Graph) and writes a Redis override for any subject whose
     ACTUAL current group membership has since diverged from what a
     still-valid, not-yet-expired token claims -- e.g. an officer removed
     from `SEBI_Compliance_Team` mid-session must lose access before
     their token's `exp`, not just at next login. `resolve_roles_from_groups`
     is consulted first; the override (when present) replaces its result
     entirely rather than merging with it, since the override represents
     more current truth than the token's stale claim.
"""
from __future__ import annotations

import logging

from app.security.models import Role

logger = logging.getLogger(__name__)


def resolve_roles_from_groups(groups: list[str], group_role_map: dict[str, str]) -> list[Role]:
    """Maps IdP group names to `Role` values via `group_role_map`
    (settings.sso_directory_group_role_map, operator-maintained -- see
    that setting's docstring: which of an institution's AD groups should
    grant Compliance_Officer vs. System_Admin is a decision about YOUR
    organization's directory structure, not something inferable from the
    group name string alone, so this is never guessed or fuzzy-matched).

    A group name with no configured mapping is silently ignored (not an
    error) -- a user can legitimately belong to many AD groups this
    system has no opinion about. A user who ends up with NO mapped role
    at all is exactly the least-privilege-by-default outcome: an empty
    `roles` list, which `TokenPayload`'s `min_length=1` validator then
    rejects as an invalid token (see app.security.jwt) -- fail closed,
    never silently grant a default role to an unrecognized directory
    group set.
    """
    resolved: set[Role] = set()
    for group in groups:
        role_value = group_role_map.get(group)
        if role_value is None:
            continue
        try:
            resolved.add(Role(role_value))
        except ValueError:
            logger.error(
                "sso_directory_group_role_map maps group %r to %r, which is not a valid Role value -- check configuration.",
                group, role_value,
            )
    if not resolved:
        logger.warning("No configured directory group mapped to a role for groups=%s; token will be rejected (empty roles).", groups)
    return sorted(resolved, key=lambda r: r.value)
