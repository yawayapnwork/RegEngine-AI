"""Manual controls for the SEBI ingestion pipeline. The beat schedule
(`app.execution.celery_app`) is the primary trigger; this endpoint exists for
on-demand re-polls (e.g. "we know SEBI just published something, don't wait
for the next cycle") and for exposing task status to the dashboard."""
from __future__ import annotations

from fastapi import APIRouter, status
from pydantic import BaseModel

from app.ingestion.tasks import poll_sebi_sources_task

router = APIRouter(prefix="/v1/ingestion", tags=["ingestion"])


class TriggerResponse(BaseModel):
    task_id: str
    status: str


@router.post("/poll", response_model=TriggerResponse, status_code=status.HTTP_202_ACCEPTED)
async def trigger_poll() -> TriggerResponse:
    """Enqueues one ingestion poll cycle immediately instead of waiting for
    the next `ingestion_poll_interval_seconds` beat tick."""
    async_result = poll_sebi_sources_task.delay()
    return TriggerResponse(task_id=async_result.id, status="queued")


@router.get("/poll/{task_id}", response_model=None)
async def poll_status(task_id: str) -> dict:
    async_result = poll_sebi_sources_task.AsyncResult(task_id)
    return {
        "task_id": task_id,
        "state": async_result.state,
        "result": async_result.result if async_result.ready() else None,
    }
