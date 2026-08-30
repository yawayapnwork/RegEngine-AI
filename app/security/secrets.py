"""Secrets management integration: where key material (JWT signing keys,
per-tenant broker credentials, HUGGINGFACEHUB_API_TOKEN, WEBHOOK_HMAC_SECRET,
database passwords) actually comes from in each environment.

`.env` / plain environment variables (app.config.Settings' default) are
fine for local development but are the wrong place for production secrets
in a financial-compliance system: no rotation, no access audit trail, no
encryption-at-rest guarantee beyond the host filesystem. This module is
the seam between "a setting has a value" and "where that value is allowed
to come from" -- swap `SECRETS_BACKEND` and nothing else in the codebase
changes.

boto3 / hvac are lazy-imported inside each provider (same convention as
this repo's other optional heavy dependencies -- unstructured, tika,
crewai) so a deployment using only one backend never needs the other
SDK installed.
"""
from __future__ import annotations

import json
import logging
import time
from functools import lru_cache
from typing import Protocol

from app.config import Settings, get_settings

logger = logging.getLogger(__name__)


class SecretNotFoundError(KeyError):
    """Raised when a named secret (or field within it) does not exist in
    the configured backend. Deliberately a subclass of KeyError, not a
    generic exception, so callers can `except SecretNotFoundError` without
    also swallowing unrelated bugs."""


class SecretsProvider(Protocol):
    """Every backend implements this. `field` addresses one key inside a
    structured (JSON) secret -- AWS Secrets Manager and Vault's KV v2
    engine both commonly store several related values (e.g. an HS256
    secret and an RSA private key) under one secret name/path rather than
    one secret per value."""

    def get_secret(self, name: str, field: str | None = None) -> str: ...


class EnvSecretsProvider:
    """Dev/local fallback: reads from the already-loaded Settings object
    (itself sourced from `.env` / process environment). `name` is treated
    as a Settings attribute name. This is the ONLY provider appropriate
    for a developer's machine; every other environment should use AWS or
    Vault."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def get_secret(self, name: str, field: str | None = None) -> str:
        value = getattr(self._settings, name, None)
        if value is None:
            raise SecretNotFoundError(f"No setting '{name}' (env-backend secrets are Settings attributes).")
        return str(value)


class AWSSecretsManagerProvider:
    """Reads from AWS Secrets Manager. `name` is the secret's ARN or
    friendly name; a plaintext string secret is returned as-is, a
    JSON-structured secret is parsed and `field` selects one key from it
    (e.g. name="regengine/prod/jwt", field="hs256_secret").

    Credentials/region come from the standard boto3 resolution chain (IAM
    role, instance profile, env vars, ~/.aws/config) -- never hardcoded
    here, consistent with the IAM-role-per-workload pattern this is meant
    to enable in EKS/ECS.
    """

    def __init__(self, region_name: str) -> None:
        self._region_name = region_name
        self._client = None  # lazily constructed on first use

    def _get_client(self):
        if self._client is None:
            import boto3  # deferred heavy import

            self._client = boto3.client("secretsmanager", region_name=self._region_name)
        return self._client

    def get_secret(self, name: str, field: str | None = None) -> str:
        from botocore.exceptions import ClientError  # deferred, ships with boto3

        try:
            response = self._get_client().get_secret_value(SecretId=name)
        except ClientError as exc:
            error_code = exc.response.get("Error", {}).get("Code")
            if error_code == "ResourceNotFoundException":
                raise SecretNotFoundError(f"AWS Secrets Manager: no secret '{name}'.") from exc
            raise

        raw = response.get("SecretString")
        if raw is None:
            raise SecretNotFoundError(f"AWS Secrets Manager: secret '{name}' has no SecretString (binary secrets unsupported).")

        if field is None:
            return raw
        try:
            structured = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise SecretNotFoundError(f"Secret '{name}' is not JSON-structured; cannot select field '{field}'.") from exc
        if field not in structured:
            raise SecretNotFoundError(f"Secret '{name}' has no field '{field}'.")
        return str(structured[field])


class HashiCorpVaultProvider:
    """Reads from Vault's KV v2 secrets engine. `name` is the secret's
    path under that mount (e.g. "regengine/jwt"); `field` selects one key
    from the version's data (KV v2 secrets are always a flat key-value map
    at a path, so `field` is effectively required in practice)."""

    def __init__(self, vault_addr: str, vault_token: str, mount_point: str = "secret") -> None:
        self._vault_addr = vault_addr
        self._vault_token = vault_token
        self._mount_point = mount_point
        self._client = None

    def _get_client(self):
        if self._client is None:
            import hvac  # deferred heavy import

            self._client = hvac.Client(url=self._vault_addr, token=self._vault_token)
            if not self._client.is_authenticated():
                raise RuntimeError("Vault client failed to authenticate with the configured token.")
        return self._client

    def get_secret(self, name: str, field: str | None = None) -> str:
        import hvac.exceptions  # deferred

        try:
            response = self._get_client().secrets.kv.v2.read_secret_version(
                path=name, mount_point=self._mount_point, raise_on_deleted_version=True
            )
        except hvac.exceptions.InvalidPath as exc:
            raise SecretNotFoundError(f"Vault: no secret at '{self._mount_point}/{name}'.") from exc

        data = response["data"]["data"]
        if field is None:
            if len(data) == 1:
                return str(next(iter(data.values())))
            raise SecretNotFoundError(f"Vault secret '{name}' has multiple fields; `field` is required.")
        if field not in data:
            raise SecretNotFoundError(f"Vault secret '{name}' has no field '{field}'.")
        return str(data[field])


class CachedSecretsProvider:
    """Wraps any SecretsProvider with a short in-process TTL cache. Secret
    material (JWT signing keys especially) gets read on every request
    otherwise -- pointless load on the backend and, for AWS/Vault, real
    added latency on a hot path. TTL is deliberately short (default 5 min),
    not "cache forever": a rotated secret should propagate without a
    process restart.
    """

    def __init__(self, inner: SecretsProvider, ttl_seconds: float = 300.0) -> None:
        self._inner = inner
        self._ttl = ttl_seconds
        self._cache: dict[tuple[str, str | None], tuple[str, float]] = {}

    def get_secret(self, name: str, field: str | None = None) -> str:
        key = (name, field)
        cached = self._cache.get(key)
        now = time.monotonic()
        if cached is not None and now - cached[1] < self._ttl:
            return cached[0]

        value = self._inner.get_secret(name, field)
        self._cache[key] = (value, now)
        return value


@lru_cache(maxsize=1)
def get_secrets_provider() -> SecretsProvider:
    """Factory: selects the backend from settings.secrets_backend
    ("env" | "aws" | "vault"). Cached process-wide -- constructing a new
    boto3/hvac client per call would defeat connection reuse."""
    settings = get_settings()
    backend = settings.secrets_backend

    if backend == "aws":
        provider: SecretsProvider = AWSSecretsManagerProvider(region_name=settings.aws_secrets_region)
    elif backend == "vault":
        provider = HashiCorpVaultProvider(
            vault_addr=settings.vault_addr,
            vault_token=settings.vault_token or "",
            mount_point=settings.vault_kv_mount,
        )
    elif backend == "env":
        provider = EnvSecretsProvider(settings)
    else:
        raise ValueError(f"Unknown secrets_backend: {backend!r} (expected 'env', 'aws', or 'vault')")

    logger.info("Secrets backend: %s", backend)
    return CachedSecretsProvider(provider, ttl_seconds=settings.secrets_cache_ttl_seconds)


def resolve_secret(name: str, *, field: str | None = None, settings: Settings | None = None) -> str:
    """Convenience wrapper most call sites should use instead of touching
    the provider directly -- e.g.
        hf_api_token = resolve_secret("hf_api_token")             # env backend
        hf_api_token = resolve_secret("regengine/prod/agents", field="hf_api_token")  # aws/vault

    `settings`, when given, is honored ONLY for the "env" backend --
    constructing a fresh, uncached EnvSecretsProvider bound to exactly that
    Settings instance, so a caller holding a specific (e.g. per-request or
    per-test) Settings object gets secret values consistent with it rather
    than silently falling back to the process-global singleton. AWS/Vault
    are deployment-wide client singletons (region/vault_addr do not vary
    per-request) and always go through the cached `get_secrets_provider()`
    regardless of `settings`.
    """
    if settings is not None and settings.secrets_backend == "env":
        return EnvSecretsProvider(settings).get_secret(name, field=field)
    return get_secrets_provider().get_secret(name, field=field)
