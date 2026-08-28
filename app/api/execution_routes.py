"""HTTP surface for the transaction execution service: instant policy
evaluation, legacy SFTP/CDC batch ingestion, and HITL case management."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status

from app.execution.dependencies import get_evaluator, get_hitl_queue
from app.execution.evaluator import Evaluator
from app.execution.hitl_queue import HITLQueue
from app.execution.models import (
    BatchIngestRequest,
    BatchJobResult,
    BatchJobStatus,
    CDCEvent,
    CDCOperation,
    Decision,
    EvaluationResult,
    HITLCase,
    HITLResolutionRequest,
    HITLStatus,
    TransactionPayload,
    WebhookEvent,
)
from app.execution.opa_engine import OPAEngineError
from app.execution.tasks import (
    dispatch_webhook_task,
    get_batch_result,
    process_batch_task,
    process_cdc_event_task,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/execution")


# --- Requirement 1: synchronous, instant transaction evaluation ---


@router.post("/transactions/evaluate", response_model=EvaluationResult, status_code=status.HTTP_200_OK)
async def evaluate_transaction(
    transaction: TransactionPayload,
    evaluator: Evaluator = Depends(get_evaluator),
) -> EvaluationResult:
    """Evaluate a single broker-submitted transaction against compiled Rego
    policies via the embedded OPA engine and return allow/deny/flagged
    immediately. FLAGGED responses carry a `hitl_case_id`; the final
    decision is delivered later to `transaction.callback_url`, if set, once
    a human resolves it (see POST /hitl/cases/{case_id}/resolve)."""
    try:
        return await evaluator.evaluate_transaction(transaction)
    except OPAEngineError as exc:
        logger.error("OPA engine unavailable evaluating transaction '%s': %s", transaction.transaction_id, exc)
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Policy engine unavailable.") from exc


# --- Requirement 2: async batch handling for legacy SFTP / CDC pipelines ---


@router.post("/batches", status_code=status.HTTP_202_ACCEPTED)
async def submit_batch(request: BatchIngestRequest) -> dict[str, str]:
    """Enqueue a batch parsed from a legacy SFTP landing-zone file (or any
    caller with an in-memory batch) for asynchronous processing on the
    `regengine_batch` Celery/Redis queue. Returns immediately with the
    batch_id; poll GET /batches/{batch_id} or supply result_webhook_url."""
    process_batch_task.delay(request.model_dump(mode="json"))
    return {"batch_id": request.batch_id, "status": BatchJobStatus.QUEUED.value}


@router.get("/batches/{batch_id}", response_model=BatchJobResult)
async def get_batch_status(batch_id: str) -> BatchJobResult:
    result = get_batch_result(batch_id)
    if result is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"No batch job '{batch_id}'.")
    return result


@router.post("/cdc/events", status_code=status.HTTP_202_ACCEPTED)
async def receive_cdc_event(event: CDCEvent, oms_webhook_url: str | None = None) -> dict[str, str]:
    """Receiver for a Debezium/Kafka-Connect HTTP sink (or a direct DB
    trigger) capturing INSERT/UPDATE on the legacy `transactions` table.
    Enqueues evaluation on the `regengine_cdc` queue and returns
    immediately so the DB trigger/connector is never blocked on policy
    evaluation latency."""
    if event.operation != CDCOperation.DELETE and not event.after:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="CDC insert/update event must include 'after'.")

    process_cdc_event_task.delay(event.model_dump(mode="json"), oms_webhook_url)
    transaction_id = (event.after or {}).get("transaction_id", (event.after or {}).get("id", "unknown"))
    return {"status": "queued", "transaction_id": str(transaction_id)}


# --- Requirement 3: HITL fallback for ambiguous / flagged decisions ---


@router.get("/hitl/cases", response_model=list[HITLCase])
async def list_hitl_cases(hitl_queue: HITLQueue = Depends(get_hitl_queue)) -> list[HITLCase]:
    return await hitl_queue.list_pending()


@router.get("/hitl/cases/{case_id}", response_model=HITLCase)
async def get_hitl_case(case_id: str, hitl_queue: HITLQueue = Depends(get_hitl_queue)) -> HITLCase:
    case = await hitl_queue.get(case_id)
    if case is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"No HITL case '{case_id}'.")
    return case


@router.post("/hitl/cases/{case_id}/resolve", response_model=HITLCase)
async def resolve_hitl_case(
    case_id: str,
    resolution: HITLResolutionRequest,
    hitl_queue: HITLQueue = Depends(get_hitl_queue),
) -> HITLCase:
    """A compliance reviewer resolves an ambiguous transaction as ALLOW or
    DENY. If the original transaction carried a `callback_url`, the final
    decision is dispatched there asynchronously (with retry) via the
    `regengine_webhooks` queue, closing the fallback loop from requirement 3."""
    if resolution.decision not in (Decision.ALLOW, Decision.DENY):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Resolution decision must be 'allow' or 'deny'.")

    status_map = {Decision.ALLOW: HITLStatus.APPROVED, Decision.DENY: HITLStatus.REJECTED}
    try:
        case = await hitl_queue.resolve(case_id, status_map[resolution.decision], resolution.resolved_by, resolution.notes)
    except KeyError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    if case.transaction.callback_url:
        event = WebhookEvent(
            event_type="hitl.case.resolved",
            transaction_id=case.transaction.transaction_id,
            decision=resolution.decision,
            payload={"case_id": case.case_id, "resolved_by": resolution.resolved_by, "notes": resolution.notes},
        )
        dispatch_webhook_task.delay(case.transaction.callback_url, event.model_dump(mode="json"))

    return case
