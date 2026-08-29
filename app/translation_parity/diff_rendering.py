"""Requirement 3's "side-by-side highlighted text diffs": renders one
aligned clause pair (or an unmatched clause with no counterpart) as an
HTML fragment for the compliance officer review dashboard.

English and Hindi are different scripts entirely, so a character/word-
level `difflib` diff between the two columns (as you'd use for two
versions of the SAME language, e.g. app.diffing) is meaningless -- there
is no shared vocabulary to align token-by-token. What IS meaningful and
directly actionable for a reviewer is highlighting exactly which
NUMBERS in each column matched, and which didn't, since that is
precisely the class of error Requirement 2 exists to catch (a
mistranslated "20%" as "50%", or a number dropped/invented entirely).
Semantic drift (prose that reads differently in tone or completeness)
is reported as a discrepancy but is not something a per-character diff
can usefully visualize across scripts either -- that's surfaced as text
(`ClauseDiscrepancy.description`), not highlighting.
"""
from __future__ import annotations

import html

from app.localization.numeric_precision import NumericPrecisionResult
from app.translation_parity.numeric_extraction import find_numeric_spans

_MARK_STYLES = {
    "matched": "background-color:#d4edda;color:#155724;",       # a value present, with a counterpart on the other side
    "mismatched": "background-color:#f8d7da;color:#721c24;font-weight:bold;",  # a value with NO counterpart on the other side -- the actionable flag
}


def _highlight(text: str, mismatched_values: list[float], tolerance: float = 1e-6) -> str:
    spans = find_numeric_spans(text)
    pieces: list[str] = []
    cursor = 0
    for span in spans:
        pieces.append(html.escape(text[cursor:span.start]))
        is_mismatched = any(abs(span.normalized_value - v) <= tolerance for v in mismatched_values)
        style_key = "mismatched" if is_mismatched else "matched"
        pieces.append(f'<mark style="{_MARK_STYLES[style_key]}">{html.escape(span.raw_match)}</mark>')
        cursor = span.end
    pieces.append(html.escape(text[cursor:]))
    return "".join(pieces)


def render_side_by_side_diff(
    english_text: str | None,
    hindi_text: str | None,
    numeric_result: NumericPrecisionResult | None,
    *,
    english_clause_number: str | None = None,
    hindi_clause_number: str | None = None,
) -> str:
    """Returns a single, self-contained HTML `<table>` fragment -- two
    columns (English left, Hindi right), each numeric token highlighted
    green (matched on the other side) or red-bold (mismatched: present
    on this side only). Either side may be None (a MISSING_CLAUSE
    discrepancy has nothing to render in the missing column, shown as
    an explicit "— no corresponding clause —" placeholder instead of an
    empty cell, so the absence itself is visually unambiguous)."""
    mismatched_in_english = numeric_result.mismatched_source_values if numeric_result else []
    mismatched_in_hindi = numeric_result.mismatched_translated_values if numeric_result else []

    english_cell = _highlight(english_text, mismatched_in_english) if english_text else '<em style="color:#888;">&mdash; no corresponding clause &mdash;</em>'
    hindi_cell = _highlight(hindi_text, mismatched_in_hindi) if hindi_text else '<em style="color:#888;">&mdash; no corresponding clause &mdash;</em>'

    return (
        '<table style="width:100%;border-collapse:collapse;table-layout:fixed;" class="translation-parity-diff">'
        "<thead><tr>"
        f'<th style="width:50%;text-align:left;border-bottom:1px solid #ccc;padding:6px;">English'
        f'{f" (Clause {html.escape(english_clause_number)})" if english_clause_number else ""}</th>'
        f'<th style="width:50%;text-align:left;border-bottom:1px solid #ccc;padding:6px;">Hindi'
        f'{f" (Clause {html.escape(hindi_clause_number)})" if hindi_clause_number else ""}</th>'
        "</tr></thead>"
        "<tbody><tr>"
        f'<td style="vertical-align:top;padding:6px;border-right:1px solid #eee;white-space:pre-wrap;">{english_cell}</td>'
        f'<td style="vertical-align:top;padding:6px;white-space:pre-wrap;" lang="hi">{hindi_cell}</td>'
        "</tr></tbody></table>"
    )
