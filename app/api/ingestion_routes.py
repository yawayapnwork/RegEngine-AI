"""Manual controls for the SEBI ingestion pipeline. The beat schedule
(`app.execution.celery_app`) is the primary trigger; this endpoint exists for
on-demand re-polls (e.g. "we know SEBI just published something, don't wait
for the next cycle") and for exposing task status to the dashboard."""
from __future__ import annotations

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel

from app.ingestion.tasks import poll_sebi_sources_task
from app.security.dependencies import require_roles
from app.security.models import Role

router = APIRouter(prefix="/v1/ingestion", tags=["ingestion"])

# Infrastructure/agent operation, not a compliance-content decision --
# System_Admin only. A Compliance_Officer reviews what ingestion produces
# (via the HITL review portal); they don't operate the ingestion pipeline
# itself.
_require_admin = Depends(require_roles(Role.SYSTEM_ADMIN))


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
