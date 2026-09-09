"""FastAPI endpoints for M&A Compliance Due-Diligence Agent.

  POST /v1/mna/compare
      Initiate an M&A compliance due-diligence comparison between two regulated entities.
      Requires dual-entity authorization.

  GET  /v1/mna/jobs/{job_id}
      Retrieve comparison progress, status, and the final due-diligence audit report.

  POST /v1/mna/jobs/{job_id}/cancel
      Cancel an in-progress comparison job.
"""
from __future__ import annotations

import asyncio
import uuid
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.config import Settings, get_settings
from app.db.session import get_session_factory
from app.mna_due_diligence.auth import verify_dual_entity_authorization
from app.mna_due_diligence.models import (
    MNAComparisonRequest,
    MNAJobProgress,
    MNAJobStatus,
)
from app.mna_due_diligence.tasks import MNAJobManager, run_mna_job_pipeline
from app.security.dependencies import get_current_principal, require_roles
from app.security.models import Principal, Role

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/mna", tags=["M&A Compliance Due-Diligence"])

_ALLOWED = require_roles(Role.COMPLIANCE_OFFICER, Role.SYSTEM_ADMIN, Role.BROKER_API_CLIENT)


@router.post(
    "/compare",
    response_model=MNAJobProgress,
    status_code=status.HTTP_200_OK,
    dependencies=[Depends(_ALLOWED)],
)
async def initiate_mna_comparison(
    request: MNAComparisonRequest,
    principal: Principal = Depends(get_current_principal),
    settings: Settings = Depends(get_settings),
) -> Any:
    """Initiates an M&A compliance due-diligence comparison between two entities.

    Enforces:
    - Dual authorization check (anti-cross-tenant leakage).
    - Returns 202 Accepted if run_in_background is requested, else 200 OK once complete.
    """
    if not settings.mna_due_diligence_enabled:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="M&A Compliance Due-Diligence Agent is disabled.",
        )

    session_factory = get_session_factory()
    async with session_factory() as session:
        await verify_dual_entity_authorization(
            principal=principal,
            entity_a_id=request.entity_a_id,
            entity_b_id=request.entity_b_id,
            db=session,
        )

    job_id = str(uuid.uuid4())
    job_manager = MNAJobManager(settings)
    job = MNAJobProgress(
        job_id=job_id,
        entity_a_id=request.entity_a_id,
        entity_b_id=request.entity_b_id,
        initiator_subject=principal.subject,
        status=MNAJobStatus.QUEUED,
        progress_pct=0,
        current_step="Queued for execution",
    )
    job_manager.save_job(job)

    ledger_engine = create_async_engine(settings.ledger_database_url, pool_pre_ping=True)

    if request.run_in_background:
        # Launch background async task
        asyncio.create_task(
            _execute_background_job(
                job_id=job_id,
                request=request,
                initiator=principal.subject,
                session_factory=session_factory,
                ledger_engine=ledger_engine,
                settings=settings,
            )
        )
        return job

    # Synchronous execution
    try:
        report = await run_mna_job_pipeline(
            job_id=job_id,
            request=request,
            initiator=principal.subject,
            session_factory=session_factory,
            ledger_engine=ledger_engine,
            settings=settings,
        )
        updated_job = job_manager.get_job(job_id)
        return updated_job or job
    finally:
        await ledger_engine.dispose()


async def _execute_background_job(
    job_id: str,
    request: MNAComparisonRequest,
    initiator: str,
    session_factory: Any,
    ledger_engine: AsyncEngine,
    settings: Settings,
) -> None:
    try:
        await run_mna_job_pipeline(
            job_id=job_id,
            request=request,
            initiator=initiator,
            session_factory=session_factory,
            ledger_engine=ledger_engine,
            settings=settings,
        )
    except Exception as exc:
        logger.exception("Background MNA job %s failed: %s", job_id, exc)
    finally:
        await ledger_engine.dispose()


@router.get(
    "/jobs/{job_id}",
    response_model=MNAJobProgress,
    dependencies=[Depends(_ALLOWED)],
)
async def get_mna_job(
    job_id: str,
    principal: Principal = Depends(get_current_principal),
    settings: Settings = Depends(get_settings),
) -> MNAJobProgress:
    """Retrieves progress, status, and final report for an M&A due-diligence job."""
    job_manager = MNAJobManager(settings)
    job = job_manager.get_job(job_id)
    if not job:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"M&A Due-Diligence job '{job_id}' not found.",
        )

    # Scoping check: non-admin principal must be initiator or have authorized access to both entities
    if not principal.is_admin() and Role.COMPLIANCE_OFFICER not in principal.roles:
        if job.initiator_subject != principal.subject:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Not authorized to inspect this comparison job.",
            )

    return job


@router.post(
    "/jobs/{job_id}/cancel",
    dependencies=[Depends(_ALLOWED)],
)
async def cancel_mna_job(
    job_id: str,
    principal: Principal = Depends(get_current_principal),
    settings: Settings = Depends(get_settings),
) -> dict[str, str]:
    """Cancels an in-progress M&A due-diligence job."""
    job_manager = MNAJobManager(settings)
    success = job_manager.cancel_job(job_id)
    if not success:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Job '{job_id}' cannot be cancelled (may be completed, already cancelled, or not found).",
        )
    return {"status": "cancelled", "job_id": job_id}
