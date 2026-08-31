"""Manual controls for the SEBI ingestion pipeline. The beat schedule
(`app.execution.celery_app`) is the primary trigger; this endpoint exists for
on-demand re-polls (e.g. "we know SEBI just published something, don't wait
for the next cycle") and for exposing task status to the dashboard.

Also home to the async manual-upload flow (POST /v1/ingestion/uploads +
GET /v1/ingestion/uploads/{job_id}) -- see app.db.models.IngestionUploadJob's
docstring for why this exists alongside the older, synchronous
POST /v1/circulars/parse-and-index (app.api.routes): a large PDF's hi-res
OCR + embedding pipeline can run far longer than any HTTP proxy's request
timeout, so this returns immediately and a Celery worker
(app.ingestion.tasks.process_manual_upload_task) does the actual work."""
from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.db.models import IngestionUploadJob
from app.db.session import get_db_session
from app.ingestion.tasks import poll_sebi_sources_task, process_manual_upload_task
from app.security.dependencies import get_current_principal, require_roles
from app.security.models import Principal, Role
from app.storage.object_store import ObjectStorageNotConfiguredError, upload_bytes

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/ingestion", tags=["ingestion"])

# Infrastructure/agent operation, not a compliance-content decision --
# System_Admin only. A Compliance_Officer reviews what ingestion produces
# (via the HITL review portal); they don't operate the ingestion pipeline
# itself.
_require_admin = Depends(require_roles(Role.SYSTEM_ADMIN))

# Same gate as the synchronous upload endpoint (app.api.routes'
# `_require_ingestion_role`): whoever may parse-and-index a circular
# synchronously may also do it via the async job flow.
_require_ingestion_role = Depends(require_roles(Role.COMPLIANCE_OFFICER, Role.SYSTEM_ADMIN))


class TriggerResponse(BaseModel):
    task_id: str
    status: str


@router.post("/poll", response_model=TriggerResponse, status_code=status.HTTP_202_ACCEPTED, dependencies=[_require_admin])
async def trigger_poll() -> TriggerResponse:
    """Enqueues one ingestion poll cycle immediately instead of waiting for
    the next `ingestion_poll_interval_seconds` beat tick."""
    async_result = poll_sebi_sources_task.delay()
    return TriggerResponse(task_id=async_result.id, status="queued")


@router.get("/poll/{task_id}", response_model=None, dependencies=[_require_admin])
async def poll_status(task_id: str) -> dict:
    async_result = poll_sebi_sources_task.AsyncResult(task_id)
    return {
        "task_id": task_id,
        "state": async_result.state,
        "result": async_result.result if async_result.ready() else None,
    }


class UploadJobResponse(BaseModel):
    job_id: str
    status: str


class UploadJobStatusResponse(BaseModel):
    job_id: str
    filename: str
    status: str
    chunks_indexed: int | None = None
    error_message: str | None = None


@router.post(
    "/uploads",
    response_model=UploadJobResponse,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[_require_ingestion_role],
)
async def create_upload_job(
    file: UploadFile = File(...),
    principal: Principal = Depends(get_current_principal),
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(get_db_session),
) -> UploadJobResponse:
    """Stages the uploaded PDF in object storage and enqueues
    `process_manual_upload_task` to parse + index it, instead of doing that
    work inline (see module docstring). Returns immediately with a job_id
    the caller polls via GET /uploads/{job_id}."""
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

    job_id = str(uuid.uuid4())
    object_key = f"uploads/{job_id}/{file.filename or 'circular.pdf'}"

    try:
        await upload_bytes(object_key, body, content_type=file.content_type or "application/pdf")

        job = IngestionUploadJob(
            job_id=job_id,
            filename=file.filename or "circular.pdf",
            object_key=object_key,
            status="queued",
            created_by=principal.subject,
        )
        session.add(job)
        await session.commit()

        process_manual_upload_task.delay(job_id)
    except ObjectStorageNotConfiguredError as exc:
        # An unhandled exception here would still reach the client as a
        # 500 -- but crucially it would skip CORSMiddleware's normal
        # response-header injection (that only wraps a clean response, not
        # a raw ASGI-level crash), so the browser sees no
        # Access-Control-Allow-Origin header and reports a generic "Failed
        # to fetch" instead of this endpoint's actual problem. Catching and
        # re-raising as HTTPException keeps this on the normal response
        # path so CORS headers -- and a debuggable error message -- still
        # reach the caller.
        logger.error("Manual upload rejected: object storage not configured: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Upload storage is not configured on this deployment.",
        ) from exc
    except Exception as exc:  # noqa: BLE001 - final safety net, never leak internals; see comment above
        logger.exception("Unhandled error creating upload job for '%s'", file.filename)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal error while starting the upload job.",
        ) from exc

    return UploadJobResponse(job_id=job_id, status="queued")


@router.get(
    "/uploads/{job_id}",
    response_model=UploadJobStatusResponse,
    dependencies=[_require_ingestion_role],
)
async def get_upload_job(
    job_id: str,
    session: AsyncSession = Depends(get_db_session),
) -> UploadJobStatusResponse:
    result = await session.execute(select(IngestionUploadJob).where(IngestionUploadJob.job_id == job_id))
    job = result.scalar_one_or_none()
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Upload job '{job_id}' not found.")

    return UploadJobStatusResponse(
        job_id=job.job_id,
        filename=job.filename,
        status=job.status,
        chunks_indexed=job.chunks_indexed,
        error_message=job.error_message,
    )
