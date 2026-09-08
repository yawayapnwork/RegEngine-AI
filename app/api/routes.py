"""HTTP surface for the SEBI Master Circular parsing service."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.db.session import get_db_session
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
from app.services.orchestrator import CircularStatusResult, E2EOrchestrator, ProcessE2EResult
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


_SAFE_ERROR_MESSAGES: dict[type[Exception], str] = {
    UnsupportedFileError: "Uploaded file is not a valid or supported PDF.",
    ScannedDocumentError: "Document appears to be a scanned image with no readable text layer.",
    ExtractionBackendError: "Document text extraction failed across all configured backends.",
    ParseTimeoutError: "PDF extraction exceeded configured timeout.",
    ChunkingError: "Failed to segment document into valid semantic chunks.",
    EmbeddingError: "Failed to generate vector embeddings for document chunks.",
    IndexingError: "Failed to index document chunks into vector search.",
}


def _safe_error_detail(exc: Exception, default: str = "Internal error while processing request.") -> str:
    return _SAFE_ERROR_MESSAGES.get(type(exc), default)


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
        raise HTTPException(status_code=_map_status(exc), detail=_safe_error_detail(exc, "Internal error while parsing the document.")) from exc
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
        raise HTTPException(status_code=_map_status(exc), detail=_safe_error_detail(exc, "Internal error while indexing chunks.")) from exc
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
        raise HTTPException(status_code=_map_status(exc), detail=_safe_error_detail(exc, "Internal error while indexing chunks.")) from exc
    except Exception as exc:  # noqa: BLE001
        logger.exception("Unhandled error indexing after parse for '%s'", file.filename)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal error while indexing chunks.",
        ) from exc


@router.post(
    "/v1/circulars/process-e2e",
    response_model=ProcessE2EResult,
    status_code=status.HTTP_200_OK,
    dependencies=[_require_ingestion_role],
)
async def process_e2e_circular(
    file: UploadFile = File(...),
    session: AsyncSession = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> ProcessE2EResult:
    """Accept a regulatory PDF and execute the complete end-to-end pipeline:
    parse_pdf -> persist_circular -> persist_clauses -> extract_and_audit
    -> compile_rule -> persist_compiled_rule -> create_hitl_review
    """
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

    orchestrator = E2EOrchestrator(settings)
    try:
        return await orchestrator.process_circular_pdf(
            session=session, file_bytes=body, filename=file.filename
        )
    except tuple(_ERROR_STATUS_MAP.keys()) as exc:
        logger.warning("E2E processing failed for '%s': %s", file.filename, exc)
        raise HTTPException(status_code=_map_status(exc), detail=_safe_error_detail(exc, "Internal error processing circular.")) from exc
    except Exception as exc:  # noqa: BLE001
        logger.exception("Unhandled error processing circular '%s'", file.filename)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal error processing circular.",
        ) from exc


@router.get(
    "/v1/circulars/{circular_id}/status",
    response_model=CircularStatusResult,
    status_code=status.HTTP_200_OK,
    dependencies=[_require_ingestion_role],
)
async def get_circular_status(
    circular_id: int,
    session: AsyncSession = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> CircularStatusResult:
    """Retrieve the lifecycle status of an ingested circular:
    processing | review_required | approved | deployed | failed
    """
    orchestrator = E2EOrchestrator(settings)
    status_result = await orchestrator.get_circular_status(session, circular_id)
    if status_result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Circular with ID {circular_id} not found.",
        )
    return status_result


@router.get(
    "/v1/circulars/status/{circular_id}",
    response_model=CircularStatusResult,
    status_code=status.HTTP_200_OK,
    dependencies=[_require_ingestion_role],
    include_in_schema=False,
)
async def get_circular_status_alias(
    circular_id: int,
    session: AsyncSession = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> CircularStatusResult:
    return await get_circular_status(circular_id=circular_id, session=session, settings=settings)


@router.get(
    "/v1/circulars",
    dependencies=[_require_ingestion_role],
    status_code=status.HTTP_200_OK,
)
async def list_circulars(
    session: AsyncSession = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> list[dict[str, Any]]:
    """List all ingested regulatory circulars with their live lifecycle status."""
    from sqlalchemy import select
    from app.db.models import Circular

    result = await session.execute(select(Circular).order_by(Circular.created_at.desc()))
    circulars = result.scalars().all()

    orchestrator = E2EOrchestrator(settings)
    items = []
    for c in circulars:
        c_status = await orchestrator.get_circular_status(session, c.id)
        items.append({
            "id": c.id,
            "circular_number": c.circular_number,
            "title": c.title,
            "issue_date": str(c.issue_date) if c.issue_date else None,
            "source_url": c.source_url,
            "source_filename": getattr(c, "source_filename", None),
            "source_retrieved_at": c.source_retrieved_at.isoformat() if getattr(c, "source_retrieved_at", None) else None,
            "source_document_sha256": c.source_document_sha256,
            "extracted_text_sha256": c.raw_text_digest,
            "raw_text_digest": c.raw_text_digest,
            "created_at": c.created_at.isoformat() if c.created_at else None,
            "status": c_status.status if c_status else "unknown",
            "clause_count": c_status.clause_count if c_status else 0,
            "active_rules": c_status.active_rules if c_status else 0,
            "pending_reviews": c_status.pending_reviews if c_status else 0,
        })
    return items


@router.get(
    "/v1/circulars/{circular_id}/details",
    dependencies=[_require_ingestion_role],
    status_code=status.HTTP_200_OK,
)
async def get_circular_details(
    circular_id: int,
    session: AsyncSession = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """Retrieve full circular detail including parsed clauses, compiled rules, and reviews."""
    from sqlalchemy import select
    from app.db.models import Circular, Clause, CompiledRule, HITLReview

    circular = await session.get(Circular, circular_id)
    if circular is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Circular with ID {circular_id} not found.",
        )

    clauses = list((await session.execute(select(Clause).where(Clause.circular_id == circular_id).order_by(Clause.id.asc()))).scalars().all())
    clause_ids = [c.id for c in clauses]

    rules_by_clause: dict[int, list[CompiledRule]] = {cid: [] for cid in clause_ids}
    reviews_by_clause: dict[int, list[HITLReview]] = {cid: [] for cid in clause_ids}

    if clause_ids:
        all_rules = (await session.execute(select(CompiledRule).where(CompiledRule.clause_id.in_(clause_ids)))).scalars().all()
        for r in all_rules:
            rules_by_clause[r.clause_id].append(r)

        all_reviews = (await session.execute(select(HITLReview).where(HITLReview.clause_id.in_(clause_ids)))).scalars().all()
        for rev in all_reviews:
            reviews_by_clause[rev.clause_id].append(rev)

    orchestrator = E2EOrchestrator(settings)
    status_obj = await orchestrator.get_circular_status(session, circular_id)

    clauses_out = []
    for c in clauses:
        c_rules = rules_by_clause.get(c.id, [])
        c_reviews = reviews_by_clause.get(c.id, [])
        clauses_out.append({
            "id": c.id,
            "clause_number": c.clause_number,
            "section_title": c.section_title,
            "section_path": c.section_path,
            "text": c.text,
            "sha256": c.sha256,
            "rules": [
                {
                    "rule_id": r.rule_id,
                    "rule_version": r.rule_version,
                    "rego_policy": r.rego_policy,
                    "jsonlogic_ast": r.jsonlogic_ast,
                    "is_compiled": r.is_compiled,
                    "is_active": r.is_active,
                    "hitl_status": r.hitl_status,
                }
                for r in c_rules
            ],
            "reviews": [
                {
                    "review_id": rev.review_id,
                    "reason_code": rev.reason_code,
                    "severity": rev.severity,
                    "description": rev.description,
                    "source_excerpt": rev.source_excerpt,
                    "status": rev.status,
                    "compliance_officer_id": rev.compliance_officer_id,
                    "resolved_at": rev.resolved_at.isoformat() if rev.resolved_at else None,
                }
                for rev in c_reviews
            ],
        })

    return {
        "circular": {
            "id": circular.id,
            "circular_number": circular.circular_number,
            "title": circular.title,
            "issue_date": str(circular.issue_date) if circular.issue_date else None,
            "source_url": circular.source_url,
            "source_filename": getattr(circular, "source_filename", None),
            "source_retrieved_at": circular.source_retrieved_at.isoformat() if getattr(circular, "source_retrieved_at", None) else None,
            "source_document_sha256": circular.source_document_sha256,
            "extracted_text_sha256": circular.raw_text_digest,
            "raw_text_digest": circular.raw_text_digest,
            "status": status_obj.status if status_obj else "unknown",
        },
        "clauses": clauses_out,
    }


@router.get("/healthz", status_code=status.HTTP_200_OK)
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


