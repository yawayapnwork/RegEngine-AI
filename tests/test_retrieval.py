"""Tests for the hybrid Graph-RAG retrieval layer (app.retrieval):
clause-level supersession/amendment auto-detection, hybrid search
orchestration, and retrieval evaluation metrics.

The vector half is tested against a REAL embedded Qdrant instance
(`AsyncQdrantClient(location=":memory:")` -- no server process needed,
see app.vectorstore.qdrant_store) with REAL sentence-transformers
embeddings, so search relevance is genuinely exercised, not mocked.
The graph half uses the same `_FakeSession`/`_FakeResult` test-double
convention tests/test_graph.py already established for Neo4j, since no
live Neo4j instance is available in this sandbox.
"""
from __future__ import annotations

import pytest
from qdrant_client import AsyncQdrantClient, models

from app.config import get_settings
from app.graph.supersession_extractor import (
    DetectedSupersession,
    SupersessionRelationshipType,
    detect_supersessions,
)
from app.retrieval.evaluation import (
    LabeledQuery,
    context_precision,
    context_recall,
    evaluate_retriever,
    reciprocal_rank,
)
from app.retrieval.graph_queries import (
    dependency_expansion_query,
    supersession_chain_forward_query,
    supersession_chain_reverse_query,
)
from app.retrieval.hybrid_search import hybrid_search
from app.vectorstore.qdrant_store import _chunk_payload, _point_id_for, ensure_collection, vector_search
from app.vectorstore.embeddings import embed_texts
from app.models import ClauseChunk


class TestSupersessionExtractor:
    def test_no_supersession_language_returns_empty_list(self) -> None:
        text = "Every stock broker shall maintain upfront margin of not less than 20% of the transaction value."
        assert detect_supersessions(text) == []

    def test_detects_supersession_with_formal_circular_number(self) -> None:
        text = (
            "Clause 3.2 hereby supersedes Clause 1.1 of the Master Circular SEBI/HO/MIRSD/2020/01, "
            "effective immediately."
        )
        results = detect_supersessions(text)
        assert len(results) == 1
        d = results[0]
        assert isinstance(d, DetectedSupersession)
        assert d.relationship_type == SupersessionRelationshipType.SUPERSEDES
        assert d.target_clause_number == "1.1"
        assert d.target_circular_number == "SEBI/HO/MIRSD/2020/01"
        assert "supersedes" in d.basis_text.lower()

    def test_detects_amendment_with_title_only_reference(self) -> None:
        text = "This clause modifies Clause 4.5 of the Master Circular on Margin Requirements."
        results = detect_supersessions(text)
        assert len(results) == 1
        d = results[0]
        assert d.relationship_type == SupersessionRelationshipType.AMENDS
        assert d.target_clause_number == "4.5"
        assert d.target_circular_number is None
        assert "margin requirements" in d.target_circular_reference.lower()

    def test_detects_both_supersession_and_amendment_in_same_text(self) -> None:
        text = (
            "Clause 3.2 hereby supersedes Clause 1.1 of the Master Circular SEBI/HO/MIRSD/2020/01. "
            "It also modifies Clause 2.2 of the Master Circular SEBI/HO/MIRSD/2019/02."
        )
        results = detect_supersessions(text)
        assert {d.relationship_type for d in results} == {
            SupersessionRelationshipType.SUPERSEDES,
            SupersessionRelationshipType.AMENDS,
        }


class TestGraphQueries:
    def test_dependency_expansion_query_interpolates_max_depth(self) -> None:
        query = dependency_expansion_query(3)
        assert "*1..3]" in query
        assert "$clause_ids" in query and "$max_hits" in query

    def test_supersession_chain_queries_interpolate_max_depth(self) -> None:
        assert "*1..2]" in supersession_chain_forward_query(2)
        assert "*1..4]" in supersession_chain_reverse_query(4)


class _FakeResult:
    def __init__(self, records: list[dict]) -> None:
        self._records = records

    async def data(self) -> list[dict]:
        return self._records


class _FakeGraphSession:
    def __init__(self, records: list[dict]) -> None:
        self._records = records
        self.calls: list[tuple[str, dict]] = []

    async def run(self, query: str, **params):
        self.calls.append((query, params))
        return _FakeResult(self._records)


@pytest.mark.asyncio
class TestHybridSearch:
    async def test_vector_only_search_returns_real_ranked_hits(self) -> None:
        """Real embedded Qdrant + real embeddings: two clauses about
        unrelated topics, a query closer to one than the other must rank
        it first."""
        settings = get_settings()
        client = AsyncQdrantClient(location=":memory:")
        try:
            await ensure_collection(client, settings)

            chunks = [
                ClauseChunk(
                    chunk_id="c1", sha256="a" * 64, text="Every stock broker shall maintain upfront margin of not less than 20% of transaction value.",
                    clause_number="4.2.b", circular_number="SEBI/HO/MIRSD/2024/100",
                ),
                ClauseChunk(
                    chunk_id="c2", sha256="b" * 64, text="Mutual fund distributors must complete continuing education requirements every three years.",
                    clause_number="7.1", circular_number="SEBI/HO/IMD/2023/050",
                ),
            ]
            vectors = await embed_texts([c.text for c in chunks], settings)
            points = [
                models.PointStruct(id=_point_id_for(c.sha256), vector=v, payload=_chunk_payload(c))
                for c, v in zip(chunks, vectors, strict=True)
            ]
            await client.upsert(collection_name=settings.qdrant_collection, points=points, wait=True)

            hits = await vector_search("What is the upfront margin requirement for brokers?", settings, top_k=2, client=client)
            assert hits[0]["clause_number"] == "4.2.b"

            results = await hybrid_search("What is the upfront margin requirement for brokers?", settings, top_k=2, qdrant_client=client)
            assert results[0].source == "vector"
            assert results[0].clause_number == "4.2.b"
            assert results[0].clause_id == "a" * 64 + ":4.2.b"
        finally:
            await client.close()

    async def test_graph_expansion_appends_dependency_hits_without_duplicating_vector_hits(self) -> None:
        settings = get_settings()
        client = AsyncQdrantClient(location=":memory:")
        try:
            await ensure_collection(client, settings)
            chunk = ClauseChunk(
                chunk_id="c1", sha256="a" * 64, text="Every stock broker shall maintain upfront margin of not less than 20%.",
                clause_number="4.2.b", circular_number="SEBI/HO/MIRSD/2024/100",
            )
            [vector] = await embed_texts([chunk.text], settings)
            point = models.PointStruct(id=_point_id_for(chunk.sha256), vector=vector, payload=_chunk_payload(chunk))
            await client.upsert(collection_name=settings.qdrant_collection, points=[point], wait=True)

            graph_records = [
                {
                    "clause_id": "b" * 64 + ":1.1", "clause_number": "1.1", "circular_number": "SEBI/HO/MIRSD/2020/01",
                    "is_stub": False, "source_clause_id": "a" * 64 + ":4.2.b",
                    "relationship_types": ["SUPERSEDES"], "hop_count": 1, "any_auto_detected": True,
                },
                # Same clause_id as the vector hit itself -- must be filtered, not duplicated.
                {
                    "clause_id": "a" * 64 + ":4.2.b", "clause_number": "4.2.b", "circular_number": "SEBI/HO/MIRSD/2024/100",
                    "is_stub": False, "source_clause_id": "a" * 64 + ":4.2.b",
                    "relationship_types": ["SUPERSEDES"], "hop_count": 1, "any_auto_detected": False,
                },
            ]
            fake_session = _FakeGraphSession(graph_records)

            results = await hybrid_search(
                "upfront margin requirement", settings, top_k=1, qdrant_client=client, neo4j_session=fake_session,
            )

            assert len(results) == 2
            assert results[0].source == "vector"
            graph_hit = results[1]
            assert graph_hit.source == "graph"
            assert graph_hit.clause_id == "b" * 64 + ":1.1"
            assert graph_hit.relationship_types == ["SUPERSEDES"]
            assert graph_hit.any_auto_detected is True
            assert fake_session.calls, "expected the graph session to be queried"
        finally:
            await client.close()

    async def test_no_neo4j_session_returns_vector_only_results(self) -> None:
        settings = get_settings()
        client = AsyncQdrantClient(location=":memory:")
        try:
            await ensure_collection(client, settings)
            chunk = ClauseChunk(chunk_id="c1", sha256="a" * 64, text="Some clause text.", clause_number="1.1", circular_number="X/1")
            [vector] = await embed_texts([chunk.text], settings)
            point = models.PointStruct(id=_point_id_for(chunk.sha256), vector=vector, payload=_chunk_payload(chunk))
            await client.upsert(collection_name=settings.qdrant_collection, points=[point], wait=True)

            results = await hybrid_search("Some clause text", settings, top_k=1, qdrant_client=client)
            assert all(r.source == "vector" for r in results)
        finally:
            await client.close()


class TestEvaluationMetrics:
    def test_context_precision_and_recall(self) -> None:
        retrieved = ["a", "b", "c"]
        relevant = frozenset({"a", "c", "d"})
        assert context_precision(retrieved, relevant) == pytest.approx(2 / 3)
        assert context_recall(retrieved, relevant) == pytest.approx(2 / 3)

    def test_precision_and_recall_on_empty_retrieval(self) -> None:
        assert context_precision([], frozenset({"a"})) == 0.0
        assert context_recall([], frozenset({"a"})) == 0.0

    def test_recall_trivially_one_when_no_relevant_docs_exist(self) -> None:
        assert context_recall(["a"], frozenset()) == 1.0

    def test_reciprocal_rank_of_first_relevant_hit(self) -> None:
        assert reciprocal_rank(["x", "y", "z"], frozenset({"z"})) == pytest.approx(1 / 3)
        assert reciprocal_rank(["z", "y"], frozenset({"z"})) == pytest.approx(1.0)
        assert reciprocal_rank(["x", "y"], frozenset({"z"})) == 0.0

    @pytest.mark.asyncio
    async def test_evaluate_retriever_computes_mean_metrics_across_queries(self) -> None:
        queries = [
            LabeledQuery(query_id="q1", query_text="query one", relevant_clause_ids=frozenset({"a"})),
            LabeledQuery(query_id="q2", query_text="query two", relevant_clause_ids=frozenset({"z"})),
        ]

        async def fake_retrieve(query_text: str) -> list[str]:
            return ["a", "b"] if query_text == "query one" else ["x", "y"]

        report = await evaluate_retriever(queries, fake_retrieve)
        assert report.per_query[0].context_precision == pytest.approx(0.5)
        assert report.per_query[1].context_precision == 0.0
        assert report.mean_context_precision == pytest.approx(0.25)
        assert report.mean_reciprocal_rank == pytest.approx(0.5)
