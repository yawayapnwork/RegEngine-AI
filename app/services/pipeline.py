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
from app.models import ClauseChunk, ParseResult
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


def _translate_one_chunk(chunk: ClauseChunk, settings: Settings) -> tuple[ClauseChunk, str | None]:
    """Sync helper (run via asyncio.to_thread -- translation backends are
    themselves synchronous model calls, same convention as
    app.localization.translation's own backends). Detects `chunk.text`'s
    language; English (or undetectable-short text, which
    detect_regional_language already refuses to guess at) passes through
    unchanged. A detected regional language is translated and the chunk
    is rebuilt via build_translated_clause_chunk, preserving every other
    field (clause_number, section_path, page_start/end, circular_number,
    etc.) and the ORIGINAL sha256 -- translation doesn't change what
    source content this chunk is provenance-linked to, only what text an
    English-only downstream (app.agents.crew) reads.

    Returns (possibly-rebuilt chunk, warning message or None) rather than
    raising on a translation-verification failure: a failed-verification
    translation is still usable (flagged for HITL, per
    RegionalDocumentResult.requires_human_review), not a parse error that
    should abort the whole document.
    """
    from app.localization.pipeline import (
        UnsupportedLanguageError,
        build_translated_clause_chunk,
        detect_regional_language,
        process_regional_text,
    )

    detected = detect_regional_language(chunk.text)
    if detected is None or detected.value == "en":
        return chunk, None

    try:
        result = process_regional_text(chunk.text, settings, source_language=detected)
    except UnsupportedLanguageError:
        return chunk, None
    except Exception as exc:  # noqa: BLE001 - a translation backend failure must not abort the whole document; fall through untranslated with a warning
        logger.warning("Translation failed for chunk %s (detected=%s): %r", chunk.chunk_id, detected.value, exc)
        return chunk, f"Chunk {chunk.chunk_id}: translation failed ({detected.value} -> en); kept original text, needs manual review."

    clause_kwargs = chunk.model_dump(exclude={"chunk_id", "sha256", "text", "extra"})
    translated = build_translated_clause_chunk(
        chunk_id=chunk.chunk_id, sha256=chunk.sha256, result=result, extra=chunk.extra, **clause_kwargs
    )

    warning = None
    if result.requires_human_review():
        warning = (
            f"Chunk {chunk.chunk_id}: {detected.value} -> en translation failed verification "
            "or has unresolved entity alignments; flagged for HITL review before extraction."
        )
        logger.warning(warning)
    return translated, warning


async def _localize_chunks(chunks: list[ClauseChunk], settings: Settings) -> tuple[list[ClauseChunk], list[str]]:
    """Runs every chunk through language detection + translation
    (app.localization.pipeline) when settings.localization_enabled --
    the seam this codebase's regional-language pipeline was built for but
    was, until now, never actually called from the main ingestion path
    (app.parsing.extractor produces English-assumed ClauseChunks
    unconditionally). Off by default (same flag app.graph's Neo4j sync
    and app.api.saml_routes' SSO login use to gate an optional,
    reviewed-but-not-always-deployed subsystem), so a deployment that has
    not provisioned a translation backend (settings.localization_translation_backend)
    is entirely unaffected."""
    if not settings.localization_enabled:
        return chunks, []

    warnings: list[str] = []
    localized: list[ClauseChunk] = []
    for chunk in chunks:
        translated, warning = await asyncio.to_thread(_translate_one_chunk, chunk, settings)
        localized.append(translated)
        if warning:
            warnings.append(warning)
    return localized, warnings


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
            chunks, localization_warnings = await _localize_chunks(chunks, settings)
            warnings.extend(localization_warnings)

            return ParseResult(
                metadata=metadata,
                chunks=chunks,
                element_count=len(elements),
                warnings=warnings,
            )
