"""LLM prompt templates for impact classification.

Used ONLY when structural comparison (app.diffing.threshold_extraction +
app.diffing.semantic_diff's deterministic rules) is inconclusive -- see
app.diffing.llm_classifier.classify_change_with_llm's docstring for
exactly which cases fall through to here. Deterministic classification is
preferred whenever possible (cheaper, faster, and -- unlike an LLM call --
produces the exact same verdict every time given the same two clauses),
mirroring the same cost-conscious hybrid-routing philosophy as
app.llm_ops.router.ModelRouter.

Two prompts:
  - IMPACT_CLASSIFICATION_SYSTEM_PROMPT: the fixed system role, defining
    the ChangeType taxonomy and the mandatory evidence-quoting discipline
    (same anti-hallucination principle as
    app.agents.schemas.ExtractedComplianceRule's verbatim_evidence field --
    a classification with no quoted textual basis is not trustworthy
    enough to act on).
  - build_classification_prompt(...): renders the actual comparison task
    (old clause text/thresholds vs. new clause text/thresholds) into the
    user turn.
"""
from __future__ import annotations

import json

from app.diffing.threshold_extraction import ExtractedThreshold

IMPACT_CLASSIFICATION_SYSTEM_PROMPT = """\
You are a regulatory change-impact analyst. You are given a NEW clause \
from a just-published circular/master direction, and OPTIONALLY the \
historical clause the semantic search believes it corresponds to (which \
may be absent if no confident match was found).

Classify the change into EXACTLY ONE of:
  - "threshold_shift": a numeric threshold's value (a percentage, amount, \
ratio, count) changed, and the underlying obligation is otherwise the same.
  - "deadline_amendment": a TIME-BOUND requirement changed (a settlement \
cycle, a reporting/filing window, a cure period, a notice period) -- \
treat this as its own category even though it is technically also a \
numeric threshold, because deadline changes typically require different \
downstream systems (schedulers, settlement engines) than a value \
threshold does (calculation/limit engines).
  - "new_obligation": there is no historical counterpart at all, or the \
new clause imposes a compliance trigger/obligation that did not exist \
before even if it superficially resembles an old clause.
  - "obligation_removed": the historical clause's obligation has been \
withdrawn/rescinded with no replacement in the new text.
  - "entity_scope_change": the same obligation now applies to a different \
or expanded/narrowed set of regulated entities.
  - "wording_only": the text changed but the enforceable obligation \
(entities, triggers, thresholds) is unchanged in substance.
  - "unchanged": no meaningful difference at all.

Rules:
  1. Quote the EXACT span of text (from either clause) that justifies your \
classification in `evidence_quote`. A classification with no verbatim \
quote from the provided text is not acceptable.
  2. If you are genuinely unsure between two categories, pick the more \
SEVERE one (new_obligation > obligation_removed > deadline_amendment > \
threshold_shift > entity_scope_change > wording_only > unchanged) and say \
so in `reasoning` -- a false "wording_only" that is actually a threshold \
shift is a much worse failure than an over-cautious threshold_shift call \
on truly cosmetic wording.
  3. Never invent a number, date, or entity that is not present in the \
provided text.

Output ONLY a single JSON object: \
{"change_type": "...", "confidence": 0.0-1.0, "evidence_quote": "...", "reasoning": "..."}
"""


def build_classification_prompt(
    new_clause_text: str,
    old_clause_text: str | None,
    new_thresholds: list[ExtractedThreshold],
    old_thresholds: list[ExtractedThreshold] | None,
    similarity_score: float | None,
) -> str:
    parts = [f"NEW CLAUSE TEXT:\n\"\"\"\n{new_clause_text}\n\"\"\""]

    if old_clause_text:
        parts.append(f"HISTORICAL CLAUSE TEXT (semantic match, similarity={similarity_score:.3f}):\n\"\"\"\n{old_clause_text}\n\"\"\"")
    else:
        parts.append("HISTORICAL CLAUSE TEXT: none found -- no confident semantic match in the existing Master Circular index.")

    if new_thresholds:
        parts.append("NEW extracted numeric thresholds:\n" + json.dumps([t.__dict__ for t in new_thresholds], indent=2))
    if old_thresholds:
        parts.append("OLD (previously compiled) numeric thresholds for the matched clause:\n" + json.dumps([t.__dict__ for t in old_thresholds], indent=2))

    parts.append("Classify this change per the taxonomy in your system prompt. Output ONLY the JSON object.")
    return "\n\n".join(parts)
