"""Clause-numbering and section-hierarchy detection for SEBI legal text.

SEBI Master Circulars mix several numbering conventions in the same document:
    1.            (top-level section)
    1.1           (sub-section)
    2.1.b / 2.1.(b)  (sub-clause, alpha)
    (i), (ii), (iii)  (roman sub-items)
    A. / B.       (annexure lettering)

This module classifies a line of text as a clause/section header and derives
its position in the document's hierarchy so chunks can carry a full
`section_path` (e.g. ["1", "1.2", "1.2.a"]) rather than a bare number.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# Ordered from most to least specific so the first match wins.
_CLAUSE_PATTERNS: list[re.Pattern[str]] = [
    # 2.1.b  /  2.1.b.  /  2.1.(b)
    re.compile(r"^\s*(?P<num>\d+\.\d+\.[a-z]{1,2})\.?\s*[).]?\s+"),
    re.compile(r"^\s*(?P<num>\d+\.\d+)\.\((?P<sub>[a-z]{1,2})\)\s+"),
    # 1.1.1
    re.compile(r"^\s*(?P<num>\d+(?:\.\d+){2,})\.?\s+"),
    # 1.1
    re.compile(r"^\s*(?P<num>\d+\.\d+)\.?\s+"),
    # 1.  (top-level section)
    re.compile(r"^\s*(?P<num>\d+)\.\s+"),
    # (i) (ii) (iii) (iv) ... roman numeral sub-items
    re.compile(r"^\s*\((?P<num>[ivxlcdm]{1,6})\)\s+", re.IGNORECASE),
    # (a) (b) (c) alpha sub-items
    re.compile(r"^\s*\((?P<num>[a-z]{1,2})\)\s+"),
    # A. / B.  annexure-style lettering
    re.compile(r"^\s*(?P<num>[A-Z])\.\s+"),
]

_FOOTNOTE_MARKER = re.compile(r"^\s*(?:\[(?P<fn1>\d{1,3})\]|(?P<fn2>\d{1,3})\s*[.)])\s*(?=\S)")
_FOOTNOTE_LINE = re.compile(r"^\s{0,4}\d{1,3}[.)]\s+\S")

_SECTION_HEADER_HINTS = re.compile(
    r"^(chapter|part|annexure|schedule|appendix)\b", re.IGNORECASE
)


@dataclass
class ClauseMatch:
    clause_number: str
    depth: int
    remainder: str


def detect_clause_number(text: str) -> ClauseMatch | None:
    """Return the leading clause/section number of a line of text, if any."""
    stripped = text.strip()
    if not stripped:
        return None
    for pattern in _CLAUSE_PATTERNS:
        m = pattern.match(stripped)
        if not m:
            continue
        num = m.group("num")
        sub = m.groupdict().get("sub")
        clause_number = f"{num}.{sub}" if sub else num
        depth = clause_number.count(".") + 1
        remainder = stripped[m.end():].strip()
        return ClauseMatch(clause_number=clause_number, depth=depth, remainder=remainder)
    return None


def is_section_header(text: str, clause: ClauseMatch | None) -> bool:
    """Heuristic: short, title-cased/uppercase line with no terminal punctuation,
    or an explicit Chapter/Part/Annexure marker."""
    stripped = text.strip()
    if not stripped:
        return False
    if _SECTION_HEADER_HINTS.match(stripped):
        return True
    if clause is not None and clause.depth == 1 and len(clause.remainder) < 90:
        if not clause.remainder.endswith((".", ";", ":")):
            return True
    return False


def is_footnote(text: str) -> bool:
    stripped = text.strip()
    return bool(_FOOTNOTE_LINE.match(stripped)) and len(stripped) < 500


@dataclass
class HierarchyTracker:
    """Maintains a running stack of (clause_number, title) as elements are
    streamed in document order, so each element can be tagged with its full
    ancestry (section_path) even when the source PDF has no bookmarks/TOC.
    """

    _stack: list[tuple[str, int]] = field(default_factory=list)

    def update(self, clause_number: str | None, depth: int) -> list[str]:
        if clause_number is None:
            return [num for num, _ in self._stack]
        while self._stack and self._stack[-1][1] >= depth:
            self._stack.pop()
        self._stack.append((clause_number, depth))
        return [num for num, _ in self._stack]

    def current_path(self) -> list[str]:
        return [num for num, _ in self._stack]
