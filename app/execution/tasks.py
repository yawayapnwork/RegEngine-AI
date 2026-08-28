"""Celery tasks bridging legacy SFTP batch files and DB CDC events onto the
same `Evaluator` used by the synchronous REST endpoint, plus outbound
webhook delivery with retry for OMS/RMS/broker notification.

Celery tasks execute outside an asyncio event loop, but `Evaluator` and its
collaborators are async (they call OPA and Redis over the network). Each
task opens one event loop via `asyncio.run` for its whole unit of work
(one batch, one CDC event) rather than one per transaction, so a batch of
N transactions pays the loop-startup cost once.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging

import redis as sync_redis

from app.config import get_settings
from app.execution.celery_app import celery_app
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
    SourceChannel,
    TransactionPayload,
    WebhookEvent,
)
from app.execution.opa_engine import OPAEngine
from app.execution.policy_registry import PolicyRegistry
from app.execution.webhook_client import send_webhook_sync

logger = logging.getLogger(__name__)

_BATCH_CONCURRENCY = 16  # bounded so one large SFTP file can't flood OPA/Redis


def _sync_redis() -> sync_redis.Redis:
    return sync_redis.from_url(get_settings().redis_url, decode_responses=True)


def _batch_key(batch_id: str) -> str:
    return f"{get_settings().policy_registry_key}:batch:{batch_id}"


def _build_evaluator() -> tuple[Evaluator, "aioredis.Redis"]:
    import redis.asyncio as aioredis

    settings = get_settings()
    client = aioredis.from_url(settings.redis_url, decode_responses=True)
    evaluator = Evaluator(
        opa_engine=OPAEngine(settings.opa_server_url, settings.opa_request_timeout_seconds),
        policy_registry=PolicyRegistry(client, settings.policy_registry_key),
        hitl_queue=HITLQueue(client, settings.hitl_key_prefix),
    )
    return evaluator, client


async def _evaluate_batch(transactions: list[TransactionPayload]) -> list[EvaluationResult]:
    evaluator, client = _build_evaluator()
    semaphore = asyncio.Semaphore(_BATCH_CONCURRENCY)

    async def _bounded(t: TransactionPayload) -> EvaluationResult:
        async with semaphore:
            return await evaluator.evaluate_transaction(t)

    try:
        return list(await asyncio.gather(*(_bounded(t) for t in transactions)))
    finally:
        await client.aclose()


@celery_app.task(name="app.execution.tasks.process_batch_task", bind=True, max_retries=2)
def process_batch_task(self, batch_request: dict) -> dict:
    """Processes a legacy SFTP-delivered batch of transactions end to end
    and, if configured, POSTs a `BatchJobResult` summary webhook when done."""
    request = BatchIngestRequest.model_validate(batch_request)
    redis_client = _sync_redis()
    result = BatchJobResult(batch_id=request.batch_id, status=BatchJobStatus.RUNNING, started_at=dt.datetime.utcnow())
    redis_client.set(_batch_key(request.batch_id), result.model_dump_json())

    try:
        evaluations = asyncio.run(_evaluate_batch(request.transactions))
        result.results = evaluations
        result.total = len(evaluations)
        result.allowed = sum(1 for e in evaluations if e.decision == Decision.ALLOW)
        result.denied = sum(1 for e in evaluations if e.decision == Decision.DENY)
        result.flagged = sum(1 for e in evaluations if e.decision == Decision.FLAGGED)
        result.status = BatchJobStatus.COMPLETED
    except Exception as exc:  # noqa: BLE001 - report failure, don't crash the worker
        logger.exception("Batch '%s' failed.", request.batch_id)
        result.status = BatchJobStatus.FAILED
        result.error = str(exc)
    finally:
        result.completed_at = dt.datetime.utcnow()
        redis_client.set(_batch_key(request.batch_id), result.model_dump_json())

    if request.result_webhook_url:
        event = WebhookEvent(
            event_type="batch.completed",
            transaction_id=request.batch_id,
            decision=Decision.ALLOW if result.status == BatchJobStatus.COMPLETED else Decision.FLAGGED,
            payload=result.model_dump(mode="json"),
        )
        dispatch_webhook_task.delay(request.result_webhook_url, event.model_dump(mode="json"))

    return result.model_dump(mode="json")


def get_batch_result(batch_id: str) -> BatchJobResult | None:
    raw = _sync_redis().get(_batch_key(batch_id))
    return BatchJobResult.model_validate_json(raw) if raw else None


def _cdc_row_to_transaction(event: CDCEvent) -> TransactionPayload:
    """Maps a legacy `transactions` table row (Debezium/Kafka Connect `after`
    image, or a direct DB-trigger HTTP call carrying the same shape) onto
    the evaluator's TransactionPayload contract. Column names below match
    the fact-field slugging convention in `app.compiler.naming` so a fact
    column like `upfront_margin_pct` requires no remapping; anything else
    is passed through as-is under `facts`."""
    row = event.after or {}
    facts = {k: v for k, v in row.items() if k not in ("transaction_id", "entity_type", "broker_id")}
    return TransactionPayload(
        transaction_id=str(row.get("transaction_id", row.get("id", ""))),
        entity_type=row.get("entity_type", "Stockbroker"),
        facts=facts,
        source_channel=SourceChannel.DB_CDC,
        broker_id=row.get("broker_id"),
    )


@celery_app.task(name="app.execution.tasks.process_cdc_event_task", bind=True, max_retries=3, default_retry_delay=5)
def process_cdc_event_task(self, cdc_event: dict, oms_webhook_url: str | None = None) -> dict | None:
    """Evaluates one CDC-captured row change. DELETE events are recorded
    but not evaluated — there is no post-image to check facts against."""
    event = CDCEvent.model_validate(cdc_event)
    if event.operation == CDCOperation.DELETE:
        logger.info("CDC delete on %s ignored for evaluation.", event.source_table)
        return None

    transaction = _cdc_row_to_transaction(event)

    async def _run() -> EvaluationResult:
        evaluator, client = _build_evaluator()
        try:
            return await evaluator.evaluate_transaction(transaction)
        finally:
            await client.aclose()

    result = asyncio.run(_run())

    if oms_webhook_url:
        webhook_event = WebhookEvent(
            event_type="cdc.transaction.evaluated",
            transaction_id=result.transaction_id,
            decision=result.decision,
            payload=result.model_dump(mode="json"),
        )
        dispatch_webhook_task.delay(oms_webhook_url, webhook_event.model_dump(mode="json"))

    return result.model_dump(mode="json")


@celery_app.task(
    name="app.execution.tasks.dispatch_webhook_task",
    bind=True,
    max_retries=5,
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=60,
    retry_jitter=True,
)
def dispatch_webhook_task(self, url: str, event: dict) -> None:
    """Delivers a WebhookEvent to an OMS/RMS/broker callback URL. Retries
    with exponential backoff + jitter on any failure (timeout, non-2xx,
    connection refused) up to `max_retries`; Celery's `acks_late` config
    means an undelivered notification survives a worker crash and is
    retried by another worker rather than silently lost."""
    settings = get_settings()
    webhook_event = WebhookEvent.model_validate(event)
    send_webhook_sync(url, webhook_event, settings.webhook_hmac_secret, settings.webhook_timeout_seconds)
