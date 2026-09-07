"""Cron-triggered queue drain -- a free substitute for a standing Celery
worker dyno.

Deployments without a dedicated worker process (e.g. Render's free tier,
which has no free Background Worker plan) can point an external scheduler
(cron-job.org, GitHub Actions schedule, etc.) at POST /v1/internal/drain-queue
on an interval. Each hit runs a real `celery worker` -- the same command
docker/entrypoint.sh's `worker` role runs -- as a time-boxed subprocess so it
picks up whatever tasks are currently queued using the actual task routing/
execution code, then is terminated before the request needs to return.

Not a substitute for a standing worker under real load: tasks only advance
once per scheduler tick, and a task still mid-run when the time box expires
is killed (Celery redelivers it to the next tick per the broker's normal
visibility-timeout/ack behavior, so nothing is silently lost -- it's just
retried, possibly from the start for a non-idempotent task).
"""
from __future__ import annotations

import hmac
import logging
import subprocess
import sys

from fastapi import APIRouter, Header, HTTPException, status

from app.config import get_settings

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/internal", tags=["internal"])

_QUEUES = ",".join(
    [
        "regengine_batch",
        "regengine_cdc",
        "regengine_webhooks",
        "regengine_ingestion",
        "regengine_agents",
        "regengine_compiler",
        "regengine_vectorstore",
    ]
)


@router.post("/drain-queue")
async def drain_queue(x_cron_secret: str | None = Header(default=None)) -> dict:
    settings = get_settings()

    # Unset secret disables the endpoint outright (404) rather than 401/403,
    # so a deployment running a standing worker never advertises this route.
    if not settings.internal_cron_secret:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

    if not x_cron_secret or not hmac.compare_digest(x_cron_secret, settings.internal_cron_secret):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="invalid or missing X-Cron-Secret")

    cmd = [
        sys.executable,
        "-m",
        "celery",
        "-A",
        "app.execution.celery_app",
        "worker",
        "-Q",
        _QUEUES,
        "--pool=solo",
        "--concurrency=1",
        "--without-heartbeat",
        "--without-gossip",
        "--without-mingle",
        "-l",
        "warning",
    ]
    proc = subprocess.Popen(cmd)
    try:
        proc.wait(timeout=settings.internal_cron_drain_seconds)
        exit_code = proc.returncode
    except subprocess.TimeoutExpired:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        exit_code = None  # time-boxed, not a failure

    logger.info("drain-queue: worker subprocess ended (exit_code=%s)", exit_code)
    return {"status": "drained", "seconds": settings.internal_cron_drain_seconds}
