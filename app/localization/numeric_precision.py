"""Requirement 2's "without losing... numeric precision": extracts every
numeric token from a regional-script source clause and from its English
translation, and reports any mismatch.

Deliberately a SEPARATE check from cross-lingual semantic similarity
(app.localization.verification), not folded into it -- empirically
verified while building this module (see tests/test_localization.py's
`TestSemanticSimilarityIsWeakOnNumericDrift`): a translation that
changes "20%" to "50%" drops a multilingual sentence-embedding
similarity score only modestly (observed ~0.80 -> ~0.69 on a real
SEBI-style margin clause via `paraphrase-multilingual-MiniLM-L12-v2`),
nowhere near enough to reliably threshold on for a numeric error that
is a materially different legal obligation. Numeric precision needs its
own exact check.
"""
from __future__ import annotations

import re

from pydantic import BaseModel

# Devanagari (Hindi, Marathi) and Gujarati digit blocks, alongside the
# Western digits every one of these languages' legal/financial text
# also commonly uses (a modern SEBI-style Hindi circular typically
# writes "20%" in Western numerals even in otherwise-Devanagari prose --
# both conventions are handled since either can appear).
_DEVANAGARI_DIGITS = "०१२३४५६७८९"
_GUJARATI_DIGITS = "૦૧૨૩૪૫૬૭૮૯"
_DIGIT_TRANSLATION = str.maketrans(
    _DEVANAGARI_DIGITS + _GUJARATI_DIGITS,
    "0123456789" * 2,
)

_NUMBER_WITH_UNIT_RE = re.compile(
    r"""
    (?P<value>[0-9०-९૦-૯][0-9०-९૦-૯,]*(?:\.[0-9०-९૦-૯]+)?)
    \s*
    (?P<unit>%|percent|per\ cent|crore|करोड़|कोटी|કરોડ|lakh|लाख|લાખ|bps|days?|दिन|दिवस|દિવસ|months?|महीने|મહિના|years?|वर्ष|वर्षे|વર્ષ|INR|Rs\.?|₹|रुपये|रुपए|રૂપિયા)?
    """,
    re.IGNORECASE | re.VERBOSE,
)


class NumericToken(BaseModel):
    raw_match: str
    normalized_value: float
    unit: str | None
    context: str


class NumericPrecisionResult(BaseModel):
    source_numbers: list[NumericToken]
    translated_numbers: list[NumericToken]
    matched_count: int
    mismatched_source_values: list[float]  # values found in source with no corresponding value in translation
    mismatched_translated_values: list[float]  # values found in translation with no corresponding value in source (a hallucinated or drifted number)
    numeric_precision_preserved: bool


def extract_numeric_tokens(text: str, context_window: int = 40) -> list[NumericToken]:
    """Same shape/purpose as app.agents.tools.scan_numeric_tokens (its
    English-only regex is reused unmodified for English text via that
    function; this one additionally recognizes Devanagari/Gujarati
    digit glyphs and their languages' unit words), so a caller comparing
    a regional source against its English translation gets numeric
    tokens in a directly comparable shape from both sides."""
    tokens: list[NumericToken] = []
    for m in _NUMBER_WITH_UNIT_RE.finditer(text):
        raw_value = m.group("value")
        if not raw_value:
            continue
        normalized = raw_value.translate(_DIGIT_TRANSLATION).replace(",", "")
        try:
            value = float(normalized)
        except ValueError:
            continue
        start = max(0, m.start() - context_window)
        end = min(len(text), m.end() + context_window)
        tokens.append(
            NumericToken(
                raw_match=m.group(0).strip(),
                normalized_value=value,
                unit=(m.group("unit") or "").strip() or None,
                context=text[start:end].strip(),
            )
        )
    return tokens


def check_numeric_precision(source_text: str, translated_text: str, *, tolerance: float = 1e-6) -> NumericPrecisionResult:
    """Set-based comparison of numeric VALUES (not units -- a unit word
    naturally changes across languages, e.g. "करोड़" -> "crore", and that
    is not a precision loss) between the source-language clause and its
    English translation. A value appearing in one side and not the
    other (within `tolerance`) is exactly the failure mode Requirement
    2 names: a translation that silently drops, rounds, or invents a
    number.
    """
    source_tokens = extract_numeric_tokens(source_text)
    translated_tokens = extract_numeric_tokens(translated_text)

    source_values = [t.normalized_value for t in source_tokens]
    translated_values = [t.normalized_value for t in translated_tokens]

    def _remove_first_match(pool: list[float], value: float) -> bool:
        for i, v in enumerate(pool):
            if abs(v - value) <= tolerance:
                pool.pop(i)
                return True
        return False

    remaining_translated = list(translated_values)
    mismatched_source: list[float] = []
    matched = 0
    for value in source_values:
        if _remove_first_match(remaining_translated, value):
            matched += 1
        else:
            mismatched_source.append(value)

    return NumericPrecisionResult(
        source_numbers=source_tokens,
        translated_numbers=translated_tokens,
        matched_count=matched,
        mismatched_source_values=mismatched_source,
        mismatched_translated_values=remaining_translated,
        numeric_precision_preserved=not mismatched_source and not remaining_translated,
    )
