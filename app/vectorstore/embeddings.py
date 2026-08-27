"""Dense embedding generation.

The sentence-transformers model is loaded once per process (expensive) and
reused across requests. Inference is CPU/GPU-bound and synchronous, so it is
offloaded to a worker thread to keep the event loop responsive.
"""
from __future__ import annotations

import asyncio
import logging
import threading
from functools import lru_cache

from app.config import Settings
from app.parsing.exceptions import EmbeddingError

logger = logging.getLogger(__name__)
_model_lock = threading.Lock()


@lru_cache(maxsize=1)
def _load_model(model_name: str):
    from sentence_transformers import SentenceTransformer  # heavy import, deferred

    logger.info("Loading embedding model %s", model_name)
    return SentenceTransformer(model_name)


def _encode_sync(texts: list[str], model_name: str, batch_size: int) -> list[list[float]]:
    with _model_lock:
        model = _load_model(model_name)
    vectors = model.encode(
        texts,
        batch_size=batch_size,
        normalize_embeddings=True,
        show_progress_bar=False,
    )
    return [v.tolist() for v in vectors]


async def embed_texts(texts: list[str], settings: Settings) -> list[list[float]]:
    if not texts:
        return []
    try:
        return await asyncio.to_thread(_encode_sync, texts, settings.embedding_model_name, settings.embedding_batch_size)
    except Exception as exc:  # noqa: BLE001
        raise EmbeddingError(f"Failed to embed {len(texts)} chunk(s): {exc!r}") from exc
