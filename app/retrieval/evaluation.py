"""Requirement 3 -- Semantic Retrieval Evaluation: context precision,
recall, and Mean Reciprocal Rank (MRR) for the hybrid retriever, against
a labeled set of multi-document SEBI regulatory queries (a query whose
correct answer legitimately spans more than one circular -- e.g. "what
is the CURRENT upfront margin requirement" needs both the amending
circular clause and enough of the superseded one to know what changed).

Pure, deterministic metric functions operate on plain clause_id lists so
they're testable with hand-built fixtures and no live Qdrant/Neo4j
(`evaluate_retriever` is the only piece that calls a real retriever,
and it accepts any async callable with `hybrid_search`'s signature --
tests can pass a stub).
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

RetrieverFn = Callable[[str], Awaitable[list[str]]]  # query_text -> ordered clause_ids


@dataclass(frozen=True)
class LabeledQuery:
    query_id: str
    query_text: str
    relevant_clause_ids: frozenset[str]  # ground-truth relevant set, from a human-labeled test set


@dataclass(frozen=True)
class QueryEvaluationResult:
    query_id: str
    query_text: str
    retrieved_clause_ids: list[str]
    context_precision: float
    context_recall: float
    reciprocal_rank: float


@dataclass(frozen=True)
class BenchmarkReport:
    per_query: list[QueryEvaluationResult]
    mean_context_precision: float
    mean_context_recall: float
    mean_reciprocal_rank: float  # MRR across the whole query set


def context_precision(retrieved_clause_ids: list[str], relevant_clause_ids: frozenset[str]) -> float:
    """Of what was retrieved, what fraction was actually relevant.
    Undefined (returns 0.0, not NaN) when nothing was retrieved -- a
    retriever that returns nothing for a query with a known answer is
    scored as precision 0, not excused from scoring."""
    if not retrieved_clause_ids:
        return 0.0
    relevant_retrieved = sum(1 for cid in retrieved_clause_ids if cid in relevant_clause_ids)
    return relevant_retrieved / len(retrieved_clause_ids)


def context_recall(retrieved_clause_ids: list[str], relevant_clause_ids: frozenset[str]) -> float:
    """Of everything actually relevant, what fraction was retrieved.
    A query with an empty ground-truth set (shouldn't appear in a real
    labeled set, but defensively) recalls trivially 1.0 -- nothing to
    miss."""
    if not relevant_clause_ids:
        return 1.0
    found = sum(1 for cid in relevant_clause_ids if cid in retrieved_clause_ids)
    return found / len(relevant_clause_ids)


def reciprocal_rank(retrieved_clause_ids: list[str], relevant_clause_ids: frozenset[str]) -> float:
    """1/rank of the FIRST relevant result (standard MRR definition),
    0.0 if no relevant result was retrieved at all."""
    for rank, cid in enumerate(retrieved_clause_ids, start=1):
        if cid in relevant_clause_ids:
            return 1.0 / rank
    return 0.0


def evaluate_single_query(query: LabeledQuery, retrieved_clause_ids: list[str]) -> QueryEvaluationResult:
    return QueryEvaluationResult(
        query_id=query.query_id,
        query_text=query.query_text,
        retrieved_clause_ids=retrieved_clause_ids,
        context_precision=context_precision(retrieved_clause_ids, query.relevant_clause_ids),
        context_recall=context_recall(retrieved_clause_ids, query.relevant_clause_ids),
        reciprocal_rank=reciprocal_rank(retrieved_clause_ids, query.relevant_clause_ids),
    )


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


async def evaluate_retriever(labeled_queries: list[LabeledQuery], retrieve: RetrieverFn) -> BenchmarkReport:
    """`retrieve` is called once per labeled query, in order, and must
    return an ORDERED list of clause_ids (best match first) -- the same
    contract `app.retrieval.hybrid_search.hybrid_search`'s results
    naturally satisfy once mapped to `.clause_id` for the entries that
    have one (graph hits are still meaningfully ranked after vector
    hits, per that function's own return order)."""
    per_query = []
    for query in labeled_queries:
        retrieved = await retrieve(query.query_text)
        per_query.append(evaluate_single_query(query, retrieved))

    return BenchmarkReport(
        per_query=per_query,
        mean_context_precision=_mean([r.context_precision for r in per_query]),
        mean_context_recall=_mean([r.context_recall for r in per_query]),
        mean_reciprocal_rank=_mean([r.reciprocal_rank for r in per_query]),
    )
