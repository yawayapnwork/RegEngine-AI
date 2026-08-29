"""End-to-end orchestration: bytes-in -> ParseResult-out.

Wraps a per-request temp file and bounds overall concurrency with a
semaphore so a burst of large-PDF uploads cannot exhaust worker threads or
memory on a single instance.
"""
from __future__ import annotations

import asyncio
import logging
import tempfile
from pathlib import Path

from app.config import Settings, get_settings
from app.models import ParseResult
from app.observability.metrics import observe_ingestion_latency
from app.observability.tracing import traced_span
from app.parsing.chunker import chunk_elements
from app.parsing.exceptions import ParsingError
from app.parsing.extractor import extract_pdf

logger = logging.getLogger(__name__)

_concurrency_gate: asyncio.Semaphore | None = None


def _gate(settings: Settings) -> asyncio.Semaphore:
    global _concurrency_gate
    if _concurrency_gate is None:
        _concurrency_gate = asyncio.Semaphore(settings.parse_concurrency)
    return _concurrency_gate


async def parse_pdf_bytes(
    file_bytes: bytes,
    filename: str | None,
    settings: Settings | None = None,
    source_tag: str | None = None,
) -> ParseResult:
    """`source_tag` identifies which regulator's feed this document came
    from (e.g. "rbi", "irdai") when known at call time -- the ingestion
    pipeline (app.ingestion.regulator_sources) always supplies it; an
    ad-hoc manual upload through the API leaves it None and the extractor
    falls back to detecting the regulator from the document's own header
    text (app.regulatory.taxonomy.detect_regulator_and_document)."""
    settings = settings or get_settings()
    warnings: list[str] = []

    async with _gate(settings):
        with observe_ingestion_latency(), traced_span(
            "ingestion.parse_pdf_bytes", filename=filename, size_bytes=len(file_bytes), backend=settings.extraction_backend
        ), tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir) / (filename or "upload.pdf")
            try:
                await asyncio.to_thread(tmp_path.write_bytes, file_bytes)
                metadata, elements = await extract_pdf(
                    file_bytes=file_bytes,
                    source_path=tmp_path,
                    filename=filename,
                    source_tag=source_tag,
                    settings=settings,
                )
            except ParsingError:
                raise
            except Exception as exc:  # noqa: BLE001 - convert unexpected errors to typed ones
                raise ParsingError(f"Unexpected failure while extracting '{filename}': {exc!r}") from exc

            if metadata.circular_number is None:
                warnings.append("Could not auto-detect circular_number; consider supplying it explicitly.")
            if metadata.issue_date is None:
                warnings.append("Could not auto-detect issue_date; consider supplying it explicitly.")

            chunks = chunk_elements(elements, metadata, settings)

            return ParseResult(
                metadata=metadata,
                chunks=chunks,
                element_count=len(elements),
                warnings=warnings,
            )
