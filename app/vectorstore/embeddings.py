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


def _mock_encode(texts: list[str], dim: int) -> list[list[float]]:
    """Deterministic hash-based offline pseudo-embeddings for tests and air-gapped environments."""
    import hashlib
    import math
    results = []
    for text in texts:
        vec = [0.0] * dim
        words = text.lower().split()
        if not words:
            words = ["empty"]
        for i, word in enumerate(words):
            h = int(hashlib.sha256(word.encode("utf-8")).hexdigest(), 16)
            for j in range(min(16, dim)):
                idx = (h + j * 31) % dim
                vec[idx] += 1.0 / (i + 1)
        norm = math.sqrt(sum(x * x for x in vec))
        if norm > 0:
            vec = [x / norm for x in vec]
        else:
            vec[0] = 1.0
        results.append(vec)
    return results


async def embed_texts(texts: list[str], settings: Settings) -> list[list[float]]:
    if not texts:
        return []
    if getattr(settings, "mock_embeddings_enabled", False) or getattr(settings, "embedding_model_name", "") == "mock":
        return _mock_encode(texts, settings.embedding_dim)
    try:
        return await asyncio.to_thread(_encode_sync, texts, settings.embedding_model_name, settings.embedding_batch_size)
    except Exception as exc:  # noqa: BLE001
        raise EmbeddingError(f"Failed to embed {len(texts)} chunk(s): {exc!r}") from exc
