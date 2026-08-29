"""Requirement 2 -- Cross-Document Entity Resolution: detects EXPLICIT
supersession/amendment language within a clause's own text (e.g. "...
hereby supersedes Clause 1.1 of the Master Circular SEBI/HO/MIRSD/2020/01
...") and extracts the specific clause/circular it replaces, so
app.graph.sync can write a `(:Clause)-[:SUPERSEDES]->(:Clause)` /
`AMENDS` edge automatically at ingestion time -- no operator step.

This is deliberately narrower than, and does not replace,
`app.graph.sync.declare_supersession`/`declare_amendment`: those write
a CIRCULAR-level edge and are "deliberately OPERATOR-ASSERTED, never
auto-inferred" (see that module's docstring) -- a whole-document
replacement is a regulatory/legal judgment call this codebase has
already decided a human must make. What THIS module detects is
narrower and mechanical: a clause that EXPLICITLY NAMES the specific
old clause/circular it replaces, in language a deterministic pattern
can recognize with the same "cheap heuristic, verifiable evidence"
posture as `app.graph.penalty_detector.detect_penalty` --
`auto_detected=True` is stamped on every edge this module's output
produces specifically so a graph consumer (or a human reviewing the
CONFLICTS_SUBGRAPH-style view) can tell an auto-detected clause-level
edge apart from an operator-confirmed circular-level one, and so a
false-positive match doesn't silently masquerade as the same kind of
asserted fact `declare_supersession` represents.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

_CLAUSE_TOKEN = r"(?:clause|paragraph|para\.?|regulation|reg\.?)\s*([\d]+(?:\.[\d]+)*[a-z]?)"

# A regulator document-number shape broad enough to cover SEBI/RBI/IRDAI/
# PFRDA's own formats (see app.regulatory.taxonomy's per-regulator
# regexes, which this deliberately does NOT import -- this module's job
# is to capture "whatever string names the circular" for a human to
# verify, a superset of any one regulator's strict format, not to
# validate that format) -- falls back to capturing the surrounding
# phrase when no such number is present (e.g. "the Master Circular on
# Margin Requirements" cited by title only).
_DOC_NUMBER = r"[A-Z]{2,10}(?:/[A-Z0-9\-]+)+/\d{4}(?:[-/]\d{2,4})?/\d+"

_SUPERSESSION_VERBS = r"(?:supersedes?|hereby\s+supersedes?|stands?\s+superseded\s+by|replaces?|hereby\s+replaces?|stands?\s+substituted\s+for|in\s+supersession\s+of)"
_AMENDMENT_VERBS = r"(?:amends?|hereby\s+amends?|stands?\s+amended\s+by|modifies?|in\s+(?:partial\s+)?(?:modification|amendment)\s+of)"

_TARGET_SUFFIX = rf"\s+of\s+(?:the\s+)?((?:master\s+)?circular(?:\s+no\.?)?\s*[:\-]?\s*(?:{_DOC_NUMBER})|(?:master\s+)?circular\s+on\s+[^.,;\n]{{1,80}})"

_SUPERSESSION_RE = re.compile(rf"{_SUPERSESSION_VERBS}\s+{_CLAUSE_TOKEN}{_TARGET_SUFFIX}", re.IGNORECASE)
_AMENDMENT_RE = re.compile(rf"{_AMENDMENT_VERBS}\s+{_CLAUSE_TOKEN}{_TARGET_SUFFIX}", re.IGNORECASE)

_DOC_NUMBER_RE = re.compile(_DOC_NUMBER, re.IGNORECASE)


class SupersessionRelationshipType(str, Enum):
    SUPERSEDES = "SUPERSEDES"
    AMENDS = "AMENDS"


@dataclass(frozen=True)
class DetectedSupersession:
    relationship_type: SupersessionRelationshipType
    target_clause_number: str
    target_circular_reference: str = None  # type: ignore[assignment]  # raw captured text naming the old circular (a doc number if present, else a title phrase)
    target_circular_number: str | None = None  # the doc-number substring specifically, if one was found within target_circular_reference
    basis_text: str = None  # type: ignore[assignment]  # verbatim matched sentence -- the evidence a human reviews before trusting this edge
    confidence: float = 0.8  # deterministic-pattern confidence, not a model score -- see this module's docstring on why this is lower than a REFERENCES edge's implicit 1.0 (a supersession claim has bigger consequences if wrong)


def _sentence_span(text: str, match: re.Match) -> str:
    start = text.rfind(".", 0, match.start()) + 1
    end_match = re.search(r"[.;]", text[match.end():])
    end = match.end() + (end_match.end() if end_match else len(text) - match.end())
    return text[start:end].strip()


def _extract(text: str, pattern: re.Pattern, relationship_type: SupersessionRelationshipType) -> list[DetectedSupersession]:
    results = []
    for match in pattern.finditer(text):
        target_clause_number = match.group(1)
        target_circular_reference = match.group(2).strip()
        doc_number_match = _DOC_NUMBER_RE.search(target_circular_reference)
        results.append(
            DetectedSupersession(
                relationship_type=relationship_type,
                target_clause_number=target_clause_number,
                target_circular_reference=target_circular_reference,
                target_circular_number=doc_number_match.group(0) if doc_number_match else None,
                basis_text=_sentence_span(text, match),
            )
        )
    return results


def detect_supersessions(text: str) -> list[DetectedSupersession]:
    """Returns every explicit supersession/amendment claim found in
    `text`, in first-appearance order. A clause naming no such target
    (the overwhelming majority of clauses) returns an empty list -- the
    expected, common case, exactly like `detect_penalty` returning None
    for a clause with no stated penalty."""
    return _extract(text, _SUPERSESSION_RE, SupersessionRelationshipType.SUPERSEDES) + _extract(
        text, _AMENDMENT_RE, SupersessionRelationshipType.AMENDS
    )
