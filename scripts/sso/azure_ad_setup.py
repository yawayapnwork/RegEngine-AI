#!/usr/bin/env python3
"""Azure AD (Microsoft Entra ID) integration setup/verification script.

Checklist this script walks through:
  1. Confirm the tenant's v2.0 OIDC discovery document is reachable and
     its issuer/JWKS match what you're about to configure.
  2. Confirm a Microsoft Graph app-only token can be acquired with the
     configured client credentials (needed for
     app.security.directory_sync_job's proactive group-membership
     revocation checks -- optional but recommended).
  3. Print the exact RegEngine AI settings to configure.

Prerequisites on the Azure AD side (done once, by a tenant admin):
  - Register an application (Azure Portal > Microsoft Entra ID > App
    registrations > New registration).
  - Add a Groups claim to the ID token: Token configuration > Add
    groups claim > Security groups (or "All groups" if using
    directory-synced groups) -- Azure AD emits group OBJECT IDs by
    default, not display names, so `sso_directory_group_role_map` must
    then be keyed on group object IDs rather than names, OR configure
    the optional claim to emit `sAMAccountName`/display name if your
    tenant and group count are small enough for Azure AD to support that
    (Azure AD falls back to a `hasgroups`/overage claim + Graph lookup
    for users in >200 groups either way -- this script's Graph check in
    step 2 is what app.security.directory_sync_job relies on for that
    case too).
  - Grant the application `GroupMember.Read.All` (Application permission)
    on Microsoft Graph, with admin consent, for directory_sync_job.

Usage:
  python scripts/sso/azure_ad_setup.py --tenant-id <tenant-guid> \\
      --client-id <app-client-id> --client-secret <secret>
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.sso.verify_oidc_provider import fetch_discovery_document, fetch_jwks


def check_graph_app_token(tenant_id: str, client_id: str, client_secret: str) -> bool:
    url = f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
    data = {
        "client_id": client_id,
        "client_secret": client_secret,
        "scope": "https://graph.microsoft.com/.default",
        "grant_type": "client_credentials",
    }
    try:
        resp = httpx.post(url, data=data, timeout=10.0)
        resp.raise_for_status()
        return "access_token" in resp.json()
    except httpx.HTTPError as exc:
        print(f"Microsoft Graph app-only token acquisition FAILED: {exc}", file=sys.stderr)
        return False


def main() -> None:
    parser = argparse.ArgumentParser(description="Verify Azure AD OIDC + Microsoft Graph configuration for RegEngine AI.")
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--client-id", required=True, help="The app registration's Application (client) ID -- becomes sso_azure_ad_audience")
    parser.add_argument("--client-secret", default=None, help="For the Microsoft Graph app-only token check (optional)")
    args = parser.parse_args()

    discovery_url = f"https://login.microsoftonline.com/{args.tenant_id}/v2.0/.well-known/openid-configuration"
    print(f"[1/3] OIDC discovery: {discovery_url}")
    discovery = fetch_discovery_document(discovery_url)
    issuer = discovery["issuer"]
    jwks_uri = discovery["jwks_uri"]
    fetch_jwks(jwks_uri)
    print(f"      issuer={issuer}")
    print(f"      jwks_uri={jwks_uri}")
    print("      OK")

    if args.client_secret:
        print("[2/3] Microsoft Graph app-only token acquisition (for app.security.directory_sync_job) ...")
        ok = check_graph_app_token(args.tenant_id, args.client_id, args.client_secret)
        print("      OK" if ok else "      FAILED (directory_sync_job will not be able to poll group membership -- check GroupMember.Read.All admin consent)")
    else:
        print("[2/3] Skipped (no --client-secret supplied) -- directory_sync_job's Azure AD polling will be inactive.")

    print("\n[3/3] Configure RegEngine AI with:")
    print(f"  SSO_AZURE_AD_ISSUER={issuer}")
    print(f"  SSO_AZURE_AD_JWKS_URL={jwks_uri}")
    print(f"  SSO_AZURE_AD_AUDIENCE={args.client_id}")
    print("  SSO_AZURE_AD_GROUP_CLAIM=groups")
    print(
        "  NOTE: Azure AD's 'groups' claim typically contains group OBJECT IDs, not display names -- "
        "verify with scripts/sso/verify_oidc_provider.py --sample-token <token> and key "
        "sso_directory_group_role_map on whatever form your tenant actually emits."
    )
    if args.client_secret:
        print(f"  AZURE_AD_TENANT_ID={args.tenant_id}")
        print(f"  AZURE_AD_CLIENT_ID={args.client_id}")
        print("  AZURE_AD_CLIENT_SECRET=<the secret you just verified>")


if __name__ == "__main__":
    main()
