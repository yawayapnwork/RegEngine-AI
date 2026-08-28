"""Dead-Letter Queue administration: inspect, edit, requeue, or discard a
failed pipeline item (an unparseable PDF, a failed LLM extraction call, a
malformed compiled AST, a vector-DB ingestion failure, or an exhausted
RSS-polling retry).

Role mapping note: the task this module implements is specified for
"compliance engineers", which is not one of this system's three RBAC
roles (Compliance_Officer / Broker_API_Client / System_Admin -- see
app.security.models.Role's docstring). Inspecting/requeueing pipeline
failures is infrastructure/pipeline operations, not a compliance sign-off
decision (a DLQ item hasn't been evaluated as compliant/non-compliant at
all -- it never got that far), so it is gated to System_Admin, matching
that role's documented remit ("infrastructure/agent management").
Compliance_Officer is deliberately NOT granted access here, for the same
separation-of-duties reason app.api.hitl_review_routes keeps
System_Admin OUT of policy approval: the two roles' authorities don't
overlap in either direction.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from app.execution.celery_app import celery_app
from app.resilience.dead_letter_queue import DeadLetterQueue, DLQEntryNotFoundError, DLQInvalidTransitionError
from app.resilience.dependencies import get_dlq
from app.resilience.models import DLQEntry, DLQEntryUpdate, DLQResolveRequest, DLQStats, DLQStatus, FailureCategory
from app.security.dependencies import require_roles
from app.security.models import Principal, Role

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/admin/dlq", tags=["dlq-admin"])

_require_admin = Depends(require_roles(Role.SYSTEM_ADMIN))


class DLQRequeueResponse(BaseModel):
    entry: DLQEntry
    dispatched_task_id: str


@router.get("", response_model=list[DLQEntry], dependencies=[_require_admin])
async def list_dlq_entries(
    category: FailureCategory | None = None,
    status_filter: DLQStatus | None = None,
    limit: int = 50,
    offset: int = 0,
    dlq: DeadLetterQueue = Depends(get_dlq),
) -> list[DLQEntry]:
    if limit < 1 or limit > 200:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="limit must be between 1 and 200.")
    return await dlq.list(category=category, status=status_filter, limit=limit, offset=offset)


@router.get("/stats", response_model=DLQStats, dependencies=[_require_admin])
async def dlq_stats(dlq: DeadLetterQueue = Depends(get_dlq)) -> DLQStats:
    return await dlq.stats()


@router.get("/{entry_id}", response_model=DLQEntry, dependencies=[_require_admin])
async def get_dlq_entry(entry_id: str, dlq: DeadLetterQueue = Depends(get_dlq)) -> DLQEntry:
    try:
        return await dlq.get(entry_id)
    except DLQEntryNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"No DLQ entry '{entry_id}'.") from exc


@router.patch("/{entry_id}", response_model=DLQEntry, dependencies=[_require_admin])
async def edit_dlq_entry_payload(
    entry_id: str,
    update: DLQEntryUpdate,
    dlq: DeadLetterQueue = Depends(get_dlq),
) -> DLQEntry:
    """A compliance engineer corrects the failing parameters -- e.g. a
    malformed source URL, an override flag that should have been set --
    before requeueing. See app.resilience.models.DLQEntryUpdate's
    docstring for why this replaces the whole payload rather than merging
    a partial one."""
    try:
        return await dlq.update_payload(entry_id, update.payload)
    except DLQEntryNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"No DLQ entry '{entry_id}'.") from exc
    except DLQInvalidTransitionError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.post("/{entry_id}/requeue", response_model=DLQRequeueResponse, dependencies=[_require_admin])
async def requeue_dlq_entry(
    entry_id: str,
    dlq: DeadLetterQueue = Depends(get_dlq),
    principal: Principal = Depends(require_roles(Role.SYSTEM_ADMIN)),
) -> DLQRequeueResponse:
    """Re-dispatches `entry.task_name` (whatever it currently is -- edit
    it first via PATCH if the failing parameters need correcting) with
    `entry.payload` as keyword arguments, via `celery_app.send_task` --
    dispatch by name, not by importing the task function, so this one
    endpoint can requeue any DLQ-routed task type without a per-category
    if/elif chain to keep in sync as new task modules are added (see
    app.compiler.tasks / app.agents.tasks / app.vectorstore.tasks /
    app.ingestion.tasks -- every one of them structures its DLQ payload as
    a kwargs-shaped dict for exactly this reason)."""
    try:
        entry = await dlq.get(entry_id)
    except DLQEntryNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"No DLQ entry '{entry_id}'.") from exc

    async_result = celery_app.send_task(entry.task_name, kwargs=entry.payload)

    try:
        updated = await dlq.mark_requeued(entry_id, requeued_by=principal.subject, requeued_task_id=async_result.id)
    except DLQInvalidTransitionError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    logger.info(
        "DLQ entry '%s' (task=%s) requeued by '%s' as Celery task '%s'.",
        entry_id, entry.task_name, principal.subject, async_result.id,
    )
    return DLQRequeueResponse(entry=updated, dispatched_task_id=async_result.id)


@router.post("/{entry_id}/discard", response_model=DLQEntry, dependencies=[_require_admin])
async def discard_dlq_entry(
    entry_id: str,
    resolution: DLQResolveRequest,
    dlq: DeadLetterQueue = Depends(get_dlq),
    principal: Principal = Depends(require_roles(Role.SYSTEM_ADMIN)),
) -> DLQEntry:
    """Marks an entry DISCARDED without reprocessing it -- e.g. a
    duplicate, a spam/junk PDF the RSS feed picked up, a test document.
    Distinct from `/resolve`: discard means "not worth fixing", resolve
    means "confirmed fixed"."""
    try:
        return await dlq.discard(entry_id, discarded_by=principal.subject, notes=resolution.notes)
    except DLQEntryNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"No DLQ entry '{entry_id}'.") from exc
    except DLQInvalidTransitionError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.post("/{entry_id}/resolve", response_model=DLQEntry, dependencies=[_require_admin])
async def resolve_dlq_entry(
    entry_id: str,
    resolution: DLQResolveRequest,
    dlq: DeadLetterQueue = Depends(get_dlq),
    principal: Principal = Depends(require_roles(Role.SYSTEM_ADMIN)),
) -> DLQEntry:
    """Marks an entry RESOLVED -- e.g. a requeued item that has now
    succeeded and no longer needs tracking here, or a failure fixed
    out-of-band. This endpoint does not verify the underlying task
    actually succeeded; it records a human's judgment that this item is
    closed."""
    try:
        return await dlq.mark_resolved(entry_id, resolved_by=principal.subject, notes=resolution.notes)
    except DLQEntryNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"No DLQ entry '{entry_id}'.") from exc
