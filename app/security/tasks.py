"""Celery task wrapper for app.security.directory_sync_job -- kept as its
own thin module (rather than folding the task decorator into
directory_sync_job.py itself) so that module stays importable/unit-
testable without pulling in Celery at all.
"""
from __future__ import annotations

import logging

from app.execution.celery_app import celery_app
from app.security.directory_sync_job import run_sync_once

logger = logging.getLogger(__name__)


@celery_app.task(name="app.security.tasks.directory_sync_poll_task")
def directory_sync_poll_task() -> dict:
    written = run_sync_once()
    if written:
        logger.info("Directory sync poll updated role overrides for %d active subject(s): %s", len(written), list(written.keys()))
    return written
