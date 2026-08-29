"""Requirement 1 -- Dynamic Indexing: hybrid search orchestration.

Runs Qdrant dense-vector search first (finds clauses similar in
MEANING to the query), then expands outward from those hits across the
Neo4j knowledge graph's SUPERSEDES/AMENDS/REFERENCES edges (finds
clauses connected by an explicit regulatory DEPENDENCY the vector space
alone won't surface) -- e.g. a query about "upfront margin
requirements" vector-matches the current Master Circular clause, and
the graph hop then pulls in the older circular clause it superseded,
even though that older clause's wording may have little embedding
similarity to the query.

Gated behind `settings.hybrid_retrieval_enabled`; graph expansion is
skipped (never errors) when a caller doesn't supply a Neo4j session,
so this degrades to plain vector search rather than failing outright
in a deployment that has `neo4j_sync_enabled=False`.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Literal

from neo4j import AsyncSession
from qdrant_client import AsyncQdrantClient

from app.config import Settings
from app.retrieval.graph_queries import dependency_expansion_query
from app.vectorstore.qdrant_store import vector_search

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RetrievedClause:
    clause_id: str | None
    clause_number: str | None
    circular_number: str | None
    text: str | None
    source: Literal["vector", "graph"]
    score: float | None = None  # cosine similarity -- vector hits only
    hop_count: int | None = None  # graph hits only
    relationship_types: list[str] = field(default_factory=list)  # graph hits only, e.g. ["SUPERSEDES"]
    via_clause_id: str | None = None  # the vector hit this graph hit was reached from
    is_stub: bool = False  # graph hits only -- see app.graph.sync's stub-node convention
    any_auto_detected: bool = False  # true if any edge on the path to this hit is an auto-detected supersession


def _clause_id(payload: dict) -> str | None:
    """Qdrant payload doesn't carry `clause_id` directly (see
    app.vectorstore.qdrant_store._chunk_payload) -- it's reconstructible
    from `sha256`/`clause_number` per app.agents.schemas.ExtractedComplianceRule.rule_id's
    documented "<source_sha256>:<clause_number>" format, letting a
    vector hit's clause_id line up with the same clause's graph node."""
    sha256 = payload.get("sha256")
    clause_number = payload.get("clause_number")
    if not sha256 or not clause_number:
        return None
    return f"{sha256}:{clause_number}"


async def hybrid_search(
    query_text: str,
    settings: Settings,
    *,
    top_k: int = 5,
    qdrant_client: AsyncQdrantClient | None = None,
    neo4j_session: AsyncSession | None = None,
) -> list[RetrievedClause]:
    vector_hits = await vector_search(query_text, settings, top_k=top_k, client=qdrant_client)

    vector_results = [
        RetrievedClause(
            clause_id=_clause_id(hit),
            clause_number=hit.get("clause_number"),
            circular_number=hit.get("circular_number"),
            text=hit.get("text"),
            source="vector",
            score=hit.get("score"),
        )
        for hit in vector_hits
    ]

    if neo4j_session is None:
        return vector_results

    seed_clause_ids = [r.clause_id for r in vector_results if r.clause_id]
    if not seed_clause_ids:
        logger.debug("hybrid_search: no clause_id-bearing vector hits to expand from; returning vector-only results.")
        return vector_results

    already_seen = {r.clause_id for r in vector_results}
    query = dependency_expansion_query(settings.hybrid_retrieval_graph_depth)
    result = await neo4j_session.run(query, clause_ids=seed_clause_ids, max_hits=settings.hybrid_retrieval_max_graph_hits)
    records = await result.data()

    graph_results = []
    for record in records:
        clause_id = record.get("clause_id")
        if clause_id in already_seen:
            continue
        already_seen.add(clause_id)
        graph_results.append(
            RetrievedClause(
                clause_id=clause_id,
                clause_number=record.get("clause_number"),
                circular_number=record.get("circular_number"),
                text=None,  # graph nodes carry no clause text -- app.graph.sync only stores identifying properties, never the raw clause body (that lives solely in Qdrant's payload, keyed by sha256+clause_number, not by clause_id -- see app.vectorstore.qdrant_store._chunk_payload); a caller wanting text for a graph-only hit re-queries Qdrant with a circular_number/clause_number filter.
                source="graph",
                hop_count=record.get("hop_count"),
                relationship_types=record.get("relationship_types") or [],
                via_clause_id=record.get("source_clause_id"),
                is_stub=bool(record.get("is_stub")),
                any_auto_detected=bool(record.get("any_auto_detected")),
            )
        )

    return vector_results + graph_results
