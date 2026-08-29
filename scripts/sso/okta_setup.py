#!/usr/bin/env python3
"""Okta integration setup/verification script.

Checklist this script walks through:
  1. Confirm the Okta Authorization Server's OIDC discovery document is
     reachable and its issuer/JWKS match what you're about to configure.
  2. Confirm the Okta Users API is reachable with the configured API
     token (needed for app.security.directory_sync_job's proactive
     group-membership revocation checks -- optional but recommended).
  3. Print the exact RegEngine AI settings to configure.

Prerequisites on the Okta side (done once, by an Okta org admin):
  - Create an OIDC application (Web or SPA, depending on your login
    flow) with a Groups Claim added to the ID/access token: Security >
    API > Authorization Servers > [server] > Claims > Add Claim, name
    "groups", value type "Groups", filter matching your compliance/audit
    group names (e.g. a regex like ".*Compliance.*|.*Audit.*").
  - (Optional, for directory_sync_job) Create an API token: Security >
    API > Tokens > Create Token, and grant it read access to Users/Groups.

Usage:
  python scripts/sso/okta_setup.py --org-url https://your-org.okta.com \\
      --auth-server default --api-token <token>
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.sso.verify_oidc_provider import fetch_discovery_document, fetch_jwks


def check_users_api(org_url: str, api_token: str) -> bool:
    url = f"{org_url.rstrip('/')}/api/v1/users?limit=1"
    try:
        resp = httpx.get(url, headers={"Authorization": f"SSWS {api_token}"}, timeout=10.0)
        resp.raise_for_status()
        return True
    except httpx.HTTPError as exc:
        print(f"Okta Users API check FAILED: {exc}", file=sys.stderr)
        return False


def main() -> None:
    parser = argparse.ArgumentParser(description="Verify Okta OIDC + Users API configuration for RegEngine AI.")
    parser.add_argument("--org-url", required=True, help="e.g. https://your-org.okta.com")
    parser.add_argument("--auth-server", default="default", help="Okta Authorization Server name")
    parser.add_argument("--api-token", default=None, help="Okta API token, for the Users API check (optional)")
    args = parser.parse_args()

    discovery_url = f"{args.org_url.rstrip('/')}/oauth2/{args.auth_server}/.well-known/openid-configuration"
    print(f"[1/3] OIDC discovery: {discovery_url}")
    discovery = fetch_discovery_document(discovery_url)
    issuer = discovery["issuer"]
    jwks_uri = discovery["jwks_uri"]
    fetch_jwks(jwks_uri)
    print(f"      issuer={issuer}")
    print(f"      jwks_uri={jwks_uri}")
    print("      OK")

    if args.api_token:
        print("[2/3] Okta Users API reachability (for app.security.directory_sync_job) ...")
        ok = check_users_api(args.org_url, args.api_token)
        print("      OK" if ok else "      FAILED (directory_sync_job will not be able to poll group membership)")
    else:
        print("[2/3] Skipped (no --api-token supplied) -- directory_sync_job's Okta polling will be inactive.")

    print("\n[3/3] Configure RegEngine AI with:")
    print(f"  SSO_OKTA_ISSUER={issuer}")
    print(f"  SSO_OKTA_JWKS_URL={jwks_uri}")
    print("  SSO_OKTA_AUDIENCE=<your app's configured audience/API identifier>")
    print("  SSO_OKTA_GROUP_CLAIM=groups   # or whatever you named the claim in the Okta admin console")
    if args.api_token:
        print(f"  OKTA_ORG_URL={args.org_url}")
        print("  OKTA_API_TOKEN=<the token you just verified>")


if __name__ == "__main__":
    main()
