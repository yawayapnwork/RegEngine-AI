#!/usr/bin/env python3
"""Generic OIDC identity-provider connectivity/config verification --
works against Okta, Azure AD (Microsoft Entra ID), PingIdentity, or any
other spec-compliant OIDC provider, since all of them publish the same
`.well-known/openid-configuration` discovery document.

Fetches the discovery document, confirms the `issuer` it declares matches
what you intend to configure in RegEngine AI (a mismatch here is the #1
cause of "why does every token get rejected as unrecognized issuer"), and
optionally decodes-without-verifying a sample token to show its claims
(so you can confirm the group claim name actually appears before wiring
up settings.sso_*_group_claim).

Usage:
  python scripts/sso/verify_oidc_provider.py \\
      --discovery-url https://your-org.okta.com/oauth2/default/.well-known/openid-configuration \\
      --expected-issuer https://your-org.okta.com/oauth2/default

  python scripts/sso/verify_oidc_provider.py \\
      --discovery-url https://your-org.okta.com/oauth2/default/.well-known/openid-configuration \\
      --sample-token eyJhbGciOi...
"""
from __future__ import annotations

import argparse
import json
import sys

import httpx
import jwt as pyjwt


def fetch_discovery_document(discovery_url: str) -> dict:
    resp = httpx.get(discovery_url, timeout=10.0)
    resp.raise_for_status()
    return resp.json()


def fetch_jwks(jwks_uri: str) -> dict:
    resp = httpx.get(jwks_uri, timeout=10.0)
    resp.raise_for_status()
    return resp.json()


def main() -> None:
    parser = argparse.ArgumentParser(description="Verify an OIDC identity provider's configuration against RegEngine AI's expectations.")
    parser.add_argument("--discovery-url", required=True, help="The IdP's .well-known/openid-configuration URL")
    parser.add_argument("--expected-issuer", default=None, help="What you plan to configure as sso_<provider>_issuer -- compared against the discovery doc's own 'issuer' field")
    parser.add_argument("--sample-token", default=None, help="Optional: a real ID/access token from this IdP, to inspect its claims (signature NOT verified by this script)")
    args = parser.parse_args()

    print(f"Fetching discovery document from {args.discovery_url} ...")
    try:
        discovery = fetch_discovery_document(args.discovery_url)
    except httpx.HTTPError as exc:
        print(f"FAILED to fetch discovery document: {exc}", file=sys.stderr)
        sys.exit(1)

    issuer = discovery.get("issuer")
    jwks_uri = discovery.get("jwks_uri")
    print(f"  issuer:  {issuer}")
    print(f"  jwks_uri: {jwks_uri}")
    print(f"  authorization_endpoint: {discovery.get('authorization_endpoint')}")
    print(f"  scopes_supported: {discovery.get('scopes_supported')}")

    if args.expected_issuer and args.expected_issuer != issuer:
        print(
            f"\nWARNING: --expected-issuer ({args.expected_issuer!r}) does NOT match the discovery "
            f"document's own issuer ({issuer!r}). Configure sso_<provider>_issuer as {issuer!r} exactly "
            "-- app.security.jwt matches the token's `iss` claim against this string byte-for-byte.",
            file=sys.stderr,
        )

    print(f"\nFetching JWKS from {jwks_uri} ...")
    try:
        jwks = fetch_jwks(jwks_uri)
        key_ids = [k.get("kid") for k in jwks.get("keys", [])]
        print(f"  {len(key_ids)} signing key(s) published: {key_ids}")
    except httpx.HTTPError as exc:
        print(f"FAILED to fetch JWKS: {exc}", file=sys.stderr)
        sys.exit(1)

    if args.sample_token:
        print("\nDecoding sample token (SIGNATURE NOT VERIFIED -- inspection only) ...")
        claims = pyjwt.decode(args.sample_token, options={"verify_signature": False})
        print(json.dumps(claims, indent=2, default=str))
        for candidate in ("groups", "roles", "wids", "group_names"):
            if candidate in claims:
                print(f"\nFound a candidate group claim: '{candidate}' = {claims[candidate]}")
        print(
            "\nConfirm which of the above (if any) is your configured group claim, and set "
            "sso_<provider>_group_claim accordingly."
        )

    print("\nDone. If issuer/jwks_uri above look correct, set:")
    print(f"  SSO_<PROVIDER>_ISSUER={issuer}")
    print(f"  SSO_<PROVIDER>_JWKS_URL={jwks_uri}")


if __name__ == "__main__":
    main()
