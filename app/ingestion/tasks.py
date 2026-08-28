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

Celery tasks run outside an asyncio event loop; each task opens exactly one
event loop for its unit of work via `asyncio.run`.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging

from app.config import get_settings
from app.execution.celery_app import celery_app
from app.ingestion.change_detector import SeenDocumentStore
from app.ingestion.exceptions import DocumentDownloadError, IngestionError, RobotsDisallowedError
from app.ingestion.feed_monitor import discover_all
from app.ingestion.http_client import SebiHttpClient
from app.ingestion.models import ChangeKind, DiscoveredDocument, IngestionRunResult
from app.ingestion.pipeline_trigger import download_document, process_discovered_document
from app.parsing.exceptions import ParsingError

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
            sources_polled=len(settings.sebi_rss_feed_urls) + len(settings.sebi_listing_page_urls),
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
    max_retries=3,
    default_retry_delay=60,
)
def poll_sebi_sources_task(self) -> dict:
    """Beat-scheduled entry point: one full poll cycle across all sources."""
    try:
        result = asyncio.run(_run_poll_cycle())
    except IngestionError as exc:
        logger.error("Poll cycle failed: %s", exc)
        raise self.retry(exc=exc) from exc
    except Exception as exc:  # noqa: BLE001 - never let an unexpected error kill the beat schedule silently
        logger.exception("Unexpected error in poll cycle")
        raise self.retry(exc=exc) from exc

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


@celery_app.task(
    name="app.ingestion.tasks.process_discovered_document_task",
    bind=True,
    max_retries=3,
    default_retry_delay=30,
)
def process_discovered_document_task(self, discovered_dict: dict, change_kind: str, content_hex: str, content_sha256: str) -> dict:
    discovered = DiscoveredDocument.model_validate(discovered_dict)
    content = bytes.fromhex(content_hex)
    try:
        return asyncio.run(_process_one(discovered, ChangeKind(change_kind), content, content_sha256))
    except ParsingError as exc:
        logger.error("Ingestion pipeline failed for %s: %s", discovered.source_url, exc)
        raise self.retry(exc=exc) from exc
    except Exception as exc:  # noqa: BLE001 - retry unexpected failures too, bounded by max_retries
        logger.exception("Unexpected error ingesting %s", discovered.source_url)
        raise self.retry(exc=exc) from exc
