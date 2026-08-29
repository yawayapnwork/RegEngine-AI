"""Semantic diff engine: for one new clause, finds the best-matching
historical clause (via dense-embedding cosine similarity against the
existing `sebi_master_circulars` Qdrant index -- app.vectorstore.qdrant_store),
then classifies what changed.

Deliberately reuses the SAME Qdrant collection app.vectorstore.qdrant_store
already indexes production clauses into, rather than a separate index --
"the historical Master Circular" IS that index, and a new circular's
clauses are diffed against it BEFORE being indexed themselves (this
module runs pre-compilation, and app.vectorstore.tasks.index_chunks_task
runs after compilation succeeds), so there is no risk of a new clause
matching against itself.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from app.agents.schemas import ExtractedComplianceRule, NumericalThreshold
from app.compiler.naming import metric_field_name
from app.config import Settings
from app.diffing.models import MatchConfidence, ThresholdDelta
from app.diffing.threshold_extraction import ExtractedThreshold

logger = logging.getLogger(__name__)

# Similarity bands -- see app.diffing.models.MatchConfidence's docstring
# for the rationale behind each cutoff. Deliberately coarser than
# app.llm_ops.semantic_cache's 0.97 exact-reuse threshold: that cache
# needs near-certainty two prompts mean the SAME thing to safely skip an
# LLM call; this diff only needs "probably the same clause" to decide
# whether a structural threshold comparison is worth attempting at all.
_CONFIDENCE_BANDS: list[tuple[float, MatchConfidence]] = [
    (0.995, MatchConfidence.IDENTICAL),
    (0.92, MatchConfidence.NEAR_DUPLICATE),
    (0.75, MatchConfidence.LIKELY_AMENDMENT),
    (0.55, MatchConfidence.WEAK_MATCH),
]


def classify_match_confidence(similarity: float) -> MatchConfidence:
    for threshold, confidence in _CONFIDENCE_BANDS:
        if similarity >= threshold:
            return confidence
    return MatchConfidence.NO_MATCH


@dataclass
class HistoricalMatch:
    chunk_id: str
    sha256: str | None
    circular_number: str | None
    clause_number: str | None
    text: str
    similarity: float
    confidence: MatchConfidence


async def find_best_historical_match(
    new_clause_text: str,
    settings: Settings,
    top_k: int = 3,
) -> HistoricalMatch | None:
    """Embeds `new_clause_text` and searches the existing clause index for
    its nearest neighbor(s). Returns the single best match (or None if the
    index is empty / unreachable -- a Qdrant outage must degrade this
    diff to "everything looks new" rather than raising and blocking the
    whole impact-report generation)."""
    from qdrant_client import AsyncQdrantClient

    from app.vectorstore.embeddings import embed_texts

    try:
        [vector] = await embed_texts([new_clause_text], settings)
    except Exception:
        logger.exception("Embedding failed for semantic diff; treating as no historical match.")
        return None

    client = AsyncQdrantClient(url=settings.qdrant_url, api_key=settings.qdrant_api_key, timeout=settings.qdrant_timeout_seconds)
    try:
        if not await client.collection_exists(settings.qdrant_collection):
            return None
        results = await client.query_points(collection_name=settings.qdrant_collection, query=vector, limit=top_k)
        points = results.points
        if not points:
            return None

        top = points[0]
        payload = top.payload or {}
        return HistoricalMatch(
            chunk_id=str(payload.get("chunk_id", top.id)),
            sha256=payload.get("sha256"),
            circular_number=payload.get("circular_number"),
            clause_number=payload.get("clause_number"),
            text=payload.get("text", ""),
            similarity=float(top.score),
            confidence=classify_match_confidence(float(top.score)),
        )
    except Exception:
        logger.exception("Qdrant query failed during semantic diff; treating as no historical match.")
        return None
    finally:
        await client.close()


def diff_thresholds(
    new_thresholds: list[NumericalThreshold],
    old_thresholds: list[ExtractedThreshold],
) -> tuple[list[ThresholdDelta], list[str], list[str]]:
    """Field-by-field comparison. Returns (deltas, new_only_fields,
    removed_fields):
      - `deltas` covers every field present on BOTH sides -- a genuine
        value/operator change.
      - `new_only_fields` are fields the new rule has that the old
        compiled rule never did -- candidate NEW_OBLIGATION signal even
        when a historical clause text match was found (the same clause
        can gain an entirely new deterministic condition on amendment).
      - `removed_fields` are the reverse -- an old threshold with no new
        counterpart, candidate OBLIGATION_REMOVED signal.
    """
    new_by_field: dict[str, NumericalThreshold] = {
        metric_field_name(t.metric, t.unit): t for t in new_thresholds
    }
    old_by_field: dict[str, ExtractedThreshold] = {
        t.field.removeprefix("facts."): t for t in old_thresholds
    }

    deltas: list[ThresholdDelta] = []
    for field, new_t in new_by_field.items():
        old_t = old_by_field.get(field)
        if old_t is None:
            continue
        delta_abs = None
        delta_pct = None
        if old_t.value:
            delta_abs = new_t.value - old_t.value
            delta_pct = (delta_abs / old_t.value) * 100.0
        tightened = _is_tightened(old_t, new_t)
        deltas.append(
            ThresholdDelta(
                field=f"facts.{field}",
                metric=new_t.metric,
                unit=new_t.unit,
                old_operator=old_t.operator,
                old_value=old_t.value,
                new_operator=new_t.operator.value,
                new_value=new_t.value,
                delta_absolute=delta_abs,
                delta_pct=delta_pct,
                tightened=tightened,
            )
        )

    new_only = [f for f in new_by_field if f not in old_by_field]
    removed = [f for f in old_by_field if f not in new_by_field]
    return deltas, new_only, removed


def _is_tightened(old_t: ExtractedThreshold, new_t: NumericalThreshold) -> bool | None:
    """True if the new threshold is strictly more restrictive. Only
    meaningful when the comparison direction didn't flip (a >= becoming a
    <= on the same metric is a different kind of change entirely, not a
    'tightening', and is reported as None here rather than a guess)."""
    op = new_t.operator.value
    if old_t.operator != op:
        return None
    if op in (">=", ">"):
        return new_t.value > old_t.value
    if op in ("<=", "<"):
        return new_t.value < old_t.value
    return None


_DEADLINE_UNIT_HINTS = {"days", "day", "hours", "hour", "months", "month", "years", "year"}
_DEADLINE_METRIC_HINTS = ("settlement", "deadline", "window", "timeline", "notice period", "cure period", "reporting")


def looks_like_deadline(metric: str, unit: str) -> bool:
    """Heuristic used by app.diffing.report_builder to route a
    THRESHOLD_SHIFT candidate into DEADLINE_AMENDMENT instead when the
    changed metric is clearly time-bound (a settlement cycle, a filing
    window) -- these generally hit different downstream systems
    (schedulers/settlement engines vs. calculation/limit engines), which
    is the entire reason the product requirement calls them out as a
    distinct category rather than folding them into threshold_shift."""
    if unit.strip().lower() in _DEADLINE_UNIT_HINTS:
        return True
    lowered = metric.lower()
    return any(hint in lowered for hint in _DEADLINE_METRIC_HINTS)
