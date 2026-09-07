"""Unit tests for app.parsing.extractor's OCR fallback control flow
(extract_pdf -> _ocr_fallback) and app.services.pipeline's regional-
language chunk translation wiring (_localize_chunks). Both the primary
extraction backends and the OCR/translation backends are monkeypatched
so these run fast and without Unstructured/Tika/Tesseract/a translation
model installed -- mirroring tests/test_parsing.py's own "avoid the
heavy backends" convention, one level up the call stack.
"""
from __future__ import annotations

import time
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


class _FakePageImage:
    """Stand-in for a pdf2image page: `save` writes its own page number
    into the file so a downstream fake `_ocr_page` (which only receives
    the tmp file path, not this object) can identify which page it's
    processing -- necessary now that pages OCR concurrently, so a test
    can't rely on call order to know which page is which."""

    def __init__(self, n: int) -> None:
        self.n = n

    def save(self, path: str) -> None:
        Path(path).write_bytes(str(self.n).encode())


@pytest.mark.asyncio
async def test_ocr_fallback_skips_a_page_that_fails_without_aborting_the_document(monkeypatch, settings) -> None:
    """One page's OCR call raising must not abort the rest of the
    document -- app.parsing.extractor._ocr_one_page must isolate a single
    page's failure (returning None) rather than propagating and aborting
    the concurrent asyncio.gather over every page."""
    import app.parsing.extractor as extractor

    monkeypatch.setattr(extractor, "_rasterize_pdf", lambda path, dpi: [_FakePageImage(1), _FakePageImage(2)])

    def fake_ocr_page(image_path, settings_):
        page_num = int(Path(image_path).read_bytes().decode())
        if page_num == 1:
            raise RuntimeError("tesseract exploded on page 1")
        return f"Recovered text from page {page_num}."

    monkeypatch.setattr(extractor, "_ocr_page", fake_ocr_page)

    elements = await extractor._ocr_fallback(Path("unused.pdf"), "doc.pdf", settings)
    assert len(elements) == 1
    assert elements[0]["page_number"] == 2
    assert "page 2" in elements[0]["text"]


@pytest.mark.asyncio
async def test_ocr_fallback_runs_pages_concurrently(monkeypatch, settings) -> None:
    """Pages must be OCR'd concurrently (bounded by
    settings.ocr_page_concurrency), not strictly one at a time -- N pages
    each taking `delay` should together take close to one `delay`, not
    N * delay."""
    import app.parsing.extractor as extractor

    page_count = 5
    delay = 0.2
    monkeypatch.setattr(
        extractor, "_rasterize_pdf", lambda path, dpi: [_FakePageImage(i) for i in range(1, page_count + 1)]
    )

    def fake_ocr_page(image_path, settings_):
        time.sleep(delay)
        page_num = int(Path(image_path).read_bytes().decode())
        return f"Recovered text from page {page_num}."

    monkeypatch.setattr(extractor, "_ocr_page", fake_ocr_page)

    assert settings.ocr_page_concurrency >= page_count  # default (6) covers this test's 5 pages

    start = time.monotonic()
    elements = await extractor._ocr_fallback(Path("unused.pdf"), "doc.pdf", settings)
    elapsed = time.monotonic() - start

    assert len(elements) == page_count
    # Serial execution would take >= page_count * delay (1.0s here); running
    # concurrently should land close to a single page's delay plus overhead.
    assert elapsed < page_count * delay * 0.6


@pytest.mark.asyncio
async def test_ocr_fallback_preserves_page_order_regardless_of_completion_order(monkeypatch, settings) -> None:
    """Output must come back in ascending page_number order even when
    pages finish OCR out of order -- asyncio.gather preserves the order
    tasks were passed in, not completion order."""
    import app.parsing.extractor as extractor

    page_count = 4
    monkeypatch.setattr(
        extractor, "_rasterize_pdf", lambda path, dpi: [_FakePageImage(i) for i in range(1, page_count + 1)]
    )

    def fake_ocr_page(image_path, settings_):
        page_num = int(Path(image_path).read_bytes().decode())
        # Earlier pages sleep longer, so they finish LAST -- if the result
        # order tracked completion order instead of input order, this would
        # come back reversed.
        time.sleep(0.05 * (page_count + 1 - page_num))
        return f"Recovered text from page {page_num}."

    monkeypatch.setattr(extractor, "_ocr_page", fake_ocr_page)

    elements = await extractor._ocr_fallback(Path("unused.pdf"), "doc.pdf", settings)
    assert [e["page_number"] for e in elements] == list(range(1, page_count + 1))


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
