"""Celery tasks for continuous SEBI regulatory ingestion.

`poll_sebi_sources_task` is beat-scheduled (see `app.execution.celery_app`)
to run every `ingestion_poll_interval_seconds`. Each run:
  1. Polls every configured RSS feed and HTML listing page for candidate
     circular/notification links (`app.ingestion.feed_monitor`).
  2. Downloads each candidate's PDF bytes and classifies it as new,
     content-amended, or unchanged against the last-seen hash
     (`app.ingestion.change_detector`).
  3. For anything new or amended, enqueues `process_discovered_document_task`
     — a separate task/queue so one slow or failing PDF can't block
     discovery of the rest of the batch, and so per-document retries don't
     re-run the whole poll cycle.

`process_manual_upload_task` is the equivalent path for a user-uploaded PDF
(`POST /v1/ingestion/uploads`, `app.api.ingestion_routes`) instead of an
auto-discovered one: the web process stages the file in object storage
(`app.storage.object_store`) and creates an `IngestionUploadJob` row before
enqueuing this task, since the file itself is too large to pass as a task
argument. This task fetches it by key, runs the same parse -> index pipeline,
and updates that row so the frontend can poll it instead of blocking on one
long-lived HTTP request.

Celery tasks run outside an asyncio event loop; each task opens exactly one
event loop for its unit of work via `asyncio.run`.

Resiliency (app.resilience): both tasks classify a failure via
`is_transient(exc)` before deciding what to do with it --
  - transient (network/connectivity)  -> `self.retry()` with Celery's
    native exponential-backoff-with-jitter (`retry_backoff`/`retry_jitter`
    task options), bounded by `settings.retry_max_attempts_network`.
  - not transient (a genuinely unparseable PDF, a parser/logic bug, a
    permanently broken feed URL) -> routed straight to the DLQ
    (`app.resilience.dead_letter_queue`), zero retries wasted.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import get_settings
from app.db.models import IngestionUploadJob
from app.execution.celery_app import celery_app
from app.ingestion.change_detector import SeenDocumentStore
from app.ingestion.exceptions import DocumentDownloadError, IngestionError, RobotsDisallowedError
from app.ingestion.feed_monitor import discover_all
from app.ingestion.http_client import SebiHttpClient
from app.ingestion.models import ChangeKind, DiscoveredDocument, IngestionRunResult
from app.ingestion.regulator_sources import count_configured_sources
from app.ingestion.pipeline_trigger import download_document, process_discovered_document
from app.parsing.exceptions import (
    ChunkingError,
    EmbeddingError,
    IndexingError,
    ScannedDocumentError,
    UnsupportedFileError,
)
from app.resilience.celery_helpers import route_to_dlq_sync
from app.resilience.models import FailureCategory
from app.resilience.retry_policy import is_transient
from app.services.pipeline import parse_pdf_bytes
from app.storage.object_store import download_bytes
from app.vectorstore.qdrant_store import index_chunks

logger = logging.getLogger(__name__)


async def _run_poll_cycle() -> IngestionRunResult:
    settings = get_settings()
    started_at = dt.datetime.now(dt.timezone.utc)

    import redis.asyncio as aioredis

    redis_client = aioredis.from_url(settings.redis_url, decode_responses=True)
    seen_store = SeenDocumentStore(redis_client, settings.ingestion_state_key_prefix)

    documents_new = 0
    documents_amended = 0
    documents_failed = 0
    errors: list[str] = []

    try:
        async with SebiHttpClient(settings) as client:
            discovered_documents = await discover_all(client, settings)
            logger.info("Poll cycle discovered %d candidate documents", len(discovered_documents))

            for discovered in discovered_documents:
                try:
                    content = await download_document(client, discovered)
                except (DocumentDownloadError, RobotsDisallowedError) as exc:
                    documents_failed += 1
                    errors.append(f"{discovered.source_url}: {exc}")
                    logger.warning("Skipping %s: %s", discovered.source_url, exc)
                    continue

                change_kind, content_sha256 = await seen_store.classify(discovered, content)
                if change_kind == ChangeKind.UNCHANGED:
                    continue

                # Dispatch to a dedicated queue/task rather than processing
                # inline: keeps one slow PDF from stalling discovery of the
                # rest of this cycle's documents, and gives per-document
                # retry semantics independent of the poll schedule.
                process_discovered_document_task.delay(
                    discovered.model_dump(mode="json"),
                    change_kind.value,
                    content.hex(),
                    content_sha256,
                )

                if change_kind == ChangeKind.NEW_DOCUMENT:
                    documents_new += 1
                else:
                    documents_amended += 1

        return IngestionRunResult(
            started_at=started_at,
            finished_at=dt.datetime.now(dt.timezone.utc),
            sources_polled=count_configured_sources(settings),
            documents_discovered=len(discovered_documents),
            documents_new=documents_new,
            documents_amended=documents_amended,
            documents_failed=documents_failed,
            errors=errors,
        )
    finally:
        await redis_client.aclose()


@celery_app.task(
    name="app.ingestion.tasks.poll_sebi_sources_task",
    bind=True,
    max_retries=None,  # enforced manually below via settings.retry_max_attempts_network; see the retry() call
    retry_backoff=True,
    retry_backoff_max=300,
    retry_jitter=True,
)
def poll_sebi_sources_task(self) -> dict:
    """Beat-scheduled entry point: one full poll cycle across all sources."""
    settings = get_settings()
    try:
        result = asyncio.run(_run_poll_cycle())
    except Exception as exc:  # noqa: BLE001 - classify every failure explicitly below (transient vs. DLQ)
        attempt = self.request.retries + 1
        if is_transient(exc) and attempt < settings.retry_max_attempts_network:
            logger.warning("Transient poll-cycle failure (attempt %d/%d): %s -- retrying with backoff.", attempt, settings.retry_max_attempts_network, exc)
            raise self.retry(exc=exc) from exc

        logger.error("Poll cycle failed after %d attempt(s), not retrying further: %s", attempt, exc)
        route_to_dlq_sync(
            category=FailureCategory.RSS_POLLING,
            task_name="app.ingestion.tasks.poll_sebi_sources_task",
            payload={},  # this task takes no arguments -- requeue is simply "run it again"
            exc=exc,
            original_task_id=self.request.id,
            attempt_count=attempt,
        )
        raise

    logger.info(
        "Poll cycle complete: %d discovered, %d new, %d amended, %d failed",
        result.documents_discovered, result.documents_new, result.documents_amended, result.documents_failed,
    )
    return result.model_dump(mode="json")


async def _process_one(discovered: DiscoveredDocument, change_kind: ChangeKind, content: bytes, content_sha256: str) -> dict:
    settings = get_settings()

    import redis.asyncio as aioredis

    redis_client = aioredis.from_url(settings.redis_url, decode_responses=True)
    seen_store = SeenDocumentStore(redis_client, settings.ingestion_state_key_prefix)
    try:
        ingested = await process_discovered_document(discovered, change_kind, content, content_sha256, settings)
        # Only mark as seen once the Phase 1 pipeline has actually succeeded,
        # so a crash mid-processing surfaces as a re-poll next cycle rather
        # than a silently dropped update.
        await seen_store.record(discovered.source_url, content_sha256)
        return ingested.model_dump(mode="json")
    finally:
        await redis_client.aclose()


# Exceptions that mean "this exact PDF will never parse, no matter how
# many times we try" -- routed to the DLQ immediately, retry attempts
# never spent on them. ExtractionBackendError/ParseTimeoutError are
# deliberately NOT here: an extraction backend being briefly unreachable,
# or a slow document tripping the timeout under load, both plausibly
# succeed on a retry -- classified via is_transient() instead, below.
#
# ScannedDocumentError IS explicitly permanent even though it subclasses
# ExtractionBackendError: a scanned/image-only PDF has no text layer to
# find regardless of how many times the same backend re-reads it, so it
# belongs with UnsupportedFileError/ChunkingError, not with the
# is_transient()-classified network-flakiness case below. Listed before
# the parent type has any chance to matter here since isinstance() against
# this tuple checks ScannedDocumentError directly.
_PERMANENT_PARSING_ERRORS = (UnsupportedFileError, ChunkingError, ScannedDocumentError)


@celery_app.task(
    name="app.ingestion.tasks.process_discovered_document_task",
    bind=True,
    max_retries=None,  # enforced manually below; see the retry() call
    retry_backoff=True,
    retry_backoff_max=300,
    retry_jitter=True,
)
def process_discovered_document_task(self, discovered_dict: dict, change_kind: str, content_hex: str, content_sha256: str) -> dict:
    settings = get_settings()
    discovered = DiscoveredDocument.model_validate(discovered_dict)
    content = bytes.fromhex(content_hex)
    payload = {
        "discovered_dict": discovered_dict,
        "change_kind": change_kind,
        "content_hex": content_hex,
        "content_sha256": content_sha256,
    }

    try:
        return asyncio.run(_process_one(discovered, ChangeKind(change_kind), content, content_sha256))
    except Exception as exc:  # noqa: BLE001 - classify every failure explicitly below (permanent / transient / DLQ)
        attempt = self.request.retries + 1

        if isinstance(exc, _PERMANENT_PARSING_ERRORS):
            logger.error("Unparseable PDF for %s (attempt %d, not retrying): %s", discovered.source_url, attempt, exc)
            route_to_dlq_sync(
                category=FailureCategory.PDF_PARSING,
                task_name="app.ingestion.tasks.process_discovered_document_task",
                payload=payload,
                exc=exc,
                original_task_id=self.request.id,
                attempt_count=attempt,
            )
            raise

        # Everything else (vector-DB indexing failures inside
        # process_discovered_document, a transiently-unreachable
        # extraction backend, an unexpected bug) is classified by
        # transience rather than exception type -- category is chosen
        # from what actually raised it so a compliance engineer sees the
        # right DLQ bucket regardless.
        max_attempts = settings.retry_max_attempts_network  # only reached below when is_transient(exc) is True
        if is_transient(exc) and attempt < max_attempts:
            logger.warning(
                "Transient failure processing %s (attempt %d/%d): %s -- retrying with backoff.",
                discovered.source_url, attempt, max_attempts, exc,
            )
            raise self.retry(exc=exc) from exc

        category = FailureCategory.VECTOR_INGESTION if isinstance(exc, (EmbeddingError, IndexingError)) else FailureCategory.PDF_PARSING
        logger.error("Ingestion pipeline failed for %s after %d attempt(s), not retrying further: %s", discovered.source_url, attempt, exc)
        route_to_dlq_sync(
            category=category,
            task_name="app.ingestion.tasks.process_discovered_document_task",
            payload=payload,
            exc=exc,
            original_task_id=self.request.id,
            attempt_count=attempt,
        )
        raise


@asynccontextmanager
async def _short_lived_session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """A fresh engine (and its connection pool) scoped to exactly one
    `asyncio.run()` call, disposed before that call returns -- deliberately
    NOT `app.db.session.get_engine`/`get_session_factory`, which are
    process-wide `@lru_cache`'d singletons meant for a single long-lived
    event loop (uvicorn's). Each Celery task invocation gets its own fresh
    event loop via `asyncio.run()`; a pooled asyncpg connection created in
    one such loop is unusable (and crashes with "Event loop is closed") the
    moment a *different* asyncio.run() call -- a different task, or even a
    second asyncio.run() within the same task -- tries to reuse it from the
    cached engine. A short-lived, per-call engine sidesteps that entirely."""
    settings = get_settings()
    engine = create_async_engine(settings.database_url)
    try:
        yield async_sessionmaker(bind=engine, expire_on_commit=False)
    finally:
        await engine.dispose()


async def _set_upload_job_status(job_id: str, **fields: object) -> None:
    async with _short_lived_session_factory() as session_factory, session_factory() as session:
        result = await session.execute(select(IngestionUploadJob).where(IngestionUploadJob.job_id == job_id))
        job = result.scalar_one_or_none()
        if job is None:
            logger.error("IngestionUploadJob '%s' vanished mid-processing -- nothing to update.", job_id)
            return
        for key, value in fields.items():
            setattr(job, key, value)
        await session.commit()


async def _process_manual_upload(job_id: str) -> int:
    """Fetches the job's staged PDF from object storage and runs it through
    the same parse -> index pipeline `process_discovered_document` uses.
    Returns the number of clause chunks indexed. Raises on failure -- the
    caller (the Celery task below) is responsible for retry/DLQ
    classification and for recording the terminal `failed` status."""
    settings = get_settings()

    async with _short_lived_session_factory() as session_factory, session_factory() as session:
        result = await session.execute(select(IngestionUploadJob).where(IngestionUploadJob.job_id == job_id))
        job = result.scalar_one_or_none()
        if job is None:
            raise IngestionError(f"IngestionUploadJob '{job_id}' does not exist.")
        job.status = "processing"
        await session.commit()
        filename, object_key = job.filename, job.object_key

    content = await download_bytes(object_key)
    parsed = await parse_pdf_bytes(content, filename=filename, settings=settings)
    await index_chunks(parsed.chunks, settings, recreate_collection=False)
    return len(parsed.chunks)


@celery_app.task(
    name="app.ingestion.tasks.process_manual_upload_task",
    bind=True,
    max_retries=None,  # enforced manually below; see the retry() call
    retry_backoff=True,
    retry_backoff_max=300,
    retry_jitter=True,
)
def process_manual_upload_task(self, job_id: str) -> dict:
    settings = get_settings()
    payload = {"job_id": job_id}

    try:
        chunks_indexed = asyncio.run(_process_manual_upload(job_id))
    except Exception as exc:  # noqa: BLE001 - classify every failure explicitly below (permanent / transient / DLQ)
        attempt = self.request.retries + 1

        if isinstance(exc, _PERMANENT_PARSING_ERRORS):
            logger.error("Unparseable upload '%s' (attempt %d, not retrying): %s", job_id, attempt, exc)
            asyncio.run(_set_upload_job_status(job_id, status="failed", error_message=str(exc)))
            route_to_dlq_sync(
                category=FailureCategory.PDF_PARSING,
                task_name="app.ingestion.tasks.process_manual_upload_task",
                payload=payload,
                exc=exc,
                original_task_id=self.request.id,
                attempt_count=attempt,
            )
            raise

        max_attempts = settings.retry_max_attempts_network
        if is_transient(exc) and attempt < max_attempts:
            logger.warning(
                "Transient failure processing upload '%s' (attempt %d/%d): %s -- retrying with backoff.",
                job_id, attempt, max_attempts, exc,
            )
            raise self.retry(exc=exc) from exc

        category = FailureCategory.VECTOR_INGESTION if isinstance(exc, (EmbeddingError, IndexingError)) else FailureCategory.PDF_PARSING
        logger.error("Upload pipeline failed for '%s' after %d attempt(s), not retrying further: %s", job_id, attempt, exc)
        asyncio.run(_set_upload_job_status(job_id, status="failed", error_message=str(exc)))
        route_to_dlq_sync(
            category=category,
            task_name="app.ingestion.tasks.process_manual_upload_task",
            payload=payload,
            exc=exc,
            original_task_id=self.request.id,
            attempt_count=attempt,
        )
        raise

    logger.info("Upload '%s' complete: %d clause chunks indexed", job_id, chunks_indexed)
    asyncio.run(_set_upload_job_status(job_id, status="completed", chunks_indexed=chunks_indexed))
    return {"job_id": job_id, "chunks_indexed": chunks_indexed}
