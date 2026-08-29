"""FastAPI surface for the backtesting service.

  POST /v1/backtest/runs
      Submit a candidate policy for backtesting against historical order
      flow. Runs async on the `regengine_backtest` Celery queue (a 90-day
      replay can be tens of thousands of transactions -- this must never
      block the request/response cycle); returns run_id immediately.

  GET  /v1/backtest/runs/{run_id}
      Poll status; once COMPLETED, includes the full BacktestSummary
      (Requirement 2's projected failure rate / false-positive counts).

  GET  /v1/backtest/runs/{run_id}/delta
      Paginated side-by-side old-vs-new delta report (Requirement 3), one
      row per replayed historical transaction.

Restricted to Compliance_Officer / System_Admin: backtesting a candidate
policy before deployment is exactly the pre-production validation step
those two roles are responsible for; a Broker_API_Client has no
legitimate reason to run one.
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.backtest.models import BacktestOutcome, BacktestRun, BacktestRunRequest, BacktestStatus
from app.backtest.tasks import get_outcomes_page, get_run, run_backtest_task
from app.security.dependencies import require_roles
from app.security.models import Role

router = APIRouter(prefix="/v1/backtest", tags=["Backtesting"])

_ALLOWED = require_roles(Role.COMPLIANCE_OFFICER, Role.SYSTEM_ADMIN)


@router.post("/runs", response_model=BacktestRun, status_code=status.HTTP_202_ACCEPTED, dependencies=[Depends(_ALLOWED)])
async def submit_backtest(request: BacktestRunRequest) -> BacktestRun:
    if request.candidate_jsonlogic_ast is None and request.candidate_opa_package is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Exactly one of candidate_jsonlogic_ast or candidate_opa_package must be set.",
        )

    run_id = str(uuid.uuid4())
    run = BacktestRun(run_id=run_id, status=BacktestStatus.PENDING, request=request)
    run_backtest_task.delay(run_id, request.model_dump(mode="json"))
    return run


@router.get("/runs/{run_id}", response_model=BacktestRun, dependencies=[Depends(_ALLOWED)])
async def get_backtest_run(run_id: str) -> BacktestRun:
    run = get_run(run_id)
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"No backtest run '{run_id}'.")
    return run


@router.get("/runs/{run_id}/delta", dependencies=[Depends(_ALLOWED)])
async def get_backtest_delta(
    run_id: str,
    page: int = Query(1, ge=1),
    page_size: int = Query(500, ge=1, le=2000),
) -> dict:
    run = get_run(run_id)
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"No backtest run '{run_id}'.")
    if run.status != BacktestStatus.COMPLETED:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"Backtest run '{run_id}' is not yet completed (status={run.status.value}).")

    entries, total = get_outcomes_page(run_id, page, page_size)
    return {
        "run_id": run_id,
        "page": page,
        "page_size": page_size,
        "total": total,
        "entries": [e.model_dump(mode="json") for e in entries],
    }
