"""LLM-backed fallback classification for changes that deterministic
structural comparison (app.diffing.semantic_diff) cannot confidently
classify on its own -- specifically:

  - MatchConfidence.WEAK_MATCH: a historical clause was found but the
    similarity score is too low to trust a structural (field-by-field)
    threshold comparison against it -- an LLM reading both texts can
    judge "is this actually the same obligation reworded, or unrelated"
    in a way a cosine-similarity threshold cannot.
  - A clause with qualitative_directives but no deterministic_logic on
    either side -- there are no numeric fields to structurally diff, so
    the only way to tell "wording_only" from "new_obligation" is to
    actually read the text.
  - MatchConfidence.NO_MATCH -- structurally this always resolves to
    NEW_OBLIGATION, but an LLM call can still add useful narrative
    context (this module is not invoked for that case; see
    app.diffing.semantic_diff for why NO_MATCH is trusted structurally).

Reuses `litellm` directly (same library app.llm_ops.cached_extraction's
escalation path and app.agents.crew's CrewAI LLM wrapper both sit on top
of) rather than adding a third LLM-calling convention to the codebase.
"""
from __future__ import annotations

import json
import logging

from pydantic import BaseModel, Field

from app.config import Settings
from app.diffing.models import ChangeType
from app.diffing.prompts import IMPACT_CLASSIFICATION_SYSTEM_PROMPT, build_classification_prompt
from app.diffing.threshold_extraction import ExtractedThreshold

logger = logging.getLogger(__name__)


class LLMClassificationResult(BaseModel):
    change_type: ChangeType
    confidence: float = Field(..., ge=0.0, le=1.0)
    evidence_quote: str
    reasoning: str


async def classify_change_with_llm(
    settings: Settings,
    new_clause_text: str,
    old_clause_text: str | None,
    new_thresholds: list[ExtractedThreshold],
    old_thresholds: list[ExtractedThreshold] | None,
    similarity_score: float | None,
) -> LLMClassificationResult:
    import litellm

    prompt = build_classification_prompt(new_clause_text, old_clause_text, new_thresholds, old_thresholds, similarity_score)

    response = await litellm.acompletion(
        model=settings.llm_router_frontier_model,
        api_key=settings.hf_api_token,
        temperature=0.0,
        max_tokens=1024,
        messages=[
            {"role": "system", "content": IMPACT_CLASSIFICATION_SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
    )
    raw_content = response.choices[0].message.content

    try:
        parsed = json.loads(raw_content)
        return LLMClassificationResult.model_validate(parsed)
    except Exception:
        logger.exception("LLM impact-classification returned unparseable/invalid output: %r", raw_content[:500])
        # A failed classification must never silently disappear from the
        # report -- surface it as the most conservative (most severe)
        # verdict with a confidence of 0, forcing HITL review rather than
        # defaulting to a falsely reassuring "unchanged".
        return LLMClassificationResult(
            change_type=ChangeType.NEW_OBLIGATION,
            confidence=0.0,
            evidence_quote="",
            reasoning="LLM classification call failed or returned unparseable output; defaulting to the most severe category pending human review.",
        )
