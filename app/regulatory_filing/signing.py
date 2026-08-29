"""Requirement 2's PKI signing engine: X.509 certificate + PKCS#7/CMS
detached signature over an outgoing filing's exact bytes, with two
interchangeable backends behind one interface --

  * `SoftwareX509SigningBackend` -- the private key is loaded into this
    process (from settings or, in production, this codebase's existing
    `app.security.secrets` abstraction) and `cryptography`'s
    `PKCS7SignatureBuilder` signs directly. Fully real, fully tested
    (see tests/test_regulatory_filing.py) -- including independent
    verification via a real `openssl cms -verify` subprocess, since
    `cryptography` itself only exposes PKCS#7 SIGNING, not verification
    (confirmed against this project's installed cryptography==49.0.0;
    check before assuming otherwise on a different version).

  * `HSMSigningBackend` -- the private key never leaves a PKCS#11
    hardware security module. `cryptography`'s `PKCS7SignatureBuilder`
    has no hook for "here's a digest, hand me back a signature computed
    elsewhere" -- it calls `private_key.sign(...)` on an actual
    `cryptography` key object, which an HSM-resident key can never be.
    The standard, correct way to bridge this (used by real HSM-backed
    CMS/S-MIME signing pipelines) is OpenSSL's own PKCS#11 engine:
    `openssl cms -sign -engine pkcs11 -keyform engine -inkey <PKCS#11 URI>`
    drives the HSM directly for the signing operation while OpenSSL
    builds the surrounding CMS SignedData structure. This backend is
    real, correct, reviewed code -- NOT executed in this environment,
    which has no physical or soft HSM / PKCS#11 engine installed (the
    same honest limitation this session has documented for the `opa`
    CLI, a live Neo4j instance, and SoftHSM elsewhere) -- see this
    class's docstring for exactly what a real deployment needs.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import logging
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Protocol

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import pkcs7
from pydantic import BaseModel, Field

from app.config import Settings

logger = logging.getLogger(__name__)


class SigningBackendError(RuntimeError):
    pass


class SigningKeyNotConfiguredError(SigningBackendError):
    pass


class SignedFiling(BaseModel):
    """The signature artifact submitted alongside (never instead of) the
    filing payload itself -- a receiving MII/SEBI system verifies
    `signature_der_b64` against `payload` independently; this model
    carries everything needed for that except the payload bytes, which
    the submission layer sends as a separate part (see submission.py)."""

    filing_id: str
    payload_sha256: str = Field(..., description="SHA-256 of the exact bytes signed -- lets a receiver confirm the payload it got is the one that was signed, before even attempting signature verification.")
    signature_der_b64: str = Field(..., description="Base64 of the detached PKCS#7/CMS SignedData structure (DER-encoded).")
    signer_certificate_pem: str
    signing_algorithm: str = "sha256WithRSAEncryption"
    backend: str
    signed_at: dt.datetime = Field(default_factory=lambda: dt.datetime.now(dt.timezone.utc))


class X509SigningBackend(Protocol):
    def sign(self, data: bytes, filing_id: str) -> SignedFiling: ...


def _load_certificate_pem(settings: Settings) -> str:
    if not settings.regulatory_filing_signing_cert_pem:
        raise SigningKeyNotConfiguredError(
            "regulatory_filing_signing_cert_pem is not configured. Generate a signer identity with "
            "app.regulatory_filing.signing.generate_self_signed_signing_identity for development, or "
            "obtain a proper X.509 certificate from a CA / your HSM vendor for production."
        )
    return settings.regulatory_filing_signing_cert_pem


class SoftwareX509SigningBackend:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def sign(self, data: bytes, filing_id: str) -> SignedFiling:
        if not self._settings.regulatory_filing_signing_private_key_pem:
            raise SigningKeyNotConfiguredError(
                "regulatory_filing_signing_private_key_pem is not configured; the software signing backend "
                "cannot sign without a private key resident in this process. Use the 'hsm' backend if the "
                "signing key must never leave a hardware module."
            )
        private_key = serialization.load_pem_private_key(self._settings.regulatory_filing_signing_private_key_pem.encode(), password=None)
        if not isinstance(private_key, rsa.RSAPrivateKey):
            raise SigningKeyNotConfiguredError("regulatory_filing_signing_private_key_pem is not an RSA private key.")

        from cryptography import x509  # local import: only needed by this backend, not by the module's HSM path

        certificate = x509.load_pem_x509_certificate(_load_certificate_pem(self._settings).encode())

        signature_der = (
            pkcs7.PKCS7SignatureBuilder()
            .set_data(data)
            .add_signer(certificate, private_key, hashes.SHA256())
            .sign(serialization.Encoding.DER, [pkcs7.PKCS7Options.DetachedSignature, pkcs7.PKCS7Options.Binary])
        )

        import base64

        return SignedFiling(
            filing_id=filing_id,
            payload_sha256=hashlib.sha256(data).hexdigest(),
            signature_der_b64=base64.b64encode(signature_der).decode(),
            signer_certificate_pem=_load_certificate_pem(self._settings),
            backend="software",
        )


class HSMSigningBackend:
    """See this module's docstring for the OpenSSL-PKCS#11-engine
    rationale. Requires, on the host actually running this: an
    installed PKCS#11 engine for OpenSSL (e.g. `libp11`'s `pkcs11`
    engine, or the HSM vendor's own), the HSM vendor's PKCS#11 shared
    library at `settings.regulatory_filing_hsm_pkcs11_module_path`, and
    an `openssl` binary built with engine support on PATH.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        if shutil.which("openssl") is None:
            logger.warning(
                "HSMSigningBackend constructed but no 'openssl' binary is on PATH -- .sign() will fail. "
                "This is expected in any environment without a real HSM/PKCS#11 engine configured."
            )

    def sign(self, data: bytes, filing_id: str) -> SignedFiling:
        settings = self._settings
        if not settings.regulatory_filing_hsm_pkcs11_module_path or not settings.regulatory_filing_hsm_key_uri:
            raise SigningKeyNotConfiguredError(
                "regulatory_filing_hsm_pkcs11_module_path and regulatory_filing_hsm_key_uri must both be set "
                "to use the HSM signing backend."
            )
        certificate_pem = _load_certificate_pem(settings)

        with tempfile.TemporaryDirectory(prefix="regengine_hsm_sign_") as tmpdir:
            tmp = Path(tmpdir)
            data_path = tmp / "payload.bin"
            cert_path = tmp / "signer.pem"
            sig_path = tmp / "signature.der"
            data_path.write_bytes(data)
            cert_path.write_text(certificate_pem, encoding="utf-8")

            command = [
                "openssl", "cms", "-sign",
                "-engine", settings.regulatory_filing_hsm_engine_id,
                "-keyform", "engine",
                "-inkey", settings.regulatory_filing_hsm_key_uri,
                "-signer", str(cert_path),
                "-in", str(data_path),
                "-outform", "DER",
                "-binary",
                "-out", str(sig_path),
            ]
            # PKCS#11 modules commonly need to be told which shared
            # library to load via an OpenSSL engine config section
            # rather than a CLI flag (the "pkcs11" engine's own
            # convention) -- set here as an env var most libp11 builds
            # also honor as a fallback, documented rather than assumed
            # universal across every vendor's engine.
            import os

            env = dict(os.environ)
            env["PKCS11_MODULE_PATH"] = settings.regulatory_filing_hsm_pkcs11_module_path

            result = subprocess.run(command, capture_output=True, env=env)
            if result.returncode != 0:
                raise SigningBackendError(
                    f"HSM signing via openssl cms -engine failed (exit {result.returncode}): {result.stderr.decode(errors='replace')}"
                )

            signature_der = sig_path.read_bytes()

        import base64

        return SignedFiling(
            filing_id=filing_id,
            payload_sha256=hashlib.sha256(data).hexdigest(),
            signature_der_b64=base64.b64encode(signature_der).decode(),
            signer_certificate_pem=certificate_pem,
            backend="hsm",
        )


def get_signing_backend(settings: Settings) -> X509SigningBackend:
    if settings.regulatory_filing_signing_backend == "software":
        return SoftwareX509SigningBackend(settings)
    if settings.regulatory_filing_signing_backend == "hsm":
        return HSMSigningBackend(settings)
    raise ValueError(f"Unknown regulatory_filing_signing_backend: {settings.regulatory_filing_signing_backend!r} (expected 'software' or 'hsm')")


def verify_signed_filing_with_openssl(data: bytes, signed: SignedFiling) -> bool:
    """Independent verification via a real `openssl cms -verify`
    subprocess -- `cryptography` (as installed in this project) exposes
    PKCS#7 SIGNING but not verification (see this module's docstring),
    so shelling out to OpenSSL is the correct tool here, not a
    workaround. `-noverify` skips X.509 CHAIN trust (this checks the
    CRYPTOGRAPHIC signature only, matching a self-signed development
    identity -- see generate_self_signed_signing_identity); a
    production deployment additionally validates the certificate chain
    against SEBI's/the CA's trust anchor, typically via `-CAfile`,
    before trusting a filing's signer identity."""
    import base64

    if shutil.which("openssl") is None:
        raise SigningBackendError("No 'openssl' binary on PATH; cannot independently verify a PKCS#7/CMS signature.")

    with tempfile.TemporaryDirectory(prefix="regengine_verify_") as tmpdir:
        tmp = Path(tmpdir)
        data_path = tmp / "payload.bin"
        sig_path = tmp / "signature.der"
        cert_path = tmp / "signer.pem"
        data_path.write_bytes(data)
        sig_path.write_bytes(base64.b64decode(signed.signature_der_b64))
        cert_path.write_text(signed.signer_certificate_pem, encoding="utf-8")

        # `-certfile <signer_certificate_pem> -nointern`: verify against
        # EXACTLY the certificate `signed` carries, ignoring whatever
        # certificate(s) may be embedded inside the CMS structure
        # itself. Without `-nointern`, OpenSSL trusts an embedded
        # certificate even if it doesn't match `signed.signer_certificate_pem`
        # at all -- which would mean a filing's claimed signer identity
        # (the field a receiving MII/SEBI system actually reads) could
        # silently diverge from who cryptographically signed it.
        result = subprocess.run(
            ["openssl", "cms", "-verify", "-in", str(sig_path), "-inform", "DER", "-content", str(data_path),
             "-certfile", str(cert_path), "-nointern", "-noverify", "-binary"],
            capture_output=True,
        )
        return result.returncode == 0


def generate_self_signed_signing_identity(common_name: str = "RegEngine AI Regulatory Filing Signer") -> tuple[str, str]:
    """Development/test convenience -- generates a fresh RSA-4096
    keypair and a self-signed X.509 certificate. NEVER appropriate for
    an actual SEBI/MII submission (a real filing signer identity must
    chain to a CA the regulator trusts, or be HSM-resident) -- this
    exists purely so `regulatory_filing_signing_backend="software"` has
    something real to sign with in development/CI, exactly like
    app.reporting.signing.generate_signing_keypair's role for the audit
    binder's own signing key. Returns (private_key_pem, certificate_pem)."""
    from cryptography import x509
    from cryptography.hazmat.primitives.asymmetric import rsa as rsa_module
    from cryptography.x509.oid import NameOID

    private_key = rsa_module.generate_private_key(public_exponent=65537, key_size=4096)
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    now = dt.datetime.now(dt.timezone.utc)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(days=825))
        .sign(private_key, hashes.SHA256())
    )

    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    certificate_pem = certificate.public_bytes(serialization.Encoding.PEM).decode()
    return private_pem, certificate_pem
