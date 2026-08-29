"""Requirement 3's dashboard-facing API: list/inspect drafted and
submitted grievances, confirm a DRAFTED grievance for submission (the
default, HITL-gated path -- see settings.grievance_escalation_auto_submit_enabled),
and force an immediate SCORES status poll rather than waiting for the
next periodic sweep. Every mutating route requires the Compliance_Officer
role, the same separation-of-duties rationale as every other HITL-facing
route in this codebase (app.api.hitl_review_routes' module docstring):
filing or confirming a regulatory grievance against a broker is a
compliance judgment call, not an infrastructure operation.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status

from app.config import Settings, get_settings
from app.execution.dependencies import get_redis_pool
from app.grievance_escalation.queue import GrievanceQueue, GrievanceRecord
from app.security.dependencies import require_roles
from app.security.models import Principal, Role

router = APIRouter(prefix="/v1/grievances", tags=["grievance-escalation"])

_require_read_role = Depends(require_roles(Role.COMPLIANCE_OFFICER, Role.SYSTEM_ADMIN))
_require_officer_role = Depends(require_roles(Role.COMPLIANCE_OFFICER))


def _get_queue(settings: Settings = Depends(get_settings)) -> GrievanceQueue:
    return GrievanceQueue(get_redis_pool(), settings.grievance_escalation_key_prefix)


@router.get("/", response_model=list[GrievanceRecord], dependencies=[_require_read_role])
async def list_grievances(queue: GrievanceQueue = Depends(_get_queue)) -> list[GrievanceRecord]:
    """Every DRAFTED-or-later grievance awaiting either compliance-officer
    confirmation or submission -- for a full history including RESOLVED/
    REJECTED, see `list_open` isn't the right call; a real deployment
    would extend `GrievanceQueue` with an archival index rather than
    keep resolved records in the pending sets forever (out of scope for
    this initial implementation, which favors the pending/open sets'
    existing shape over adding a third storage index only this one
    listing route would use)."""
    return await queue.list_pending_submission()


@router.get("/open", response_model=list[GrievanceRecord], dependencies=[_require_read_role])
async def list_open_grievances(queue: GrievanceQueue = Depends(_get_queue)) -> list[GrievanceRecord]:
    """Submitted grievances awaiting SCORES resolution -- Requirement 3's
    "resolution timelines" view: each record's `response_due_at`/
    `is_overdue` fields are exactly what the dashboard renders as a
    countdown/overdue flag."""
    return await queue.list_open()


@router.get("/{grievance_id}", response_model=GrievanceRecord, dependencies=[_require_read_role])
async def get_grievance(grievance_id: str, queue: GrievanceQueue = Depends(_get_queue)) -> GrievanceRecord:
    record = await queue.get(grievance_id)
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"No grievance '{grievance_id}'.")
    return record


@router.post("/{grievance_id}/confirm", response_model=GrievanceRecord)
async def confirm_grievance(
    grievance_id: str,
    queue: GrievanceQueue = Depends(_get_queue),
    principal: Principal = Depends(require_roles(Role.COMPLIANCE_OFFICER)),
) -> GrievanceRecord:
    """A compliance officer reviews a DRAFTED grievance's evidence
    package and confirms it should actually be filed with SEBI SCORES
    -- the default gate every automatically-drafted grievance passes
    through unless `settings.grievance_escalation_auto_submit_enabled`
    is set. Confirming enqueues it for the next submission sweep; it
    does not submit synchronously (submission is Celery's job, exactly
    like every other outbound-integration write in this codebase)."""
    del principal  # audit trail lives in the record's own last_error/status transitions today; a fuller implementation would additionally log who confirmed
    try:
        return await queue.confirm_for_submission(grievance_id)
    except KeyError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.post("/{grievance_id}/refresh-status", response_model=GrievanceRecord, dependencies=[_require_officer_role])
async def refresh_grievance_status(grievance_id: str, queue: GrievanceQueue = Depends(_get_queue)) -> GrievanceRecord:
    """Forces an immediate SCORES status poll for one grievance rather
    than waiting for the next `poll_pending_grievances_task` sweep --
    useful when a compliance officer knows (e.g. from an out-of-band
    SEBI communication) that a status change is expected. Runs the same
    poll logic Celery's periodic sweep uses, synchronously, so the
    response reflects the freshly-polled state immediately."""
    from app.grievance_escalation.tasks import poll_grievance_status_now  # deferred: avoids a Celery app import for every route module load

    record = await queue.get(grievance_id)
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"No grievance '{grievance_id}'.")
    if record.scores_reference_number is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"Grievance '{grievance_id}' has not been submitted to SCORES yet; nothing to poll.")

    await poll_grievance_status_now(grievance_id)
    return await queue.get(grievance_id)
