"""FastAPI DI wiring, mirroring app.execution.dependencies's shape."""
from __future__ import annotations

from fastapi import Depends

from app.config import Settings, get_settings
from app.execution.dependencies import get_redis_pool
from app.translation_parity.checker import SemanticParityChecker
from app.translation_parity.queue import TranslationDiscrepancyQueue


def get_translation_discrepancy_queue(settings: Settings = Depends(get_settings)) -> TranslationDiscrepancyQueue:
    return TranslationDiscrepancyQueue(redis_client=get_redis_pool(), key_prefix=settings.translation_parity_queue_key_prefix)


def get_semantic_parity_checker(settings: Settings = Depends(get_settings)) -> SemanticParityChecker:
    return SemanticParityChecker(settings)
