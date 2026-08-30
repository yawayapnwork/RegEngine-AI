"""Layout-aware PDF extraction using Unstructured (primary) with an Apache
Tika fallback, and an OCR fallback (app.localization.ocr) below that for
scanned/image-only pages neither text-layer backend can read.

Both `unstructured.partition.pdf.partition_pdf` and `tika.parser.from_file`
are synchronous, CPU/IO-heavy calls (the former shells out to detectron2/
poppler for layout+table detection, the latter talks to a Tika server over
HTTP). We run them in a worker thread via `asyncio.to_thread` so the FastAPI
event loop is never blocked, and enforce a hard timeout so a pathological
PDF cannot pin a worker indefinitely.
"""
from __future__ import annotations

import asyncio
import logging
import re
import tempfile
import uuid
from pathlib import Path

from app.config import Settings
from app.models import BoundingBox, CircularMetadata, DocumentElement, ElementKind
from app.parsing.exceptions import (
    ExtractionBackendError,
    ParseTimeoutError,
    ScannedDocumentError,
    UnsupportedFileError,
)
from app.parsing.hierarchy import HierarchyTracker, detect_clause_number, is_footnote, is_section_header
from app.regulatory.taxonomy import detect_regulator_and_document

logger = logging.getLogger(__name__)

_PDF_MAGIC = b"%PDF-"

# Regulator-agnostic fallback: matches a generic "Circular No. ..." phrasing
# when none of app.regulatory.taxonomy's regulator-specific document-number
# patterns hit. Kept narrow (this exact phrasing) rather than widened,
# since a looser generic pattern would start shadowing the more precise
# regulator-specific patterns it's meant to be a fallback for.
_GENERIC_DOC_NUMBER_RE = re.compile(r"Circular\s+No\.?\s*[:\-]?\s*[\w/\-]+", re.IGNORECASE)
_ISSUE_DATE_RE = re.compile(
    r"(\d{1,2}\s+(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{4})",
    re.IGNORECASE,
)


def _validate_pdf_bytes(data: bytes) -> None:
    if not data:
        raise UnsupportedFileError("Uploaded file is empty.")
    if not data.lstrip()[:1024].startswith(_PDF_MAGIC) and _PDF_MAGIC not in data[:2048]:
        raise UnsupportedFileError("Uploaded file does not appear to be a valid PDF.")


def _classify_element(category: str, text: str) -> ElementKind:
    category_lower = (category or "").lower()
    if category_lower == "table":
        return ElementKind.TABLE
    if category_lower == "title":
        return ElementKind.TITLE
    if category_lower in {"listitem", "list-item"}:
        return ElementKind.LIST_ITEM
    if is_footnote(text):
        return ElementKind.FOOTNOTE
    return ElementKind.NARRATIVE_TEXT


def _partition_with_unstructured(path: str, strategy: str) -> list[dict]:
    from unstructured.partition.pdf import partition_pdf  # heavy import, deferred

    elements = partition_pdf(
        filename=path,
        strategy=strategy,
        infer_table_structure=True,
        include_page_breaks=True,
    )
    out: list[dict] = []
    for el in elements:
        meta = el.metadata.to_dict() if el.metadata else {}
        out.append(
            {
                "text": getattr(el, "text", "") or "",
                "category": el.category if hasattr(el, "category") else "UncategorizedText",
                "page_number": meta.get("page_number"),
                "coordinates": meta.get("coordinates", {}).get("points") if meta.get("coordinates") else None,
                "text_as_html": meta.get("text_as_html"),
            }
        )
    return out


def _partition_with_tika(path: str, server_url: str) -> list[dict]:
    from tika import parser as tika_parser  # heavy import, deferred

    parsed = tika_parser.from_file(path, serverEndpoint=server_url, xmlContent=False)
    content = (parsed or {}).get("content") or ""
    out: list[dict] = []
    for line in content.splitlines():
        if not line.strip():
            continue
        out.append(
            {
                "text": line.strip(),
                "category": "UncategorizedText",
                "page_number": None,
                "coordinates": None,
                "text_as_html": None,
            }
        )
    return out


def _extract_metadata_from_elements(
    raw_elements: list[dict], filename: str | None, source_tag: str | None = None
) -> CircularMetadata:
    head_text = " \n".join(e["text"] for e in raw_elements[:40])

    # `source_tag` (set by the ingestion routing layer -- see
    # app.ingestion.regulator_sources -- when it already knows which
    # regulator's feed a document was discovered from) takes precedence
    # over text-sniffing; an ad-hoc upload with no source_tag falls back
    # to detecting the regulator from the document-number pattern found
    # in its own header text.
    regulator, document_type, circular_number = detect_regulator_and_document(head_text, source_tag)
    if circular_number is None and (m := _GENERIC_DOC_NUMBER_RE.search(head_text)):
        circular_number = m.group(0).strip()

    issue_date = None
    if m := _ISSUE_DATE_RE.search(head_text):
        try:
            from dateutil import parser as dateutil_parser  # light dependency

            issue_date = dateutil_parser.parse(m.group(1)).date()
        except (ValueError, ImportError):
            issue_date = None

    title = next((e["text"] for e in raw_elements if e["category"].lower() == "title"), None)

    return CircularMetadata(
        circular_number=circular_number,
        issue_date=issue_date,
        title=title,
        source_filename=filename,
        regulator=regulator,
        document_type=document_type,
    )


def _build_document_elements(raw_elements: list[dict]) -> list[DocumentElement]:
    tracker = HierarchyTracker()
    result: list[DocumentElement] = []

    for raw in raw_elements:
        text = raw["text"].strip()
        if not text:
            continue

        clause = detect_clause_number(text)
        kind = _classify_element(raw["category"], text)

        if kind == ElementKind.TABLE:
            section_path = tracker.current_path()
            result.append(
                DocumentElement(
                    element_id=str(uuid.uuid4()),
                    kind=ElementKind.TABLE,
                    text=raw.get("text_as_html") or text,
                    clause_number=None,
                    section_path=section_path,
                    page_number=raw.get("page_number"),
                    bbox=_bbox(raw),
                )
            )
            continue

        # `is_footnote`'s heuristic ("digit(s) + '.'/')' + text") is
        # structurally identical to a top-level clause header ("1. Applicability"
        # matches it exactly the same as a real footnote "1. As amended..." would).
        # Clause detection above is the more specific signal (a dedicated,
        # ordered pattern table vs. one loose regex), so a line already
        # recognized as a numbered clause/section header must never be
        # demoted to a footnote and silently dropped from the hierarchy
        # tracker -- that would corrupt section_path for every subsequent
        # clause nested under it.
        if kind == ElementKind.FOOTNOTE and clause is None:
            result.append(
                DocumentElement(
                    element_id=str(uuid.uuid4()),
                    kind=ElementKind.FOOTNOTE,
                    text=text,
                    section_path=tracker.current_path(),
                    page_number=raw.get("page_number"),
                    bbox=_bbox(raw),
                    is_footnote_ref=True,
                )
            )
            continue

        if clause is not None:
            section_path = tracker.update(clause.clause_number, clause.depth)
            header = is_section_header(text, clause)
            result.append(
                DocumentElement(
                    element_id=str(uuid.uuid4()),
                    kind=ElementKind.SECTION_HEADER if header else ElementKind.CLAUSE,
                    text=text,
                    clause_number=clause.clause_number,
                    section_path=section_path,
                    page_number=raw.get("page_number"),
                    bbox=_bbox(raw),
                )
            )
            continue

        result.append(
            DocumentElement(
                element_id=str(uuid.uuid4()),
                kind=kind,
                text=text,
                clause_number=None,
                section_path=tracker.current_path(),
                page_number=raw.get("page_number"),
                bbox=_bbox(raw),
            )
        )

    return result


def _rasterize_pdf(path: str, dpi: int) -> list:
    """Sync helper (run via asyncio.to_thread, matching this module's other
    backend calls): renders every page to a PIL Image via pdf2image, which
    shells out to poppler's `pdftoppm` -- already an OS-level dependency of
    this image (Dockerfile installs poppler-utils for Unstructured's own
    hi_res strategy), so this adds no new system dependency."""
    from pdf2image import convert_from_path  # deferred heavy import

    return convert_from_path(path, dpi=dpi)


def _ocr_page(image_path: str, settings: Settings) -> str:
    """Sync helper (run via asyncio.to_thread): OCRs one rasterized page
    image via app.localization.ocr's PaddleOCR->Tesseract fallback chain,
    forcing English since this fallback only runs for the plain English
    SEBI/RBI/IRDAI/PFRDA ingestion path -- app.localization.pipeline is the
    separate entrypoint for known-regional-language documents."""
    from app.localization.languages import RegionalLanguage
    from app.localization.ocr import extract_regional_text

    result = extract_regional_text(image_path, RegionalLanguage.ENGLISH, settings)
    return result.full_text


async def _ocr_fallback(source_path: Path, filename: str | None, settings: Settings) -> list[dict]:
    """Last-resort text recovery for a scanned/image-only PDF: rasterize
    each page and OCR it, returning elements in the same shape
    `_partition_with_unstructured`/`_partition_with_tika` produce so the
    rest of `extract_pdf` (metadata detection, DocumentElement building)
    is unaffected by which backend actually supplied the text. A page
    whose OCR fails or comes back empty is skipped, not fatal -- the
    caller decides whether the OVERALL result has enough text to proceed
    (same "no element has non-whitespace text" check as the primary path)."""
    try:
        pages = await asyncio.to_thread(_rasterize_pdf, str(source_path), 300)
    except Exception as exc:  # noqa: BLE001 - pdf2image/poppler failure never fatal to the caller; just yields no OCR text
        logger.error("OCR fallback: failed to rasterize '%s': %r", filename or source_path, exc)
        return []

    out: list[dict] = []
    for page_num, page_image in enumerate(pages, start=1):
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
            tmp_path = tmp.name
        try:
            await asyncio.to_thread(page_image.save, tmp_path)
            text = await asyncio.to_thread(_ocr_page, tmp_path, settings)
        except Exception as exc:  # noqa: BLE001 - one page's OCR failure must not abort the rest of the document
            logger.warning("OCR fallback: page %d of '%s' failed: %r", page_num, filename or source_path, exc)
            continue
        finally:
            Path(tmp_path).unlink(missing_ok=True)

        if text.strip():
            out.append(
                {
                    "text": text,
                    "category": "UncategorizedText",
                    "page_number": page_num,
                    "coordinates": None,
                    "text_as_html": None,
                }
            )
    return out


def _bbox(raw: dict) -> BoundingBox | None:
    coords = raw.get("coordinates")
    page = raw.get("page_number")
    if coords is None and page is None:
        return None
    return BoundingBox(page_number=page, coordinates=[tuple(p) for p in coords] if coords else None)


async def extract_pdf(
    *,
    file_bytes: bytes,
    source_path: Path,
    filename: str | None,
    settings: Settings,
    source_tag: str | None = None,
) -> tuple[CircularMetadata, list[DocumentElement]]:
    """Extract layout-aware elements from a PDF, preferring Unstructured and
    falling back to Tika if the primary backend errors out."""
    _validate_pdf_bytes(file_bytes)

    async def _run() -> list[dict]:
        try:
            if settings.extraction_backend == "unstructured":
                return await asyncio.to_thread(
                    _partition_with_unstructured, str(source_path), settings.unstructured_strategy
                )
            return await asyncio.to_thread(_partition_with_tika, str(source_path), settings.tika_server_url)
        except Exception as primary_exc:  # noqa: BLE001 - deliberate fallback boundary
            logger.warning("Primary extraction backend %s failed: %s", settings.extraction_backend, primary_exc)
            try:
                if settings.extraction_backend == "unstructured":
                    return await asyncio.to_thread(_partition_with_tika, str(source_path), settings.tika_server_url)
                return await asyncio.to_thread(
                    _partition_with_unstructured, str(source_path), settings.unstructured_strategy
                )
            except Exception as fallback_exc:  # noqa: BLE001
                raise ExtractionBackendError(
                    f"Both extraction backends failed. primary={primary_exc!r} fallback={fallback_exc!r}"
                ) from fallback_exc

    try:
        raw_elements = await asyncio.wait_for(_run(), timeout=settings.parse_timeout_seconds)
    except asyncio.TimeoutError as exc:
        raise ParseTimeoutError(
            f"PDF extraction exceeded {settings.parse_timeout_seconds}s timeout."
        ) from exc

    # A scanned/image-only PDF still commonly produces *some* raw elements
    # (Unstructured detects page/table regions from layout alone), just with
    # no recoverable text in any of them -- so "zero elements" alone
    # under-detects the scanned case. Checking for "no element has non-
    # whitespace text" catches both shapes with one branch.
    if not raw_elements or not any(el["text"].strip() for el in raw_elements):
        logger.warning(
            "'%s' produced %d element(s) with no extractable text -- likely a scanned/image-only PDF; "
            "attempting OCR fallback.",
            filename or "<unnamed upload>",
            len(raw_elements),
        )
        try:
            ocr_elements = await asyncio.wait_for(
                _ocr_fallback(source_path, filename, settings), timeout=settings.parse_timeout_seconds
            )
        except asyncio.TimeoutError as exc:
            raise ParseTimeoutError(
                f"OCR fallback for '{filename or 'document'}' exceeded {settings.parse_timeout_seconds}s timeout."
            ) from exc

        if not ocr_elements:
            raise ScannedDocumentError(
                f"'{filename or 'document'}' has no extractable text layer (likely a scanned/image-only PDF), "
                "and OCR fallback produced no usable text either. Re-submit a higher-quality scan, or if this "
                "is a known regional-language document, route it through app.localization.pipeline instead "
                "of this English-only ingestion path."
            )
        logger.info(
            "OCR fallback recovered text from %d/%d page(s) of '%s'.",
            len(ocr_elements), len(raw_elements) or len(ocr_elements), filename or "<unnamed upload>",
        )
        raw_elements = ocr_elements

    metadata = _extract_metadata_from_elements(raw_elements, filename, source_tag)
    elements = _build_document_elements(raw_elements)
    return metadata, elements
