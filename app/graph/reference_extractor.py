"""Extracts cross-referenced clause numbers from clause text, to populate
`(:Clause)-[:REFERENCES]->(:Clause)` edges. Deliberately reuses
`app.agents.graph.complexity_router`'s clause-number-token regex (the
SAME pattern already validated there for detecting "this clause has
nested cross-references" during dynamic agent routing) rather than
maintaining a second, potentially-drifting copy of the same regex --
"does this clause reference other clauses" is one fact about the text,
consulted by two different subsystems (agent routing, graph population).
"""
from __future__ import annotations

from app.agents.graph.complexity_router import CLAUSE_NUMBER_TOKEN_RE


def extract_referenced_clause_numbers(text: str, own_clause_number: str | None = None) -> list[str]:
    """Returns every distinct clause-number-shaped token in `text` other
    than the clause's own number, in first-appearance order. These are
    CANDIDATE references -- app.graph.sync creates a stub `:Clause` node
    for a referenced number that doesn't already exist in the graph
    (rather than skipping the edge), since the referenced clause may
    belong to a circular not yet ingested; the stub is filled in normally
    if/when that circular IS later ingested (MERGE, not CREATE)."""
    seen: list[str] = []
    for token in CLAUSE_NUMBER_TOKEN_RE.findall(text):
        if token == (own_clause_number or ""):
            continue
        if token not in seen:
            seen.append(token)
    return seen
