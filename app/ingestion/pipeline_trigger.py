"""Bridges a discovered/amended SEBI document into the existing Phase 1
ingestion pipeline (`app.services.pipeline.parse_pdf_bytes` +
`app.vectorstore.qdrant_store.index_chunks`) — the same path
`POST /v1/circulars/parse-and-index` uses for a manual upload.
"""
from __future__ import annotations

import logging
from pathlib import Path

from app.config import Settings, get_settings
from app.ingestion.exceptions import DocumentDownloadError
from app.ingestion.http_client import SebiHttpClient
from app.ingestion.models import ChangeKind, DiscoveredDocument, IngestedDocument
from app.parsing.exceptions import ParsingError
from app.services.pipeline import parse_pdf_bytes
from app.vectorstore.qdrant_store import index_chunks

logger = logging.getLogger(__name__)


async def download_document(client: SebiHttpClient, discovered: DiscoveredDocument) -> bytes:
    try:
        response = await client.get(discovered.source_url)
    except Exception as exc:  # noqa: BLE001 - normalize every download failure to one type
        raise DocumentDownloadError(f"Failed to download {discovered.source_url}: {exc!r}") from exc

    content_type = response.headers.get("content-type", "")
    if "pdf" not in content_type.lower() and not discovered.source_url.lower().endswith(".pdf"):
        raise DocumentDownloadError(
            f"{discovered.source_url} did not resolve to a PDF (content-type={content_type!r})"
        )
    return response.content


def _archive_path(discovered: DiscoveredDocument, settings: Settings) -> Path:
    directory = Path(settings.ingestion_pdf_download_dir)
    directory.mkdir(parents=True, exist_ok=True)
    filename = discovered.source_url.rsplit("/", 1)[-1] or f"{discovered.circular_number or 'circular'}.pdf"
    return directory / filename


async def process_discovered_document(
    discovered: DiscoveredDocument,
    change_kind: ChangeKind,
    content: bytes,
    content_sha256: str,
    settings: Settings | None = None,
) -> IngestedDocument:
    """Runs one discovered document through parse -> index. Raises on
    failure; the caller (Celery task) is responsible for retry/backoff and
    surfacing the failure without crashing the rest of the poll cycle.
    """
    settings = settings or get_settings()

    # Best-effort local archive of the raw PDF: useful for audit/replay and
    # for the Rego/JSON-Logic compiler's provenance trail, but never fatal.
    try:
        _archive_path(discovered, settings).write_bytes(content)
    except OSError as exc:
        logger.warning("Could not archive %s locally: %s", discovered.source_url, exc)

    try:
        parsed = await parse_pdf_bytes(content, filename=discovered.source_url.rsplit("/", 1)[-1], settings=settings)
        await index_chunks(parsed.chunks, settings, recreate_collection=False)
    except ParsingError:
        raise
    except Exception as exc:  # noqa: BLE001 - convert unexpected failures to a typed one for the caller
        raise ParsingError(f"Unexpected failure ingesting {discovered.source_url}: {exc!r}") from exc

    logger.info(
        "Ingested %s (%s): %s -> %d clause chunks indexed",
        discovered.circular_number or discovered.title,
        change_kind.value,
        discovered.source_url,
        len(parsed.chunks),
    )

    return IngestedDocument(
        discovered=discovered,
        content_sha256=content_sha256,
        content_length=len(content),
        change_kind=change_kind,
    )
