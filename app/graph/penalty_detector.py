"""Penalty-language detection: a cheap regex heuristic run over clause
text to populate `:Penalty` nodes, matching this codebase's established
"deterministic heuristic before anything structured" pattern (the same
shape as app.agents.graph.complexity_router's math/cross-reference
detection and app.diffing.semantic_diff's `looks_like_deadline`).

Not every clause with an obligation states a penalty -- SEBI circulars
frequently defer enforcement consequences to a separate master
circular/regulation on penalties generally. `detect_penalty` returning
`None` for such a clause is the expected, common case, not a detection
failure; app.graph.conflict_detection's gap-detection query is what
surfaces "mandatory obligation with no penalty anywhere in the graph" as
an actual finding worth a human's attention.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

_PENALTY_KEYWORD_RE = re.compile(
    r"\b(penalty|penalties|fine|fined|forfeit(?:ure)?|debar(?:ment|red)?|suspension of (?:certificate|registration)|"
    r"cancellation of (?:certificate|registration)|monetary penalty|liable to pay)\b",
    re.IGNORECASE,
)

# An amount near a penalty keyword -- INR/Rs figure, or a per-day/per-
# instance recurring penalty phrase.
_AMOUNT_RE = re.compile(
    r"(?:INR|Rs\.?|₹)\s*[\d,]+(?:\.\d+)?\s*(?:crore|lakh)?(?:\s*per\s*(?:day|instance|violation))?",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class DetectedPenalty:
    description: str
    amount_text: str | None
    basis_text: str  # the verbatim sentence/span the detection came from


def detect_penalty(text: str) -> DetectedPenalty | None:
    keyword_match = _PENALTY_KEYWORD_RE.search(text)
    if not keyword_match:
        return None

    # Scope the "sentence" a penalty keyword appears in for basis_text,
    # rather than the whole (possibly long) clause -- a clause can state
    # an obligation AND a penalty in different sentences, and only the
    # penalty-bearing sentence should be the recorded evidence.
    sentence_start = text.rfind(".", 0, keyword_match.start()) + 1
    sentence_end_match = re.search(r"[.;]", text[keyword_match.end():])
    sentence_end = keyword_match.end() + (sentence_end_match.end() if sentence_end_match else len(text) - keyword_match.end())
    basis_text = text[sentence_start:sentence_end].strip()

    amount_match = _AMOUNT_RE.search(basis_text)
    return DetectedPenalty(
        description=keyword_match.group(0),
        amount_text=amount_match.group(0) if amount_match else None,
        basis_text=basis_text,
    )
