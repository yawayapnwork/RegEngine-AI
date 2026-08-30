"""Layout-aware PDF extraction using Unstructured (primary) with an Apache
Tika fallback.

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
            "'%s' produced %d element(s) with no extractable text -- likely a scanned/image-only PDF with no text layer.",
            filename or "<unnamed upload>",
            len(raw_elements),
        )
        raise ScannedDocumentError(
            f"'{filename or 'document'}' has no extractable text layer (likely a scanned/image-only PDF). "
            "Retrying the same extraction backend will not help -- route this document through the regional "
            "OCR pipeline (app.localization.ocr.extract_regional_text) instead, or re-submit a PDF with a "
            "real text layer."
        )

    metadata = _extract_metadata_from_elements(raw_elements, filename, source_tag)
    elements = _build_document_elements(raw_elements)
    return metadata, elements
