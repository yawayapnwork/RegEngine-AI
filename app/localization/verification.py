"""Requirement 3 -- Verification Cross-Check: compares a translated
English clause against its regional-language source using cross-
lingual semantic similarity scoring, AND (see this module's docstring
on why these are separate checks) exact numeric-precision comparison
(app.localization.numeric_precision).

Semantic similarity alone is NOT enough to catch a numeric translation
error -- verified empirically while building this module: embedding
'20%' vs '50%' in an otherwise-identical real SEBI-style Hindi margin
clause via `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`
only dropped cosine similarity from ~0.80 to ~0.69 (see
tests/test_localization.py's `TestCrossLingualVerifier` for the
recorded real numbers), nowhere near a reliable threshold for a change
that is a materially different legal obligation. A translation must
pass BOTH checks; failing either is a HITL-worthy flag, matching this
codebase's established "false precision is worse than acknowledged
ambiguity" philosophy (app.compiler.hitl's module docstring) rather
than averaging the two signals into one score that could mask either
kind of failure.
"""
from __future__ import annotations

import logging
from functools import lru_cache

from pydantic import BaseModel, Field

from app.config import Settings
from app.localization.languages import RegionalLanguage
from app.localization.numeric_precision import NumericPrecisionResult, check_numeric_precision

logger = logging.getLogger(__name__)


class TranslationVerificationResult(BaseModel):
    source_language: RegionalLanguage
    semantic_similarity: float
    semantic_similarity_passed: bool
    numeric_precision: NumericPrecisionResult
    passed: bool = False  # both checks must pass; see model_post_init
    reasons: list[str] = Field(default_factory=list)

    def model_post_init(self, __context) -> None:
        reasons = []
        if not self.semantic_similarity_passed:
            reasons.append(f"Semantic similarity {self.semantic_similarity:.3f} is below the configured threshold.")
        if not self.numeric_precision.numeric_precision_preserved:
            reasons.append(
                f"Numeric mismatch: source has {self.numeric_precision.mismatched_source_values} with no translated "
                f"counterpart; translation has {self.numeric_precision.mismatched_translated_values} not present in source."
            )
        self.reasons = reasons
        self.passed = not reasons


@lru_cache(maxsize=1)
def _load_similarity_model(model_id: str):
    from sentence_transformers import SentenceTransformer  # deferred heavy import

    logger.info("Loading cross-lingual similarity model %s (first call only; cached for the life of this process).", model_id)
    return SentenceTransformer(model_id)


def compute_cross_lingual_similarity(source_text: str, translated_text: str, model_id: str) -> float:
    """Embeds BOTH texts with the SAME multilingual sentence-embedding
    model (they land in a shared embedding space by construction of how
    such models are trained) and returns cosine similarity -- no back-
    translation step, so this measures the translation's fidelity to
    the source directly, not the fidelity of a second, independent
    translation step back into the source language."""
    from sentence_transformers import util  # deferred heavy import

    model = _load_similarity_model(model_id)
    embeddings = model.encode([source_text, translated_text], normalize_embeddings=True)
    return float(util.cos_sim(embeddings[0], embeddings[1]))


class CrossLingualVerifier:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def verify(self, source_text: str, translated_text: str, source_language: RegionalLanguage) -> TranslationVerificationResult:
        similarity = compute_cross_lingual_similarity(source_text, translated_text, self._settings.localization_similarity_model_id)
        numeric_result = check_numeric_precision(source_text, translated_text)

        return TranslationVerificationResult(
            source_language=source_language,
            semantic_similarity=similarity,
            semantic_similarity_passed=similarity >= self._settings.localization_similarity_threshold,
            numeric_precision=numeric_result,
        )
