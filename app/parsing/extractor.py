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
from app.parsing.exceptions import ExtractionBackendError, ParseTimeoutError, UnsupportedFileError
from app.parsing.hierarchy import HierarchyTracker, detect_clause_number, is_footnote, is_section_header

logger = logging.getLogger(__name__)

_PDF_MAGIC = b"%PDF-"

_CIRCULAR_NUMBER_RE = re.compile(
    r"(SEBI/[A-Z\-]+/\d{4}[-/]\d{2,4}/\d+|Circular\s+No\.?\s*[:\-]?\s*[\w/\-]+)",
    re.IGNORECASE,
)
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


def _extract_metadata_from_elements(raw_elements: list[dict], filename: str | None) -> CircularMetadata:
    head_text = " \n".join(e["text"] for e in raw_elements[:40])
    circular_number = None
    if m := _CIRCULAR_NUMBER_RE.search(head_text):
        circular_number = m.group(1).strip()

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

        if kind == ElementKind.FOOTNOTE:
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

    if not raw_elements:
        raise ExtractionBackendError("Extraction produced zero elements; document may be scanned/unreadable.")

    metadata = _extract_metadata_from_elements(raw_elements, filename)
    elements = _build_document_elements(raw_elements)
    return metadata, elements
