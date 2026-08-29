"""Celery application for asynchronous batch/CDC/webhook processing.

Kept on Redis (not RabbitMQ) as both broker and result backend so the
execution service has a single infrastructure dependency shared with the
policy registry and HITL queue. Separate queues per workload
(`regengine_batch`, `regengine_cdc`, `regengine_webhooks`) let ops scale
SFTP batch workers independently from latency-sensitive webhook delivery
workers, and stop either from starving the other.

Run with, e.g.:
    celery -A app.execution.celery_app worker \
        -Q regengine_batch,regengine_cdc,regengine_webhooks,regengine_ingestion,regengine_agents,regengine_compiler,regengine_vectorstore \
        -l info
Or scale each workload's worker pool independently -- `-Q regengine_agents`
alone for a pool sized to Anthropic's rate limit, separate from a
CPU-bound `-Q regengine_compiler` pool.
"""
from __future__ import annotations

from celery import Celery

from app.config import get_settings

settings = get_settings()

celery_app = Celery(
    "regengine_execution",
    broker=settings.redis_url,
    backend=settings.redis_url,
)

celery_app.conf.update(
    task_default_queue=settings.celery_task_default_queue,
    task_routes={
        "app.execution.tasks.process_batch_task": {"queue": settings.celery_batch_queue},
        "app.execution.tasks.process_cdc_event_task": {"queue": settings.celery_cdc_queue},
        "app.execution.tasks.dispatch_webhook_task": {"queue": settings.celery_webhook_queue},
        "app.ingestion.tasks.poll_sebi_sources_task": {"queue": settings.celery_ingestion_queue},
        "app.ingestion.tasks.process_discovered_document_task": {"queue": settings.celery_ingestion_queue},
        "app.agents.tasks.extract_and_audit_clause_task": {"queue": settings.celery_agents_queue},
        "app.compiler.tasks.compile_audited_rule_task": {"queue": settings.celery_compiler_queue},
        "app.vectorstore.tasks.index_chunks_task": {"queue": settings.celery_vectorstore_queue},
        "app.llm_ops.tasks.purge_expired_cache_entries_task": {"queue": settings.celery_llm_ops_queue},
        "app.incident.tasks.process_escalation_stage_task": {"queue": settings.celery_incidents_queue},
        "app.incident.tasks.sweep_overdue_escalations_task": {"queue": settings.celery_incidents_queue},
    },
    task_acks_late=True,           # redeliver a batch/CDC job if the worker dies mid-processing
    worker_prefetch_multiplier=1,  # avoid one worker hoarding a large SFTP batch queue
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    result_expires=86400,
    beat_schedule={
        "poll-sebi-sources": {
            "task": "app.ingestion.tasks.poll_sebi_sources_task",
            "schedule": settings.ingestion_poll_interval_seconds,
        },
        "purge-expired-llm-cache-entries": {
            "task": "app.llm_ops.tasks.purge_expired_cache_entries_task",
            "schedule": settings.llm_cache_purge_interval_seconds,
        },
        "sweep-overdue-escalations": {
            "task": "app.incident.tasks.sweep_overdue_escalations_task",
            "schedule": settings.incident_escalation_sweep_interval_seconds,
        },
    },
)

celery_app.autodiscover_tasks(["app.execution", "app.ingestion", "app.agents", "app.compiler", "app.vectorstore", "app.llm_ops", "app.incident"])
