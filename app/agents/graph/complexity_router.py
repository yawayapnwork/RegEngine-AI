"""Dynamic routing (Requirement 1): pure regex/heuristic detection of
clause complexity, deciding which specialist agent a clause should go to
BEFORE any LLM call is made -- consistent with this codebase's existing
"cheap, fast, deterministic pre-call classification" pattern already used
by app.llm_ops.router.ModelRouter and app.diffing.semantic_diff's
`looks_like_deadline` heuristic. No LLM call is spent just deciding which
LLM/agent to invoke.
"""
from __future__ import annotations

import re

from app.agents.graph.state import ComplexityFlags

# Mathematical notation and formula-introducing phrases. Combines actual
# math symbols (which plain SEBI legal prose essentially never contains)
# with phrases that reliably precede a formula in Indian financial
# regulatory drafting (CRAR/VaR/margin-computation clauses).
_MATH_SYMBOL_RE = re.compile(r"[±×÷√≤≥∑Σ∫]")
_MATH_PHRASE_RE = re.compile(
    r"\b(the formula|computed as|calculated as follows|shall be calculated using|weighted average of|"
    r"standard deviation|variance of|value at risk|regression coefficient|square root of|"
    r"summation of|shall be computed in accordance with the formula)\b",
    re.IGNORECASE,
)
# "Variable = expression" pattern -- e.g. "CRAR = (Tier I + Tier II) / RWA".
# Requires a short (<=6 word) left-hand side to avoid false-positiving on
# ordinary prose that happens to contain an equals sign in an unrelated
# sense (rare in legal text, but cheap to guard against).
_EQUATION_RE = re.compile(r"\b([A-Za-z][\w\s/]{0,40}?)\s*=\s*[\w\d(){}\[\]+\-*/.]+")

# Cross-reference phrases pointing at another clause/annexure/circular.
_CROSS_REFERENCE_PHRASE_RE = re.compile(
    r"\b(read with|in terms of (clause|regulation|circular)|as specified in|pursuant to|"
    r"subject to the (provisions|conditions) (of|in)|as defined in|in accordance with clause|"
    r"annexure|schedule [ivx]+)\b",
    re.IGNORECASE,
)
# A clause-number-shaped token, e.g. "3.2.1", "4.1.b", "2.1.(c)" --
# counted separately from the clause's OWN leading number (the caller
# passes that in as `own_clause_number` to exclude it) so a clause
# doesn't get flagged as "cross-referencing" purely for containing its
# own number once in a header.
_CLAUSE_NUMBER_TOKEN_RE = re.compile(r"\b\d+\.\d+(?:\.[a-z\d]{1,3})?\b", re.IGNORECASE)

# 2+ distinct OTHER clause numbers, or an explicit cross-reference phrase,
# is what actually indicates "nested cross-references" per Requirement 1
# -- a single incidental clause-number mention (e.g. "unlike clause 2.1")
# is common in ordinary drafting and not by itself worth a specialist agent.
_MIN_OTHER_CLAUSE_REFERENCES = 2


def detect_complexity(text: str, own_clause_number: str | None = None) -> ComplexityFlags:
    math_signals: list[str] = []
    if match := _MATH_SYMBOL_RE.search(text):
        math_signals.append(f"math symbol: {match.group(0)!r}")
    if match := _MATH_PHRASE_RE.search(text):
        math_signals.append(f"formula phrase: {match.group(0)!r}")
    if match := _EQUATION_RE.search(text):
        math_signals.append(f"equation pattern: {match.group(0)!r}")

    cross_reference_signals: list[str] = []
    if match := _CROSS_REFERENCE_PHRASE_RE.search(text):
        cross_reference_signals.append(f"cross-reference phrase: {match.group(0)!r}")

    other_clause_numbers = {
        token for token in _CLAUSE_NUMBER_TOKEN_RE.findall(text) if token != (own_clause_number or "")
    }
    if len(other_clause_numbers) >= _MIN_OTHER_CLAUSE_REFERENCES:
        cross_reference_signals.append(f"{len(other_clause_numbers)} distinct other clause number(s) referenced: {sorted(other_clause_numbers)}")

    return ComplexityFlags(
        has_math_formulas=bool(math_signals),
        has_cross_references=bool(cross_reference_signals),
        math_signals=tuple(math_signals),
        cross_reference_signals=tuple(cross_reference_signals),
    )
