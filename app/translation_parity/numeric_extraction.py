"""Requirement 2's numerical-extraction regex tool, position-aware.

`app.localization.numeric_precision.extract_numeric_tokens`/
`check_numeric_precision` already do the definitive VALUE comparison
(exact, set-based, multilingual-unit-aware) -- this module deliberately
does NOT reimplement that logic; `find_numeric_spans` below is a
thin, position-preserving sibling of the same regex idea, needed only
because `check_numeric_precision` discards character offsets (it only
needs a `context` window string, not a span) while
`app.translation_parity.diff_rendering` needs exact offsets to wrap a
mismatched number in an HTML `<mark>` at the right place in the
original text.
"""
from __future__ import annotations

import re

from pydantic import BaseModel

# Same digit blocks and unit vocabulary as
# app.localization.numeric_precision._NUMBER_WITH_UNIT_RE (Devanagari +
# Gujarati digit glyphs, Western digits, and each language's own unit
# words) -- kept as an independent regex rather than importing that
# module's private pattern, matching this codebase's established
# preference (see app.graph.supersession_extractor's module docstring)
# for a small amount of duplicated pattern text over a cross-module
# dependency on another module's private implementation detail.
_DEVANAGARI_DIGITS = "०१२३४५६७८९"
_GUJARATI_DIGITS = "૦૧૨૩૪૫૬૭૮૯"
_DIGIT_TRANSLATION = str.maketrans(_DEVANAGARI_DIGITS + _GUJARATI_DIGITS, "0123456789" * 2)

NUMERIC_SPAN_RE = re.compile(
    r"""
    (?P<value>[0-9०-९૦-૯][0-9०-९૦-૯,]*(?:\.[0-9०-९૦-૯]+)?)
    \s*
    (?P<unit>%|percent|per\ cent|crore|करोड़|कोटी|કરોડ|lakh|लाख|લાખ|bps|days?|दिन|दिवस|દિવસ|months?|महीने|મહિના|years?|वर्ष|वर्षे|વર્ષ|INR|Rs\.?|₹|रुपये|रुपए|રૂપિયા)?
    """,
    re.IGNORECASE | re.VERBOSE,
)


class NumericSpan(BaseModel):
    start: int
    end: int
    raw_match: str
    normalized_value: float


def find_numeric_spans(text: str) -> list[NumericSpan]:
    """Every numeric token in `text` with its exact character offsets,
    in document order -- used only for highlighting (see
    app.translation_parity.diff_rendering.render_side_by_side_diff),
    never for the pass/fail fidelity decision itself (that's
    `app.localization.numeric_precision.check_numeric_precision`, the
    single source of truth for "did this value survive translation")."""
    spans: list[NumericSpan] = []
    for m in NUMERIC_SPAN_RE.finditer(text):
        raw_value = m.group("value")
        if not raw_value:
            continue
        normalized = raw_value.translate(_DIGIT_TRANSLATION).replace(",", "")
        try:
            value = float(normalized)
        except ValueError:
            continue
        # The pattern's `\s*` before the optional unit group can consume
        # trailing whitespace even when no unit word actually follows
        # (e.g. "50,000 " before "per day", where "per" isn't itself a
        # unit word) -- trim that trailing whitespace from the match so
        # `end` and `raw_match` stay in exact sync with `text[start:end]`.
        full_match = m.group(0)
        trimmed = full_match.rstrip()
        spans.append(NumericSpan(start=m.start(), end=m.start() + len(trimmed), raw_match=trimmed, normalized_value=value))
    return spans
