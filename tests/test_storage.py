"""Tests for the storage abstraction layer in RegEngine AI.

Covers:
* upload and retrieval via LocalFilesystemStorage and get_storage()
* invalid extension / file type validation (PDF magic bytes %PDF-)
* oversized file rejection
* path traversal prevention (prevent escaping base directory)
* duplicate filename collision handling (safe unique keys)
* missing/unsupported storage backend handling
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from app.config import Settings
from app.storage.object_store import (
    FileTooLargeError,
    InvalidFileTypeError,
    LocalFilesystemStorage,
    ObjectStorageNotConfiguredError,
    PathTraversalError,
    StorageError,
    StorageFileNotFoundError,
    generate_safe_key,
    get_storage,
)


@pytest.fixture
def temp_storage_dir(tmp_path: Path) -> Path:
    storage_dir = tmp_path / "storage"
    storage_dir.mkdir(parents=True, exist_ok=True)
    return storage_dir


@pytest.fixture
def local_storage(temp_storage_dir: Path) -> LocalFilesystemStorage:
    # 1MB max upload limit for testing
    return LocalFilesystemStorage(base_dir=temp_storage_dir, max_size_bytes=1024 * 1024)


# ---------------------------------------------------------------------------
# 1. Upload & Retrieval
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upload_and_retrieval(local_storage: LocalFilesystemStorage) -> None:
    test_pdf_content = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\ntrailer\n<<>>\n%%EOF"
    key = "uploads/job123/circular.pdf"

    stored_key = await local_storage.upload_bytes(key, test_pdf_content, content_type="application/pdf")
    assert stored_key == key
    assert await local_storage.exists(key) is True

    retrieved = await local_storage.download_bytes(key)
    assert retrieved == test_pdf_content


@pytest.mark.asyncio
async def test_download_nonexistent_file_raises_error(local_storage: LocalFilesystemStorage) -> None:
    with pytest.raises(StorageFileNotFoundError):
        await local_storage.download_bytes("uploads/nonexistent/sample.pdf")


@pytest.mark.asyncio
async def test_delete_existing_and_nonexistent(local_storage: LocalFilesystemStorage) -> None:
    test_pdf_content = b"%PDF-1.4\nsample"
    key = "uploads/del/sample.pdf"

    await local_storage.upload_bytes(key, test_pdf_content)
    assert await local_storage.exists(key) is True

    deleted = await local_storage.delete_bytes(key)
    assert deleted is True
    assert await local_storage.exists(key) is False

    # Deleting again returns False without crashing
    assert await local_storage.delete_bytes(key) is False


# ---------------------------------------------------------------------------
# 2. Invalid Extension / Magic Byte Validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_invalid_pdf_content_rejected(local_storage: LocalFilesystemStorage) -> None:
    fake_exe_content = b"MZ\x90\x00\x03\x00\x00\x00"  # Windows PE executable
    key = "uploads/malicious/fake.pdf"

    with pytest.raises(InvalidFileTypeError) as exc:
        await local_storage.upload_bytes(key, fake_exe_content, content_type="application/pdf")
    assert "%PDF-" in str(exc.value)


# ---------------------------------------------------------------------------
# 3. Oversized File Handling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_oversized_file_rejected(local_storage: LocalFilesystemStorage) -> None:
    # Storage configured with 1MB limit; provide 1.5MB
    large_pdf = b"%PDF-1.4" + (b"\x00" * (1024 * 1024 + 500))
    key = "uploads/large/big.pdf"

    with pytest.raises(FileTooLargeError):
        await local_storage.upload_bytes(key, large_pdf, content_type="application/pdf")


# ---------------------------------------------------------------------------
# 4. Path Traversal Prevention
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "traversal_key",
    [
        "../outside.pdf",
        "uploads/../../etc/passwd",
        "..\\..\\windows\\system32\\calc.exe",
        "uploads/....//nested/test.pdf",
        "/etc/shadow",
    ],
)
async def test_path_traversal_prevention(local_storage: LocalFilesystemStorage, traversal_key: str) -> None:
    data = b"%PDF-1.4\nsample"
    with pytest.raises(PathTraversalError):
        await local_storage.upload_bytes(traversal_key, data)

    with pytest.raises(PathTraversalError):
        await local_storage.download_bytes(traversal_key)

    with pytest.raises(PathTraversalError):
        await local_storage.exists(traversal_key)


# ---------------------------------------------------------------------------
# 5. Duplicate Filename / Collision-Resistant Safe Keys
# ---------------------------------------------------------------------------


def test_safe_key_generation_prevents_collision_and_traversal() -> None:
    client_name_1 = "../../malicious/name.pdf"
    client_name_2 = "../../malicious/name.pdf"

    key1 = generate_safe_key(client_name_1)
    key2 = generate_safe_key(client_name_2)

    assert key1 != key2  # Unique UUID per call prevents collision
    assert ".." not in key1
    assert key1.endswith(".pdf")
    assert key1.startswith("uploads/")


# ---------------------------------------------------------------------------
# 6. Missing / Unsupported Storage Backend Configuration
# ---------------------------------------------------------------------------


def test_missing_s3_credentials_raises_error() -> None:
    settings = Settings(
        storage_backend="s3",
        object_storage_endpoint_url=None,
        object_storage_bucket=None,
        object_storage_access_key_id=None,
        object_storage_secret_access_key=None,
    )
    with pytest.raises(ObjectStorageNotConfiguredError):
        get_storage(settings)


def test_unsupported_storage_backend_raises_error() -> None:
    settings = Settings(storage_backend="azure_blob")
    with pytest.raises(StorageError) as exc:
        get_storage(settings)
    assert "Unsupported STORAGE_BACKEND" in str(exc.value)


def test_default_backend_is_local_and_functional(tmp_path: Path) -> None:
    settings = Settings(storage_backend="local", storage_local_dir=str(tmp_path))
    storage = get_storage(settings)
    assert isinstance(storage, LocalFilesystemStorage)
