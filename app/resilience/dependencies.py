"""FastAPI dependency wiring for the Dead-Letter Queue admin API."""
from __future__ import annotations

from fastapi import Depends

from app.config import Settings, get_settings
from app.execution.dependencies import get_redis_pool
from app.resilience.dead_letter_queue import DeadLetterQueue


def get_dlq(settings: Settings = Depends(get_settings)) -> DeadLetterQueue:
    return DeadLetterQueue(redis_client=get_redis_pool(), key_prefix=settings.dlq_key_prefix)
