"""LLM-backed explanation fallback -- see prompts.py's module docstring
for exactly when this is invoked (never on the hot evaluate/ledger-write
path; only from the on-demand explanation API for a violation string the
deterministic parser couldn't structurally match).

Reuses `litellm` directly, the same convention as
app.llm_ops.cached_extraction's escalation path and
app.diffing.llm_classifier -- one LLM-calling pattern across the codebase.
"""
from __future__ import annotations

import json
import logging

from app.config import Settings
from app.explainability.models import ExplanationSource, LegalExplanation
from app.explainability.prompts import EXPLANATION_SYSTEM_PROMPT, build_explanation_prompt
from app.execution.models import PolicyOutcome

logger = logging.getLogger(__name__)


async def explain_violation_with_llm(
    settings: Settings,
    outcome: PolicyOutcome,
    raw_violation_text: str,
    regulator: str = "sebi",
) -> LegalExplanation:
    import litellm

    prompt = build_explanation_prompt(raw_violation_text, outcome.rule_id, outcome.circular_number, outcome.clause_number, regulator)

    try:
        response = await litellm.acompletion(
            model=settings.llm_router_frontier_model,
            api_key=settings.hf_api_token,
            temperature=0.0,
            max_tokens=512,
            messages=[
                {"role": "system", "content": EXPLANATION_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
        )
        parsed = json.loads(response.choices[0].message.content)
        return LegalExplanation(
            rule_id=outcome.rule_id,
            circular_number=outcome.circular_number,
            clause_number=outcome.clause_number,
            headline=parsed["headline"],
            citation=parsed["citation"],
            structured_violation=None,
            source=ExplanationSource.LLM,
            confidence=float(parsed.get("confidence", 0.7)),
        )
    except Exception:
        logger.exception("LLM explanation fallback failed for rule_id=%s; passing raw violation text through verbatim.", outcome.rule_id)
        # A failed LLM call must never hide the violation from the
        # auditor -- fall through to the raw compiler-generated text
        # rather than dropping the explanation entirely.
        return LegalExplanation(
            rule_id=outcome.rule_id,
            circular_number=outcome.circular_number,
            clause_number=outcome.clause_number,
            headline=f"Trade rejected: {raw_violation_text}",
            citation=f"Clause {outcome.clause_number or 'unscoped'} ({outcome.circular_number or 'circular unknown'})",
            structured_violation=None,
            source=ExplanationSource.UNPARSEABLE,
            confidence=0.0,
        )
