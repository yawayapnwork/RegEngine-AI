"""Digital signature for the audit binder (Requirement 2: "Summary PDF
with executive metrics and digital signatures").

RSA-PSS/SHA-256 over the package MANIFEST (a JSON document listing every
file in the ZIP plus its own SHA-256 -- see app.reporting.audit_binder),
not over the ZIP bytes directly: signing the manifest means the signature
verifies the CONTENT INTEGRITY of every individual file via its listed
hash, which is what an auditor actually wants to check ("has anything in
this package been altered since RegEngine issued it") -- and it composes
naturally with the ledger's own SHA-256 hash-chain proof already inside
the package, rather than introducing a second, incompatible integrity
mechanism.

Uses `cryptography` directly (already a transitive dependency via
`pyjwt[crypto]`) rather than a higher-level signing library -- RSA-PSS
signing/verification is a handful of well-documented calls, and this is
exactly the kind of narrow, auditable use of primitives that doesn't
benefit from an abstraction layer on top.
"""
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import logging

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from pydantic import BaseModel, Field

from app.config import Settings

logger = logging.getLogger(__name__)


class SigningKeyNotConfiguredError(RuntimeError):
    pass


class DigitalSignature(BaseModel):
    algorithm: str = "RSA-PSS-SHA256"
    signer_id: str
    signed_at: dt.datetime
    manifest_sha256: str
    signature_b64: str
    public_key_pem: str | None = Field(
        None, description="Embedded so the ZIP is self-contained for verification without a separate key-distribution step."
    )


def _load_private_key(settings: Settings) -> rsa.RSAPrivateKey:
    if not settings.audit_binder_signing_private_key_pem:
        raise SigningKeyNotConfiguredError(
            "audit_binder_signing_private_key_pem is not configured; the audit binder cannot be digitally signed. "
            "Generate one with: openssl genrsa -out audit_signing_key.pem 4096"
        )
    key = serialization.load_pem_private_key(settings.audit_binder_signing_private_key_pem.encode(), password=None)
    if not isinstance(key, rsa.RSAPrivateKey):
        raise SigningKeyNotConfiguredError("audit_binder_signing_private_key_pem is not an RSA private key.")
    return key


def sign_manifest(manifest_json: bytes, settings: Settings) -> DigitalSignature:
    """`manifest_json` is the exact bytes that will be written into the
    ZIP as `manifest.json` (built by app.reporting.audit_binder) MINUS
    this signature's own eventual embedding -- the manifest is signed
    BEFORE the signature is attached, obviously, since a signature cannot
    cover itself."""
    private_key = _load_private_key(settings)
    manifest_sha256 = hashlib.sha256(manifest_json).hexdigest()

    signature_bytes = private_key.sign(
        manifest_json,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.MAX_LENGTH),
        hashes.SHA256(),
    )

    public_key_pem = None
    if settings.audit_binder_signing_public_key_pem:
        public_key_pem = settings.audit_binder_signing_public_key_pem
    else:
        public_key_pem = private_key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM, format=serialization.PublicFormat.SubjectPublicKeyInfo
        ).decode()

    return DigitalSignature(
        signer_id=settings.audit_binder_signer_id,
        signed_at=dt.datetime.now(dt.timezone.utc),
        manifest_sha256=manifest_sha256,
        signature_b64=base64.b64encode(signature_bytes).decode(),
        public_key_pem=public_key_pem,
    )


def verify_signature(manifest_json: bytes, signature: DigitalSignature) -> bool:
    """Independent verification path an auditor (or this project's own
    CI) can run with ONLY `signature.public_key_pem` -- no access to this
    service, its database, or any private key required. Returns False
    (never raises) for any verification failure, matching
    app.ledger.verifier's "a break is data, not an exception" convention."""
    if signature.public_key_pem is None:
        logger.error("Cannot verify: signature has no embedded public key.")
        return False
    try:
        public_key = serialization.load_pem_public_key(signature.public_key_pem.encode())
        if not isinstance(public_key, rsa.RSAPublicKey):
            return False
        public_key.verify(
            base64.b64decode(signature.signature_b64),
            manifest_json,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.MAX_LENGTH),
            hashes.SHA256(),
        )
        return True
    except (InvalidSignature, ValueError, TypeError):
        return False


def generate_signing_keypair() -> tuple[str, str]:
    """Convenience for `regengine-report keygen` -- generates a fresh
    4096-bit RSA key pair and returns (private_key_pem, public_key_pem).
    Never used at report-generation time itself; this is an operator
    setup utility."""
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=4096)
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    public_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM, format=serialization.PublicFormat.SubjectPublicKeyInfo
    ).decode()
    return private_pem, public_pem
