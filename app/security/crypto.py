"""AES-256-GCM payload encryption -- application-layer, defense-in-depth
ON TOP OF transport TLS, not a replacement for it.

Why this exists alongside TLS: TLS protects the wire between the client
and wherever it is terminated (an ingress/load balancer -- see
helm/regengine-ai's nginx-ingress annotations), which is not necessarily
the same trust boundary as "this application's code". A payload encrypted
with a tenant-specific key stays confidential across any intermediate
proxy, a misconfigured internal hop, or a request/response body that ends
up in a log or APM trace capture -- the kind of control financial/PCI-style
compliance reviews specifically ask for on top of "TLS everywhere".

Format (all binary, base64-encoded for the header/body): `nonce(12 bytes)
|| ciphertext || tag(16 bytes, appended by AESGCM)`. AAD binds the
ciphertext to the tenant_id so a ciphertext encrypted for one tenant can
never be replayed/decrypted under a different tenant's context even if an
attacker swapped which tenant_id header accompanied it.
"""
from __future__ import annotations

import base64
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_NONCE_LEN = 12
_KEY_LEN = 32  # AES-256


class PayloadDecryptionError(Exception):
    """Raised on a malformed ciphertext, wrong key, or (via AEAD's tag
    check) any tampering -- these are deliberately not distinguished in
    the exception, only in the log message, for the same reason
    `app.security.jwt.TokenError` doesn't distinguish failure modes to
    callers: no free diagnostic signal for an attacker probing the endpoint."""


def generate_tenant_key() -> str:
    """Generates a new base64-encoded 256-bit key -- run once per tenant
    when provisioning it, store the result in the secrets backend (AWS
    Secrets Manager / Vault) at `regengine/tenants/<tenant_id>/payload_key`,
    never in application config."""
    return base64.b64encode(os.urandom(_KEY_LEN)).decode("ascii")


def encrypt_payload(plaintext: bytes, key_b64: str, *, tenant_id: str) -> str:
    key = base64.b64decode(key_b64)
    if len(key) != _KEY_LEN:
        raise ValueError(f"Payload encryption key must decode to {_KEY_LEN} bytes, got {len(key)}.")

    nonce = os.urandom(_NONCE_LEN)
    ciphertext = AESGCM(key).encrypt(nonce, plaintext, associated_data=tenant_id.encode("utf-8"))
    return base64.b64encode(nonce + ciphertext).decode("ascii")


def decrypt_payload(encoded: str, key_b64: str, *, tenant_id: str) -> bytes:
    try:
        raw = base64.b64decode(encoded, validate=True)
        key = base64.b64decode(key_b64)
    except Exception as exc:  # noqa: BLE001 - any decode failure is a malformed payload, not a 500
        raise PayloadDecryptionError(f"Malformed base64 payload/key: {exc!r}") from exc

    if len(key) != _KEY_LEN:
        raise PayloadDecryptionError(f"Payload encryption key must decode to {_KEY_LEN} bytes, got {len(key)}.")
    if len(raw) < _NONCE_LEN:
        raise PayloadDecryptionError("Ciphertext shorter than the nonce; cannot be valid.")

    nonce, ciphertext = raw[:_NONCE_LEN], raw[_NONCE_LEN:]
    try:
        return AESGCM(key).decrypt(nonce, ciphertext, associated_data=tenant_id.encode("utf-8"))
    except InvalidTag as exc:
        # Wrong key, wrong tenant_id (AAD mismatch), or the ciphertext was
        # tampered with -- AEAD's tag check does not distinguish which.
        raise PayloadDecryptionError("Payload authentication failed (wrong key or tampered ciphertext).") from exc
