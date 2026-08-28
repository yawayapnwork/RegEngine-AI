"""Celery application for asynchronous batch/CDC/webhook processing.

Kept on Redis (not RabbitMQ) as both broker and result backend so the
execution service has a single infrastructure dependency shared with the
policy registry and HITL queue. Separate queues per workload
(`regengine_batch`, `regengine_cdc`, `regengine_webhooks`) let ops scale
SFTP batch workers independently from latency-sensitive webhook delivery
workers, and stop either from starving the other.

Run with, e.g.:
    celery -A app.execution.celery_app worker -Q regengine_batch,regengine_cdc,regengine_webhooks -l info
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
    },
    task_acks_late=True,           # redeliver a batch/CDC job if the worker dies mid-processing
    worker_prefetch_multiplier=1,  # avoid one worker hoarding a large SFTP batch queue
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    result_expires=86400,
)

celery_app.autodiscover_tasks(["app.execution"])
