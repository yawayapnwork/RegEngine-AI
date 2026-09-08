"""Unit tests for the pure-Python parsing logic (hierarchy, chunking, hashing).

These deliberately avoid the heavy Unstructured/Tika/embedding backends so
they run fast and without external services, by constructing DocumentElement
lists directly rather than parsing an actual PDF.
"""
from __future__ import annotations

import datetime as dt

import pytest

from app.config import Settings
from app.models import CircularMetadata, DocumentElement, ElementKind
from app.parsing.chunker import chunk_elements
from app.parsing.hashing import sha256_of_clause, sha256_of_text
from app.parsing.hierarchy import HierarchyTracker, detect_clause_number, is_section_header


def _pdf_bytes() -> bytes:
    return b"%PDF-1.4\n%fake pdf for extraction-strategy-escalation tests\n"


def test_detect_clause_number_variants() -> None:
    assert detect_clause_number("1. Applicability").clause_number == "1"
    assert detect_clause_number("2.1 Scope of this circular").clause_number == "2.1"
    assert detect_clause_number("2.1.b Additional disclosure required").clause_number == "2.1.b"
    assert detect_clause_number("2.1.(b) Additional disclosure required").clause_number == "2.1.b"
    assert detect_clause_number("(iii) any other matter").clause_number == "iii"
    assert detect_clause_number("Not numbered text") is None


def test_hierarchy_tracker_builds_section_path() -> None:
    tracker = HierarchyTracker()
    assert tracker.update("1", 1) == ["1"]
    assert tracker.update("1.1", 2) == ["1", "1.1"]
    assert tracker.update("1.1.a", 3) == ["1", "1.1", "1.1.a"]
    # sibling at depth 2 should pop the depth-3 clause
    assert tracker.update("1.2", 2) == ["1", "1.2"]
    # new top-level section resets everything below it
    assert tracker.update("2", 1) == ["2"]


def test_sha256_is_deterministic_and_scoped_to_circular() -> None:
    h1 = sha256_of_clause(circular_number="SEBI/HO/1", clause_number="2.1.b", text="Entities shall report.")
    h2 = sha256_of_clause(circular_number="SEBI/HO/1", clause_number="2.1.b", text="Entities shall report.")
    h3 = sha256_of_clause(circular_number="SEBI/HO/2", clause_number="2.1.b", text="Entities shall report.")
    assert h1 == h2
    assert h1 != h3
    assert len(h1) == 64


def test_sha256_of_text_normalizes_whitespace() -> None:
    assert sha256_of_text("a   b\n c") == sha256_of_text("a b c")


def _el(kind: ElementKind, text: str, clause_number: str | None, section_path: list[str], page: int = 1) -> DocumentElement:
    return DocumentElement(
        element_id="x",
        kind=kind,
        text=text,
        clause_number=clause_number,
        section_path=section_path,
        page_number=page,
    )


def test_chunking_keeps_clause_intact_and_attaches_footnote() -> None:
    elements = [
        _el(ElementKind.SECTION_HEADER, "Reporting Obligations", "2", ["2"]),
        _el(ElementKind.CLAUSE, "2.1 All intermediaries shall submit reports within 15 days of quarter end.", "2.1", ["2", "2.1"]),
        _el(ElementKind.CLAUSE, "2.1.b The report shall be filed in the format prescribed in Annexure A.", "2.1.b", ["2", "2.1", "2.1.b"]),
        _el(ElementKind.FOOTNOTE, "1. As amended by circular dated 1 Jan 2024.", None, ["2", "2.1", "2.1.b"]),
    ]
    metadata = CircularMetadata(circular_number="SEBI/HO/MRD/2024/1", issue_date=dt.date(2024, 1, 1))
    settings = Settings(chunk_min_chars=5)

    chunks = chunk_elements(elements, metadata, settings)

    assert len(chunks) >= 1
    clause_b_chunk = next(c for c in chunks if c.clause_number == "2.1.b")
    assert "Annexure A" in clause_b_chunk.text
    assert clause_b_chunk.footnotes and "amended" in clause_b_chunk.footnotes[0]
    assert clause_b_chunk.circular_number == "SEBI/HO/MRD/2024/1"
    assert len(clause_b_chunk.sha256) == 64


@pytest.mark.asyncio
async def test_extract_pdf_default_strategy_is_fast_and_skips_hi_res(monkeypatch, tmp_path) -> None:
    """A normal text-layer PDF must be extracted with the default "fast"
    Unstructured strategy (app.config.Settings.unstructured_strategy) and
    never need the "hi_res" escalation -- hi_res is only for a fast-path
    result that trips the "no extractable text" check."""
    import app.parsing.extractor as extractor

    settings = Settings(_env_file=None)
    assert settings.unstructured_strategy == "fast"

    calls: list[str] = []

    def fake_partition(path, strategy):
        calls.append(strategy)
        return [
            {
                "text": "Every stockbroker shall collect an upfront margin of not less than 20%.",
                "category": "UncategorizedText",
                "page_number": 1,
                "coordinates": None,
                "text_as_html": None,
            }
        ]

    monkeypatch.setattr(extractor, "_partition_with_unstructured", fake_partition)

    src = tmp_path / "text_layer.pdf"
    src.write_bytes(_pdf_bytes())

    metadata, elements = await extractor.extract_pdf(
        file_bytes=_pdf_bytes(), source_path=src, filename="text_layer.pdf", settings=settings
    )

    assert calls == ["fast"]
    assert len(elements) == 1


@pytest.mark.asyncio
async def test_extract_pdf_escalates_fast_to_hi_res_before_ocr(monkeypatch, tmp_path) -> None:
    """"fast" trips the "no element has non-whitespace text" check ->
    extract_pdf must retry with "hi_res" and use its result, without ever
    falling through to the (last-resort) OCR fallback."""
    import app.parsing.extractor as extractor

    settings = Settings(_env_file=None)

    def fake_partition(path, strategy):
        if strategy == "fast":
            return [{"text": "", "category": "UncategorizedText", "page_number": 1, "coordinates": None, "text_as_html": None}]
        assert strategy == "hi_res"
        return [
            {
                "text": "Every stockbroker shall collect an upfront margin of not less than 20%.",
                "category": "UncategorizedText",
                "page_number": 1,
                "coordinates": None,
                "text_as_html": None,
            }
        ]

    monkeypatch.setattr(extractor, "_partition_with_unstructured", fake_partition)

    async def ocr_should_not_run(source_path, filename, settings_):
        raise AssertionError("OCR fallback must not run once hi_res recovers text")

    monkeypatch.setattr(extractor, "_ocr_fallback", ocr_should_not_run)

    src = tmp_path / "unusual_layout.pdf"
    src.write_bytes(_pdf_bytes())

    metadata, elements = await extractor.extract_pdf(
        file_bytes=_pdf_bytes(), source_path=src, filename="unusual_layout.pdf", settings=settings
    )

    assert len(elements) == 1
    assert "upfront margin" in elements[0].text


def test_is_section_header_heuristic() -> None:
    match = detect_clause_number("1. Applicability")
    assert is_section_header("1. Applicability", match) is True
    long_match = detect_clause_number("1. This is a much longer clause body that reads like actual obligation text.")
    assert is_section_header(
        "1. This is a much longer clause body that reads like actual obligation text.", long_match
    ) is False


def _generate_real_sebi_pdf() -> bytes:
    """Generate a minimal, real, well-formed text-layer PDF using reportlab."""
    from io import BytesIO
    from reportlab.pdfgen import canvas

    buf = BytesIO()
    c = canvas.Canvas(buf)
    c.drawString(100, 750, "Circular No. SEBI/HO/MIRSD/2024/001")
    c.drawString(100, 730, "15 January 2024")
    c.drawString(100, 700, "1. Collection of Upfront Margin")
    c.drawString(100, 680, "1.1 Stock brokers shall collect upfront margin from clients.")
    c.drawString(100, 660, "2. Daily Collateral Reporting")
    c.drawString(100, 640, "2.1 Daily client collateral reporting must occur by EOD.")
    c.save()
    return buf.getvalue()


@pytest.mark.asyncio
async def test_pypdf_fallback_extracts_real_text_layer_pdf(monkeypatch, tmp_path) -> None:
    """When unstructured and tika backends fail/are unavailable, extract_pdf
    must cleanly fall back to native pypdf without requiring OCR, Tika, or Poppler."""
    import app.parsing.extractor as extractor
    from app.parsing.exceptions import ScannedDocumentError

    settings = Settings(_env_file=None, extraction_backend="unstructured")

    # Simulate unstructured and tika failing
    def fail_unstructured(path, strategy):
        raise ModuleNotFoundError("No module named 'unstructured'")

    def fail_tika(path, url):
        raise ConnectionRefusedError("Tika server unreachable")

    monkeypatch.setattr(extractor, "_partition_with_unstructured", fail_unstructured)
    monkeypatch.setattr(extractor, "_partition_with_tika", fail_tika)

    # OCR must NOT be called for a normal text-layer PDF
    async def fail_ocr(source_path, filename, settings_):
        raise AssertionError("OCR fallback should never be called when pypdf extracts text successfully")

    monkeypatch.setattr(extractor, "_ocr_fallback", fail_ocr)

    pdf_bytes = _generate_real_sebi_pdf()
    pdf_file = tmp_path / "sebi_circular.pdf"
    pdf_file.write_bytes(pdf_bytes)

    metadata, elements = await extractor.extract_pdf(
        file_bytes=pdf_bytes,
        source_path=pdf_file,
        filename="sebi_circular.pdf",
        settings=settings,
    )

    assert metadata.circular_number == "SEBI/HO/MIRSD/2024/001"
    assert metadata.issue_date == dt.date(2024, 1, 15)
    assert len(elements) >= 4

    clause_1_1 = next((el for el in elements if el.clause_number == "1.1"), None)
    assert clause_1_1 is not None
    assert "upfront margin" in clause_1_1.text.lower()
    assert clause_1_1.page_number == 1


@pytest.mark.asyncio
async def test_pypdf_primary_backend(tmp_path) -> None:
    """When settings.extraction_backend is set to 'pypdf', it extracts directly."""
    import app.parsing.extractor as extractor

    settings = Settings(_env_file=None, extraction_backend="pypdf")
    pdf_bytes = _generate_real_sebi_pdf()
    pdf_file = tmp_path / "sebi_pypdf.pdf"
    pdf_file.write_bytes(pdf_bytes)

    metadata, elements = await extractor.extract_pdf(
        file_bytes=pdf_bytes,
        source_path=pdf_file,
        filename="sebi_pypdf.pdf",
        settings=settings,
    )

    assert metadata.circular_number == "SEBI/HO/MIRSD/2024/001"
    assert any(el.clause_number == "2.1" for el in elements)


@pytest.mark.asyncio
async def test_all_backends_failing_raises_extraction_backend_error(monkeypatch, tmp_path) -> None:
    """If all three text-layer backends error out, ExtractionBackendError is raised."""
    import app.parsing.extractor as extractor
    from app.parsing.exceptions import ExtractionBackendError

    settings = Settings(_env_file=None)

    def fail_backend(*args, **kwargs):
        raise RuntimeError("Backend failed")

    monkeypatch.setattr(extractor, "_partition_with_unstructured", fail_backend)
    monkeypatch.setattr(extractor, "_partition_with_tika", fail_backend)
    monkeypatch.setattr(extractor, "_partition_with_pypdf", fail_backend)

    pdf_bytes = _generate_real_sebi_pdf()
    pdf_file = tmp_path / "fail.pdf"
    pdf_file.write_bytes(pdf_bytes)

    with pytest.raises(ExtractionBackendError) as exc_info:
        await extractor.extract_pdf(
            file_bytes=pdf_bytes,
            source_path=pdf_file,
            filename="fail.pdf",
            settings=settings,
        )
    assert "All extraction backends failed" in str(exc_info.value)

