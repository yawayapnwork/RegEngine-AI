"""S3-compatible object storage (Backblaze B2 / Cloudflare R2 / AWS S3) for
manually-uploaded PDFs awaiting async processing -- see
app.db.models.IngestionUploadJob and app.ingestion.tasks.process_manual_upload_task.

boto3's client is synchronous; every call here wraps it via
`asyncio.to_thread` so callers in the (async) FastAPI request path and the
(sync, per Celery task) worker path can both use it without either
blocking an event loop or needing a second client implementation.
"""
from __future__ import annotations

import asyncio
import functools
from typing import TYPE_CHECKING
from urllib.parse import urlparse

import boto3
from botocore.config import Config

from app.config import get_settings

# botocore >=1.36 defaults to AWS's newer flexible-checksum request/response
# behavior, which non-AWS S3-compatible providers (Backblaze B2, MinIO, ...)
# don't fully implement -- the connection gets dropped mid-response instead
# of a clean error. Forcing "when_required" restores the pre-1.36 behavior
# that every S3-compatible provider actually supports.
_S3_CONFIG = Config(request_checksum_calculation="when_required", response_checksum_validation="when_required")

if TYPE_CHECKING:
    from mypy_boto3_s3.client import S3Client


class ObjectStorageNotConfiguredError(RuntimeError):
    """Raised when an object-storage call is made without
    OBJECT_STORAGE_ENDPOINT_URL/BUCKET/ACCESS_KEY_ID/SECRET_ACCESS_KEY set."""


def _region_from_endpoint(endpoint_url: str) -> str:
    """SigV4 signing requires the *actual* region or Backblaze B2 rejects
    the signature outright ("AccessDenied: Signature validation failed") --
    boto3 defaults to us-east-1 if not told otherwise. B2 endpoints are
    `s3.<region>.backblazeb2.com` (e.g. `s3.eu-central-003.backblazeb2.com`);
    Cloudflare R2 endpoints carry no region in the hostname at all and use
    the literal region "auto" instead, so that's the fallback here."""
    host = urlparse(endpoint_url).hostname or ""
    labels = host.split(".")
    if len(labels) >= 3 and labels[0] == "s3":
        return labels[1]
    return "auto"


@functools.lru_cache(maxsize=1)
def _client() -> "S3Client":
    settings = get_settings()
    if not (
        settings.object_storage_endpoint_url
        and settings.object_storage_bucket
        and settings.object_storage_access_key_id
        and settings.object_storage_secret_access_key
    ):
        raise ObjectStorageNotConfiguredError(
            "OBJECT_STORAGE_ENDPOINT_URL, OBJECT_STORAGE_BUCKET, "
            "OBJECT_STORAGE_ACCESS_KEY_ID and OBJECT_STORAGE_SECRET_ACCESS_KEY "
            "must all be set to use object storage."
        )
    return boto3.client(
        "s3",
        endpoint_url=settings.object_storage_endpoint_url,
        aws_access_key_id=settings.object_storage_access_key_id,
        aws_secret_access_key=settings.object_storage_secret_access_key,
        region_name=_region_from_endpoint(settings.object_storage_endpoint_url),
        config=_S3_CONFIG,
    )


async def upload_bytes(key: str, data: bytes, *, content_type: str) -> str:
    """Uploads `data` under `key` in the configured bucket. Returns `key`
    unchanged (the caller already generated it) so this can be used
    fluently: `job.object_key = await upload_bytes(key, data, ...)`."""
    client = _client()
    await asyncio.to_thread(
        client.put_object,
        Bucket=get_settings().object_storage_bucket,
        Key=key,
        Body=data,
        ContentType=content_type,
    )
    return key


async def download_bytes(key: str) -> bytes:
    client = _client()
    response = await asyncio.to_thread(
        client.get_object, Bucket=get_settings().object_storage_bucket, Key=key
    )
    return await asyncio.to_thread(response["Body"].read)
