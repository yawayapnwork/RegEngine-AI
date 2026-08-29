"""Orchestrates OCR -> language detection -> translation -> cross-
lingual verification -> entity alignment into one call, and packages
the result into the SAME `app.models.ClauseChunk` the rest of this
codebase's ingestion/extraction pipeline already consumes -- regional-
language support is therefore an ADDITION at the front of ingestion,
never a fork of app.agents/app.compiler/app.execution, which continue
to see plain English `ClauseChunk.text` exactly as before.

A translation that fails verification (app.localization.verification)
is NOT silently dropped or silently trusted -- it is still translated
and chunked, but `ClauseChunk.extra["verification"]["passed"]` is
False and `requires_human_review()` returns True, so a caller (the
ingestion task that will eventually route this chunk onward) can send
it to HITL review instead of the extraction agent, matching this
codebase's established "flag, don't silently block or silently accept"
philosophy (app.compiler.hitl's module docstring).
"""
from __future__ import annotations

import logging

from pydantic import BaseModel

from app.config import Settings
from app.localization.entity_alignment import EntityAlignmentResult, align_regional_entity
from app.localization.languages import RegionalLanguage, regional_language_from_langdetect_code
from app.localization.translation import get_translation_backend
from app.localization.verification import CrossLingualVerifier, TranslationVerificationResult
from app.models import ClauseChunk

logger = logging.getLogger(__name__)


class UnsupportedLanguageError(ValueError):
    """Raised when `detect_regional_language` cannot confidently place
    the text in English or one of the three supported regional
    languages -- routed to human review / DLQ by the caller, never
    force-mapped to the nearest supported language (a wrong guess here
    would silently mistranslate)."""


class RegionalDocumentResult(BaseModel):
    source_language: RegionalLanguage
    original_text: str
    translated_text: str
    translation_backend: str
    verification: TranslationVerificationResult
    entity_alignments: list[EntityAlignmentResult]

    def requires_human_review(self) -> bool:
        return not self.verification.passed or any(not e.resolved for e in self.entity_alignments)


def detect_regional_language(text: str) -> RegionalLanguage | None:
    """`langdetect` is itself a statistical/probabilistic detector, not
    infallible on short strings -- this returns None (rather than a
    best-effort guess) whenever `langdetect` either fails outright or
    returns a language this pipeline doesn't handle, since a caller
    silently treating an unrecognized language as e.g. Hindi would
    mistranslate with high confidence-looking output."""
    from langdetect import LangDetectException, detect  # deferred: only needed on this path

    try:
        code = detect(text)
    except LangDetectException:
        logger.warning("langdetect could not determine a language for the given text (too short/ambiguous?).")
        return None
    return regional_language_from_langdetect_code(code)


def process_regional_text(
    original_text: str,
    settings: Settings,
    *,
    source_language: RegionalLanguage | None = None,
    entity_phrases: list[str] | None = None,
) -> RegionalDocumentResult:
    """Text-level entrypoint: `original_text` is already-extracted text
    (either a text-native regional PDF's own extracted layer, or
    app.localization.ocr's output joined into one string). Auto-detects
    `source_language` via `detect_regional_language` when not supplied.
    """
    if source_language is None:
        detected = detect_regional_language(original_text)
        if detected is None:
            raise UnsupportedLanguageError("Could not determine a supported source language for the given text.")
        source_language = detected

    if source_language == RegionalLanguage.ENGLISH:
        # Nothing to translate; verification is trivially a perfect
        # match against itself and numeric precision is preserved by
        # construction (identical text on both sides of the check).
        verification = CrossLingualVerifier(settings).verify(original_text, original_text, source_language)
        return RegionalDocumentResult(
            source_language=source_language,
            original_text=original_text,
            translated_text=original_text,
            translation_backend="none",
            verification=verification,
            entity_alignments=[align_regional_entity(p, source_language) for p in (entity_phrases or [])],
        )

    backend = get_translation_backend(settings)
    translated_text = backend.translate_text(original_text, source_language)

    verifier = CrossLingualVerifier(settings)
    verification = verifier.verify(original_text, translated_text, source_language)
    if not verification.passed:
        logger.warning("Translation verification FAILED for a %s clause: %s", source_language.value, "; ".join(verification.reasons))

    entity_alignments = [
        align_regional_entity(phrase, source_language, translate_fn=lambda t: backend.translate_text(t, source_language))
        for phrase in (entity_phrases or [])
    ]

    return RegionalDocumentResult(
        source_language=source_language,
        original_text=original_text,
        translated_text=translated_text,
        translation_backend=settings.localization_translation_backend,
        verification=verification,
        entity_alignments=entity_alignments,
    )


def build_translated_clause_chunk(
    *,
    chunk_id: str,
    sha256: str,
    result: RegionalDocumentResult,
    **clause_kwargs,
) -> ClauseChunk:
    """Wraps a `RegionalDocumentResult` into the SAME `ClauseChunk`
    model app.parsing.chunker produces for English-native PDFs --
    `text` is the verified English translation (what
    app.agents.crew.build_extraction_task actually reads), and every
    localization detail (original script, translation backend, full
    verification result) is preserved in `extra` for audit and for a
    HITL reviewer deciding whether to trust a failed-verification
    chunk's translation."""
    extra = dict(clause_kwargs.pop("extra", None) or {})
    extra["localization"] = {
        "source_language": result.source_language.value,
        "original_text": result.original_text,
        "translation_backend": result.translation_backend,
        "verification": result.verification.model_dump(mode="json"),
        "entity_alignments": [e.model_dump(mode="json") for e in result.entity_alignments],
    }
    return ClauseChunk(chunk_id=chunk_id, sha256=sha256, text=result.translated_text, extra=extra, **clause_kwargs)
