"""Layout-aware PDF extraction using pypdf (fast text-layer pass), then
Unstructured (layout/table-aware) and Apache Tika as secondary backends,
with an OCR fallback (app.localization.ocr) below that for scanned/image-
only pages none of the text-layer backends could read.

`unstructured.partition.pdf.partition_pdf` and `tika.parser.from_file`
are synchronous, CPU/IO-heavy calls (the former shells out to detectron2/
poppler for layout+table detection, the latter talks to a Tika server over
HTTP). We run them on a dedicated bounded worker pool via
`asyncio.get_running_loop().run_in_executor` so the FastAPI event loop is
never blocked, and enforce a per-backend timeout so a pathological PDF
cannot pin a worker indefinitely.

IMPORTANT (why not a single shared cascade budget, and why not
`asyncio.to_thread`): each backend gets its OWN `asyncio.wait_for` budget.
With a single shared `parse_timeout_seconds` around the whole cascade, a
slow/hung primary backend (e.g. unstructured stalled on a network model
download) silently consumed the ENTIRE budget before the "cheap" pypdf
fallback ever ran -- the exact failure signature observed in production as
"PDF extraction exceeded configured timeout" for a 1-page text-layer PDF.
And `asyncio.to_thread` runs on the default executor (~32 threads on
CPython) and does NOT interrupt a worker thread when the awaiting coroutine
times out: a hung backend leaks a thread, and enough leaked threads
saturated the default pool so even pypdf was left queued behind them,
turning a one-off timeout into "every upload times out". The dedicated,
small `_EXTRACTION_EXECUTOR` below bounds that damage.
"""
from __future__ import annotations

import asyncio
import functools
import logging
import os
import re
import tempfile
import uuid
from concurrent.futures import ThreadPoolExecutor
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

# Dedicated, bounded worker pool for the extraction backend calls below.
# See the module docstring for why this is NOT asyncio.to_thread: leaked
# threads from timed-out backend calls must never saturate the default
# executor and starve every later parse.
_EXTRACTION_MAX_WORKERS = min(4, (os.cpu_count() or 1) + 2)
_EXTRACTION_EXECUTOR = ThreadPoolExecutor(
    max_workers=_EXTRACTION_MAX_WORKERS,
    thread_name_prefix="pdf-extract",
)

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

# unstructured's "hi_res" strategy (its detectron2 layout model) and
# PaddleOCR (if ever preferred for a regional-language OCR fallback -- see
# app.localization.ocr) both fetch ML model weights from Hugging Face Hub /
# their own CDN on first use. In a network-restricted deployment this
# doesn't fail with a clear "no internet" error -- it hangs or raises a
# generic connection/timeout error deep inside a third-party library that
# looks, from here, like a parsing bug. These markers let the hi_res
# escalation and OCR fallback below (the two model-loading call sites in
# this module) recognize that shape of failure and re-raise it as an
# actionable, typed ExtractionBackendError instead of whatever raw
# exception urllib3/requests/huggingface_hub happened to throw.
_MODEL_DOWNLOAD_ERROR_MARKERS = (
    "huggingface_hub",
    "hf_hub",
    "hfvalidationerror",
    "connectionerror",
    "maxretryerror",
    "newconnectionerror",
    "failed to establish a new connection",
    "getaddrinfo failed",
    "name or service not known",
    "temporary failure in name resolution",
    "read timed out",
    "connecttimeout",
    "connect timeout",
    "no route to host",
    "sslerror",
    "urllib3",
    "proxyerror",
)


def _is_model_download_failure(exc: BaseException) -> bool:
    """Best-effort classification of an exception as "couldn't fetch ML
    model weights over the network" rather than some other parsing
    failure, by sniffing the exception's type name and message for the
    markers huggingface_hub/urllib3/requests leave behind. Heuristic, not
    exhaustive -- a false negative just falls through to this module's
    existing, less specific error handling; a false positive is
    vanishingly unlikely given how distinctive these markers are."""
    text = f"{type(exc).__module__}.{type(exc).__qualname__}: {exc}".lower()
    return any(marker in text for marker in _MODEL_DOWNLOAD_ERROR_MARKERS)


def _model_download_error(stage: str, exc: BaseException) -> ExtractionBackendError:
    return ExtractionBackendError(
        f"{stage} could not load its ML model weights -- this looks like a failed network download "
        f"rather than a parsing bug (underlying error: {exc!r}). If this deployment has restricted or "
        "no internet egress, pre-warm the model weights into the Docker image at build time (see the "
        "Dockerfile's model pre-warming step) or set HF_HUB_OFFLINE=1 with the weights already cached "
        "on disk, instead of relying on a first-use download at runtime."
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


def _partition_with_pypdf(path: str) -> list[dict]:
    """Native pypdf extraction for text-layer PDFs. Requires no external
    Java (Tika) or poppler/detectron2 (Unstructured) system dependencies.
    Produces elements in the identical shape _partition_with_unstructured /
    _partition_with_tika emit."""
    import pypdf  # light dependency, native Python

    reader = pypdf.PdfReader(path)
    out: list[dict] = []
    for page_num, page in enumerate(reader.pages, start=1):
        try:
            page_text = page.extract_text() or ""
        except Exception as exc:  # noqa: BLE001
            logger.warning("pypdf extraction error on page %d of '%s': %r", page_num, path, exc)
            continue

        for line in page_text.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            out.append(
                {
                    "text": stripped,
                    "category": "UncategorizedText",
                    "page_number": page_num,
                    "coordinates": None,
                    "text_as_html": None,
                }
            )
    return out


def _has_text(elements: list[dict]) -> bool:
    """Return True if at least one element contains non-whitespace text."""
    return bool(elements) and any(bool(el.get("text", "").strip()) for el in elements)


async def _run_backend(call, timeout: float):
    """Run one synchronous partition backend call on the dedicated bounded
    executor with its OWN wait_for budget. A slow/hung backend burns only
    its own budget (the cheaper backends that follow stay eligible), and
    the thread it leaks is confined to the small `_EXTRACTION_EXECUTOR`
    pool so it cannot starve other work."""
    loop = asyncio.get_running_loop()
    return await asyncio.wait_for(loop.run_in_executor(_EXTRACTION_EXECUTOR, call), timeout=timeout)



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


async def _ocr_one_page(
    page_num: int, page_image, filename: str | None, source_path: Path, settings: Settings, semaphore: asyncio.Semaphore
) -> dict | None:
    """OCRs a single rasterized page, gated by `semaphore` (bounds how many
    pages hold a rasterized image + in-flight OCR call at once --
    settings.ocr_page_concurrency). Returns None (never raises) if this
    page's OCR fails or comes back empty, so one bad page can't sink the
    rest of the document -- same isolation as the previous sequential loop,
    just per-task instead of per-iteration."""
    async with semaphore:
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
            tmp_path = tmp.name
        try:
            await asyncio.to_thread(page_image.save, tmp_path)
            text = await asyncio.to_thread(_ocr_page, tmp_path, settings)
        except Exception as exc:  # noqa: BLE001 - one page's OCR failure must not abort the rest of the document
            if _is_model_download_failure(exc):
                # Not a per-page problem -- every remaining page would fail
                # identically (and identically slowly) for the same reason,
                # so abort the whole document with a clear diagnosis instead
                # of retrying the same doomed download once per page.
                raise _model_download_error("OCR fallback's text-recognition backend", exc) from exc
            logger.warning("OCR fallback: page %d of '%s' failed: %r", page_num, filename or source_path, exc)
            return None
        finally:
            Path(tmp_path).unlink(missing_ok=True)

    if not text.strip():
        return None
    return {
        "text": text,
        "category": "UncategorizedText",
        "page_number": page_num,
        "coordinates": None,
        "text_as_html": None,
    }


async def _ocr_fallback(source_path: Path, filename: str | None, settings: Settings) -> list[dict]:
    """Last-resort text recovery for a scanned/image-only PDF: rasterize
    every page and OCR them concurrently (bounded by
    settings.ocr_page_concurrency, since 100+ pages OCR'd strictly in
    series was the second-largest contributor to slow ingestion after the
    hi_res default), returning elements in the same shape
    `_partition_with_unstructured`/`_partition_with_tika` produce so the
    rest of `extract_pdf` (metadata detection, DocumentElement building)
    is unaffected by which backend actually supplied the text. A page
    whose OCR fails or comes back empty is skipped, not fatal -- the
    caller decides whether the OVERALL result has enough text to proceed
    (same "no element has non-whitespace text" check as the primary path).
    `asyncio.gather` returns results in the order its awaitables were
    passed in, regardless of completion order, so the output list stays in
    page_number order even though pages complete OCR out of order."""
    try:
        pages = await asyncio.to_thread(_rasterize_pdf, str(source_path), 300)
    except Exception as exc:  # noqa: BLE001 - pdf2image/poppler failure never fatal to the caller; just yields no OCR text
        logger.error("OCR fallback: failed to rasterize '%s': %r", filename or source_path, exc)
        return []

    semaphore = asyncio.Semaphore(max(1, settings.ocr_page_concurrency))
    results = await asyncio.gather(
        *(
            _ocr_one_page(page_num, page_image, filename, source_path, settings, semaphore)
            for page_num, page_image in enumerate(pages, start=1)
        )
    )
    return [element for element in results if element is not None]


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
        # Extraction cascade order and per-backend budgets.
        #
        # pypdf is the ONLY backend with no third-party system/model
        # dependencies and no outbound network calls: it is instant for a
        # text-layer PDF (every real SEBI/RBI/IRDAI/PFRDA circular). It
        # runs FIRST with its own small budget so a slow/hung configured
        # backend (unstructured's layout model / a network model download)
        # can never starve it; the configured backend is then attempted
        # for documents pypdf cannot read (scanned/layout-heavy), with
        # tika last of all.
        candidate_order = ["pypdf"]
        for b in (settings.extraction_backend, "unstructured", "tika", "pypdf"):
            if b not in candidate_order:
                candidate_order.append(b)

        errors: list[tuple[str, Exception]] = []
        last_empty_result: list[dict] | None = None

        # Each backend gets its own budget -- never one shared budget for
        # the whole cascade (see the module docstring). pypdf is capped so
        # a pathological text-layer PDF is declined quickly and the slower
        # layout-aware backends still get most of parse_timeout_seconds.
        per_backend_timeout = {
            "pypdf": min(30, settings.parse_timeout_seconds),
            "unstructured": settings.parse_timeout_seconds,
            "tika": min(60, settings.parse_timeout_seconds),
        }

        for backend in candidate_order:
            if backend == "unstructured":
                call = functools.partial(_partition_with_unstructured, str(source_path), settings.unstructured_strategy)
            elif backend == "tika":
                call = functools.partial(_partition_with_tika, str(source_path), settings.tika_server_url)
            elif backend == "pypdf":
                call = functools.partial(_partition_with_pypdf, str(source_path))
            else:
                logger.warning("Unknown extraction backend '%s' configured; skipping.", backend)
                continue

            timeout = per_backend_timeout.get(backend, min(60, settings.parse_timeout_seconds))
            try:
                res = await _run_backend(call, timeout)
                if _has_text(res):
                    return res
                last_empty_result = res
            except asyncio.TimeoutError:
                logger.warning(
                    "Extraction backend '%s' exceeded its %ss budget for '%s'; trying next backend.",
                    backend, timeout, filename or source_path,
                )
                errors.append((backend, asyncio.TimeoutError(f"backend exceeded {timeout}s budget")))
            except Exception as exc:  # noqa: BLE001 - deliberate cascade boundary
                logger.warning("Extraction backend '%s' failed for '%s': %r", backend, filename or source_path, exc)
                errors.append((backend, exc))

        if last_empty_result is not None:
            return last_empty_result

        error_details = " ".join(f"{b}={e!r}" for b, e in errors)
        raise ExtractionBackendError(
            f"All extraction backends failed ({', '.join(candidate_order)}). {error_details}"
        )

    try:
        raw_elements = await asyncio.wait_for(_run(), timeout=settings.parse_timeout_seconds)
    except asyncio.TimeoutError as exc:
        raise ParseTimeoutError(
            f"PDF extraction exceeded {settings.parse_timeout_seconds}s timeout."
        ) from exc

    # "fast" (the default -- see unstructured_strategy in app.config) skips
    # detectron2 layout detection, so it can occasionally miss text on a
    # layout it doesn't handle well even though the PDF has a real text
    # layer. Before assuming that's a scanned/image-only PDF and paying for
    # OCR, retry once with "hi_res" -- much cheaper than OCR and often
    # enough to recover text "fast" under-extracted. This escalation is not
    # gated on the configured backend name: pypdf (the default first
    # backend) is a plain text-layer extractor, so a layout that defeats it
    # benefits from the hi_res retry exactly as much as one that defeated
    # unstructured "fast" would.
    if not _has_text(raw_elements) and settings.unstructured_strategy == "fast":
        logger.info(
            "'%s' produced no extractable text with the 'fast' Unstructured strategy -- retrying with "
            "'hi_res' before falling back to OCR.",
            filename or "<unnamed upload>",
        )
        try:
            hi_res_elements = await _run_backend(
                functools.partial(_partition_with_unstructured, str(source_path), "hi_res"),
                settings.parse_timeout_seconds,
            )
        except asyncio.TimeoutError as exc:
            raise ParseTimeoutError(
                f"hi_res retry for '{filename or 'document'}' exceeded {settings.parse_timeout_seconds}s timeout."
            ) from exc
        except Exception as exc:  # noqa: BLE001 - hi_res retry failing is not fatal; OCR is still the last resort
            if _is_model_download_failure(exc):
                # OCR (the next fallback) depends on model weights too (see
                # app.localization.ocr) -- if hi_res couldn't reach the
                # network for its weights, silently falling through would
                # just repeat the same failure there. Fail fast and clearly
                # instead of surfacing a misleading "scanned document"
                # error once OCR fails for the same underlying reason.
                raise _model_download_error("The 'hi_res' Unstructured strategy (detectron2 layout model)", exc) from exc
            logger.warning("hi_res retry failed for '%s': %r", filename or source_path, exc)
        else:
            if _has_text(hi_res_elements):
                raw_elements = hi_res_elements

    if not _has_text(raw_elements):
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
