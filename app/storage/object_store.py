"""Storage abstraction for RegEngine AI.

Supports:
- LocalFilesystemStorage (default for POC and local dev, STORAGE_BACKEND=local)
- S3Storage (AWS S3, MinIO, Backblaze B2, Cloudflare R2, STORAGE_BACKEND=s3)

Guarantees:
- Safe unique key generation: never blindly trusts client filenames.
- Path traversal prevention: strictly validates all file keys stay within base directory.
- Content validation: enforces valid file types (e.g. PDF magic header %PDF-) and size limits.
- Seamless fallback: local demo runs without requiring a running MinIO or S3 container.
"""
from __future__ import annotations

import asyncio
import functools
import logging
import os
import uuid
from abc import ABC, abstractmethod
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from app.config import Settings, get_settings

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from mypy_boto3_s3.client import S3Client


# ---------------------------------------------------------------------------
# Storage Exceptions
# ---------------------------------------------------------------------------


class StorageError(Exception):
    """Base exception for storage errors."""
    pass


class ObjectStorageNotConfiguredError(StorageError, RuntimeError):
    """Raised when S3/MinIO object storage is selected without required credentials."""
    pass


class InvalidFileTypeError(StorageError):
    """Raised when an uploaded file fails content or magic-byte validation."""
    pass


class FileTooLargeError(StorageError):
    """Raised when an uploaded file exceeds the configured size limit."""
    pass


class PathTraversalError(StorageError):
    """Raised when a key or path attempts directory traversal."""
    pass


class StorageFileNotFoundError(StorageError, FileNotFoundError):
    """Raised when a requested file or object is not found."""
    pass


# ---------------------------------------------------------------------------
# Storage Abstraction Interface
# ---------------------------------------------------------------------------


class ObjectStorage(ABC):
    """Abstract interface for object and file storage."""

    @abstractmethod
    async def upload_bytes(self, key: str, data: bytes, *, content_type: str = "application/pdf") -> str:
        """Uploads raw bytes to the specified key. Returns the stored key."""
        ...

    @abstractmethod
    async def download_bytes(self, key: str) -> bytes:
        """Downloads raw bytes from the specified key."""
        ...

    @abstractmethod
    async def delete_bytes(self, key: str) -> bool:
        """Deletes object at key. Returns True if deleted or False if not found."""
        ...

    @abstractmethod
    async def exists(self, key: str) -> bool:
        """Checks if key exists in storage."""
        ...


# ---------------------------------------------------------------------------
# Safe Key Helper
# ---------------------------------------------------------------------------


def generate_safe_key(original_filename: str | None = None, prefix: str = "uploads") -> str:
    """Generates a collision-resistant, path-traversal-safe storage key.
    Never trusts client-supplied path separators or naming.
    """
    safe_id = uuid.uuid4().hex
    safe_ext = ".pdf"
    if original_filename:
        ext = Path(original_filename).suffix.lower()
        if ext in (".pdf", ".txt", ".json"):
            safe_ext = ext
    return f"{prefix}/{safe_id}{safe_ext}"


# ---------------------------------------------------------------------------
# Local Filesystem Storage Implementation
# ---------------------------------------------------------------------------


class LocalFilesystemStorage(ObjectStorage):
    """Secure, local filesystem storage implementation.

    Enforces strict path-traversal confinement within base_dir.
    """

    def __init__(self, base_dir: str | Path, max_size_bytes: int = 50 * 1024 * 1024) -> None:
        self.base_dir = Path(base_dir).resolve()
        self.max_size_bytes = max_size_bytes
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def _resolve_safe_path(self, key: str) -> Path:
        """Confines key resolution strictly within self.base_dir to prevent path traversal."""
        clean = key.replace("\\", "/").strip()
        if clean.startswith("/") or (len(clean) > 1 and clean[1] == ":"):
            raise PathTraversalError(f"Absolute path not permitted in storage key: {key}")

        parts = [p for p in clean.split("/") if p and p != "."]
        if any(p.strip(".") == "" for p in parts):
            raise PathTraversalError(f"Path traversal sequence detected in key: {key}")

        target = (self.base_dir / Path(*parts)).resolve()
        try:
            target.relative_to(self.base_dir)
        except ValueError as exc:
            raise PathTraversalError(f"Path escapes storage root directory: {key}") from exc

        return target

    def _validate_content(self, data: bytes, content_type: str, safe_path: Path) -> None:
        if len(data) > self.max_size_bytes:
            raise FileTooLargeError(
                f"File size ({len(data)} bytes) exceeds max limit of {self.max_size_bytes} bytes."
            )
        if safe_path.suffix.lower() == ".pdf" or content_type == "application/pdf":
            if not data.startswith(b"%PDF-"):
                raise InvalidFileTypeError("Uploaded file content does not start with valid PDF magic bytes %PDF-.")

    def _write_sync(self, target: Path, data: bytes) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        # Write to temporary file in same directory and atomic replace
        temp_path = target.with_suffix(f".tmp.{uuid.uuid4().hex}")
        with open(temp_path, "wb") as f:
            f.write(data)
        os.replace(temp_path, target)

    def _read_sync(self, target: Path) -> bytes:
        if not target.is_file():
            raise StorageFileNotFoundError(f"File not found at storage path: {target}")
        with open(target, "rb") as f:
            return f.read()

    async def upload_bytes(self, key: str, data: bytes, *, content_type: str = "application/pdf") -> str:
        safe_path = self._resolve_safe_path(key)
        self._validate_content(data, content_type, safe_path)
        await asyncio.to_thread(self._write_sync, safe_path, data)
        return key

    async def download_bytes(self, key: str) -> bytes:
        safe_path = self._resolve_safe_path(key)
        return await asyncio.to_thread(self._read_sync, safe_path)

    async def delete_bytes(self, key: str) -> bool:
        safe_path = self._resolve_safe_path(key)

        def _delete() -> bool:
            if safe_path.is_file():
                safe_path.unlink()
                return True
            return False

        return await asyncio.to_thread(_delete)

    async def exists(self, key: str) -> bool:
        safe_path = self._resolve_safe_path(key)
        return await asyncio.to_thread(safe_path.is_file)


# ---------------------------------------------------------------------------
# S3 / MinIO Storage Implementation
# ---------------------------------------------------------------------------


class S3Storage(ObjectStorage):
    """S3-compatible (MinIO, Backblaze B2, AWS S3) implementation."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        if not (
            settings.object_storage_endpoint_url
            and settings.object_storage_bucket
            and settings.object_storage_access_key_id
            and settings.object_storage_secret_access_key
        ):
            raise ObjectStorageNotConfiguredError(
                "OBJECT_STORAGE_ENDPOINT_URL, OBJECT_STORAGE_BUCKET, "
                "OBJECT_STORAGE_ACCESS_KEY_ID and OBJECT_STORAGE_SECRET_ACCESS_KEY "
                "must all be set to use S3 storage."
            )

    def _client(self) -> "S3Client":
        import boto3
        from botocore.config import Config

        s3_config = Config(request_checksum_calculation="when_required", response_checksum_validation="when_required")
        endpoint_url = self.settings.object_storage_endpoint_url
        host = urlparse(endpoint_url).hostname or ""
        labels = host.split(".")
        region = labels[1] if len(labels) >= 3 and labels[0] == "s3" else "auto"

        return boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            aws_access_key_id=self.settings.object_storage_access_key_id,
            aws_secret_access_key=self.settings.object_storage_secret_access_key,
            region_name=region,
            config=s3_config,
        )

    async def upload_bytes(self, key: str, data: bytes, *, content_type: str = "application/pdf") -> str:
        client = self._client()
        await asyncio.to_thread(
            client.put_object,
            Bucket=self.settings.object_storage_bucket,
            Key=key,
            Body=data,
            ContentType=content_type,
        )
        return key

    async def download_bytes(self, key: str) -> bytes:
        client = self._client()
        try:
            response = await asyncio.to_thread(
                client.get_object,
                Bucket=self.settings.object_storage_bucket,
                Key=key,
            )
            return await asyncio.to_thread(response["Body"].read)
        except Exception as exc:
            if "NoSuchKey" in str(exc) or "404" in str(exc):
                raise StorageFileNotFoundError(f"Object '{key}' not found in S3 bucket.") from exc
            raise StorageError(f"S3 download error for key '{key}': {exc}") from exc

    async def delete_bytes(self, key: str) -> bool:
        client = self._client()
        try:
            await asyncio.to_thread(
                client.delete_object,
                Bucket=self.settings.object_storage_bucket,
                Key=key,
            )
            return True
        except Exception:
            return False

    async def exists(self, key: str) -> bool:
        client = self._client()
        try:
            await asyncio.to_thread(
                client.head_object,
                Bucket=self.settings.object_storage_bucket,
                Key=key,
            )
            return True
        except Exception:
            return False


# ---------------------------------------------------------------------------
# Storage Factory & Top-level API
# ---------------------------------------------------------------------------


@functools.lru_cache(maxsize=4)
def _cached_local_storage(dir_path: str, max_size_bytes: int) -> LocalFilesystemStorage:
    return LocalFilesystemStorage(dir_path, max_size_bytes=max_size_bytes)


def get_storage(settings: Settings | None = None) -> ObjectStorage:
    """Factory creating the configured storage backend."""
    settings = settings or get_settings()
    backend = (settings.storage_backend or "local").strip().lower()

    if backend == "local":
        return _cached_local_storage(
            settings.storage_local_dir,
            settings.max_upload_mb * 1024 * 1024,
        )
    if backend in ("s3", "minio"):
        return S3Storage(settings)

    raise StorageError(f"Unsupported STORAGE_BACKEND '{backend}'. Supported backends: 'local', 's3'.")


async def upload_bytes(key: str, data: bytes, *, content_type: str = "application/pdf") -> str:
    """Uploads data using the configured storage backend."""
    storage = get_storage()
    return await storage.upload_bytes(key, data, content_type=content_type)


async def download_bytes(key: str) -> bytes:
    """Downloads data using the configured storage backend."""
    storage = get_storage()
    return await storage.download_bytes(key)


async def delete_bytes(key: str) -> bool:
    """Deletes data using the configured storage backend."""
    storage = get_storage()
    return await storage.delete_bytes(key)


async def exists(key: str) -> bool:
    """Checks if key exists using the configured storage backend."""
    storage = get_storage()
    return await storage.exists(key)
