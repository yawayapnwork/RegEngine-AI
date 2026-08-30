"""HTTP surface for the SEBI Master Circular parsing service."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status

from app.config import Settings, get_settings
from app.models import IndexRequest, IndexResponse, ParseResult
from app.parsing.exceptions import (
    ChunkingError,
    EmbeddingError,
    ExtractionBackendError,
    IndexingError,
    ParseTimeoutError,
    ScannedDocumentError,
    UnsupportedFileError,
)
from app.security.dependencies import require_roles
from app.security.models import Role
from app.services.pipeline import parse_pdf_bytes
from app.vectorstore.qdrant_store import index_chunks

logger = logging.getLogger(__name__)
router = APIRouter()

# Ingesting a new circular is what feeds the compiler pipeline that
# eventually produces enforceable policy -- gated the same as the HITL
# review portal's read access: compliance officers and infra admins, never
# a broker's own API client.
_require_ingestion_role = Depends(require_roles(Role.COMPLIANCE_OFFICER, Role.SYSTEM_ADMIN))

_ERROR_STATUS_MAP: dict[type[Exception], int] = {
    UnsupportedFileError: status.HTTP_422_UNPROCESSABLE_ENTITY,
    # Must be listed before ExtractionBackendError: `_map_status` looks up
    # `type(exc)` exactly (no MRO walk), so this subclass needs its own
    # entry or it silently falls through to ExtractionBackendError's 502 --
    # wrong here, since a scanned PDF is a client-fixable content problem
    # (422: resubmit via OCR or a text-layer PDF), not a broken backend.
    ScannedDocumentError: status.HTTP_422_UNPROCESSABLE_ENTITY,
    ExtractionBackendError: status.HTTP_502_BAD_GATEWAY,
    ParseTimeoutError: status.HTTP_504_GATEWAY_TIMEOUT,
    ChunkingError: status.HTTP_422_UNPROCESSABLE_ENTITY,
    EmbeddingError: status.HTTP_502_BAD_GATEWAY,
    IndexingError: status.HTTP_502_BAD_GATEWAY,
}


def _map_status(exc: Exception) -> int:
    return _ERROR_STATUS_MAP.get(type(exc), status.HTTP_500_INTERNAL_SERVER_ERROR)


@router.post(
    "/v1/circulars/parse",
    response_model=ParseResult,
    status_code=status.HTTP_200_OK,
    dependencies=[_require_ingestion_role],
)
async def parse_circular(
    file: UploadFile = File(...),
    settings: Settings = Depends(get_settings),
) -> ParseResult:
    """Upload a SEBI Master Circular PDF and receive layout-aware,
    clause-hashed chunks ready for indexing."""
    if file.content_type not in ("application/pdf", "application/octet-stream", None):
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=f"Unsupported content type: {file.content_type}",
        )

    body = await file.read()
    if len(body) > settings.max_upload_mb * 1024 * 1024:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"File exceeds {settings.max_upload_mb}MB limit.",
        )

    try:
        return await parse_pdf_bytes(body, file.filename, settings)
    except tuple(_ERROR_STATUS_MAP.keys()) as exc:
        logger.warning("Parse failed for '%s': %s", file.filename, exc)
        raise HTTPException(status_code=_map_status(exc), detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - final safety net, never leak internals
        logger.exception("Unhandled error parsing '%s'", file.filename)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal error while parsing the document.",
        ) from exc


@router.post(
    "/v1/circulars/index",
    response_model=IndexResponse,
    status_code=status.HTTP_200_OK,
    dependencies=[_require_ingestion_role],
)
async def index_circular(
    request: IndexRequest,
    settings: Settings = Depends(get_settings),
) -> IndexResponse:
    """Embed and upsert a set of previously parsed clause chunks into Qdrant."""
    try:
        return await index_chunks(request.chunks, settings, recreate_collection=request.recreate_collection)
    except (EmbeddingError, IndexingError) as exc:
        logger.warning("Indexing failed: %s", exc)
        raise HTTPException(status_code=_map_status(exc), detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        logger.exception("Unhandled error during indexing")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal error while indexing chunks.",
        ) from exc


@router.post(
    "/v1/circulars/parse-and-index",
    response_model=IndexResponse,
    status_code=status.HTTP_200_OK,
    dependencies=[_require_ingestion_role],
)
async def parse_and_index_circular(
    file: UploadFile = File(...),
    recreate_collection: bool = False,
    settings: Settings = Depends(get_settings),
) -> IndexResponse:
    """Convenience endpoint: parse a PDF and index its chunks in one call."""
    parsed = await parse_circular(file=file, settings=settings)
    try:
        return await index_chunks(parsed.chunks, settings, recreate_collection=recreate_collection)
    except (EmbeddingError, IndexingError) as exc:
        logger.warning("Indexing failed for '%s': %s", file.filename, exc)
        raise HTTPException(status_code=_map_status(exc), detail=str(exc)) from exc


@router.get("/healthz", status_code=status.HTTP_200_OK)
async def healthz() -> dict[str, str]:
    return {"status": "ok"}
