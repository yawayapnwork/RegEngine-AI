"""Unit tests for app.parsing.extractor's OCR fallback control flow
(extract_pdf -> _ocr_fallback) and app.services.pipeline's regional-
language chunk translation wiring (_localize_chunks). Both the primary
extraction backends and the OCR/translation backends are monkeypatched
so these run fast and without Unstructured/Tika/Tesseract/a translation
model installed -- mirroring tests/test_parsing.py's own "avoid the
heavy backends" convention, one level up the call stack.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.config import Settings
from app.models import ClauseChunk
from app.parsing.exceptions import ScannedDocumentError


@pytest.fixture
def settings() -> Settings:
    return Settings(_env_file=None)


def _pdf_bytes() -> bytes:
    return b"%PDF-1.4\n%fake pdf for extraction-backend-mocking tests\n"


@pytest.mark.asyncio
async def test_scanned_pdf_recovers_text_via_ocr_fallback(monkeypatch, settings, tmp_path) -> None:
    """Primary backend returns zero usable text (simulating a scanned
    PDF); OCR fallback recovers real text -- extract_pdf must succeed
    using the OCR-recovered elements, not raise ScannedDocumentError."""
    import app.parsing.extractor as extractor

    monkeypatch.setattr(
        extractor,
        "_partition_with_unstructured",
        lambda path, strategy: [{"text": "", "category": "UncategorizedText", "page_number": 1, "coordinates": None, "text_as_html": None}],
    )
    monkeypatch.setattr(
        extractor,
        "_partition_with_tika",
        lambda path, url: (_ for _ in ()).throw(RuntimeError("no tika server in this test")),
    )

    async def fake_ocr_fallback(source_path, filename, settings_):
        return [
            {
                "text": "Every stockbroker shall collect an upfront margin of not less than 20%.",
                "category": "UncategorizedText",
                "page_number": 1,
                "coordinates": None,
                "text_as_html": None,
            }
        ]

    monkeypatch.setattr(extractor, "_ocr_fallback", fake_ocr_fallback)

    src = tmp_path / "scanned.pdf"
    src.write_bytes(_pdf_bytes())

    metadata, elements = await extractor.extract_pdf(
        file_bytes=_pdf_bytes(), source_path=src, filename="scanned.pdf", settings=settings
    )
    assert len(elements) == 1
    assert "upfront margin" in elements[0].text


@pytest.mark.asyncio
async def test_scanned_pdf_raises_when_ocr_also_fails(monkeypatch, settings, tmp_path) -> None:
    """Primary backend AND OCR fallback both yield nothing -- must raise
    ScannedDocumentError (permanent, routes straight to DLQ -- see
    app.ingestion.tasks._PERMANENT_PARSING_ERRORS), not silently succeed
    with zero elements."""
    import app.parsing.extractor as extractor

    monkeypatch.setattr(
        extractor,
        "_partition_with_unstructured",
        lambda path, strategy: [],
    )
    monkeypatch.setattr(
        extractor,
        "_partition_with_tika",
        lambda path, url: (_ for _ in ()).throw(RuntimeError("no tika server in this test")),
    )

    async def empty_ocr_fallback(source_path, filename, settings_):
        return []

    monkeypatch.setattr(extractor, "_ocr_fallback", empty_ocr_fallback)

    src = tmp_path / "blank.pdf"
    src.write_bytes(_pdf_bytes())

    with pytest.raises(ScannedDocumentError):
        await extractor.extract_pdf(file_bytes=_pdf_bytes(), source_path=src, filename="blank.pdf", settings=settings)


@pytest.mark.asyncio
async def test_ocr_fallback_skips_a_page_that_fails_without_aborting_the_document(monkeypatch, settings) -> None:
    """One page's OCR call raising must not abort the rest of the
    document -- app.parsing.extractor._ocr_fallback's page loop must
    `continue`, not propagate."""
    import app.parsing.extractor as extractor

    class FakeImage:
        def __init__(self, n):
            self.n = n

        def save(self, path):
            Path(path).write_bytes(b"fake-png")

    monkeypatch.setattr(extractor, "_rasterize_pdf", lambda path, dpi: [FakeImage(1), FakeImage(2)])

    def fake_ocr_page(image_path, settings_):
        # First page's OCR blows up; second page succeeds.
        if not hasattr(fake_ocr_page, "calls"):
            fake_ocr_page.calls = 0
        fake_ocr_page.calls += 1
        if fake_ocr_page.calls == 1:
            raise RuntimeError("tesseract exploded on page 1")
        return "Recovered text from page 2."

    monkeypatch.setattr(extractor, "_ocr_page", fake_ocr_page)

    elements = await extractor._ocr_fallback(Path("unused.pdf"), "doc.pdf", settings)
    assert len(elements) == 1
    assert elements[0]["page_number"] == 2
    assert "page 2" in elements[0]["text"]


def test_localize_chunks_passthrough_when_disabled() -> None:
    """settings.localization_enabled=False (the default) must be a true
    no-op: no language detection, no translation-backend import
    attempted, chunks returned as-is."""
    import asyncio

    from app.services.pipeline import _localize_chunks

    chunk = ClauseChunk(chunk_id="c1", sha256="a" * 64, text="Some English clause text.")
    settings = Settings(_env_file=None, localization_enabled=False)

    chunks, warnings = asyncio.run(_localize_chunks([chunk], settings))
    assert chunks == [chunk]
    assert warnings == []


def test_localize_chunks_english_text_passes_through_unchanged_when_enabled() -> None:
    """settings.localization_enabled=True but the chunk's text is already
    English -- detect_regional_language must short-circuit before any
    translation backend is touched, and the chunk must be returned
    byte-identical (same object, not a rebuilt copy)."""
    import asyncio

    from app.services.pipeline import _localize_chunks

    chunk = ClauseChunk(
        chunk_id="c1",
        sha256="a" * 64,
        text="Every stockbroker shall collect an upfront margin of not less than twenty percent of the transaction value before executing any trade.",
    )
    settings = Settings(_env_file=None, localization_enabled=True)

    chunks, warnings = asyncio.run(_localize_chunks([chunk], settings))
    assert chunks == [chunk]
    assert warnings == []


def test_translate_one_chunk_falls_back_gracefully_on_backend_failure(monkeypatch) -> None:
    """A translation-backend failure (missing model, network error, etc.)
    must degrade to the original chunk plus a warning -- never raise and
    abort the whole document's ingestion over one chunk's translation
    failure."""
    from app.services.pipeline import _translate_one_chunk
    from app.localization.languages import RegionalLanguage

    chunk = ClauseChunk(chunk_id="c1", sha256="a" * 64, text="किसी भी दलाल को यह करना होगा।")
    settings = Settings(_env_file=None, localization_enabled=True)

    import app.services.pipeline as pipeline_mod

    def fake_detect(text):
        return RegionalLanguage.HINDI

    monkeypatch.setattr("app.localization.pipeline.detect_regional_language", fake_detect)

    def fake_process_regional_text(*args, **kwargs):
        raise RuntimeError("translation backend unavailable in this test")

    monkeypatch.setattr("app.localization.pipeline.process_regional_text", fake_process_regional_text)

    result_chunk, warning = _translate_one_chunk(chunk, settings)
    assert result_chunk.text == chunk.text  # untranslated fallback
    assert warning is not None and "translation failed" in warning
