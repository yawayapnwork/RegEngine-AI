#!/usr/bin/env python
"""regengine-eval-retrieval -- hybrid Graph-RAG retriever benchmark CLI.

Runs app.retrieval.hybrid_search.hybrid_search against a JSON-labeled
set of multi-document SEBI regulatory queries (see
`--labeled-queries`'s docstring below for the file format) and reports
context precision, context recall, and Mean Reciprocal Rank (MRR) --
Requirement 3's "Semantic Retrieval Evaluation" deliverable.

Requires a running Qdrant collection (already populated via
`regengine-cli.py`'s ingest pipeline) and, for the graph-expansion half
of the score, `settings.neo4j_sync_enabled=True` with a reachable
Neo4j instance -- pass `--vector-only` to benchmark the plain Qdrant
retriever alone (e.g. as a baseline to compare against the hybrid
score).

Usage:
    python regengine-eval-retrieval.py run --labeled-queries eval/sebi_retrieval_queries.json
    python regengine-eval-retrieval.py run --labeled-queries eval/sebi_retrieval_queries.json --vector-only --top-k 10
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from app.config import get_settings
from app.graph.client import session as open_neo4j_session
from app.retrieval.evaluation import LabeledQuery, evaluate_retriever
from app.retrieval.hybrid_search import hybrid_search

app = typer.Typer(add_completion=False)
console = Console()


def _load_labeled_queries(path: Path) -> list[LabeledQuery]:
    """Expects a JSON array of objects:
    `{"query_id": "...", "query_text": "...", "relevant_clause_ids": ["<sha256>:<clause_number>", ...]}`
    -- `relevant_clause_ids` in the same `"<source_sha256>:<clause_number>"`
    format `app.agents.schemas.ExtractedComplianceRule.rule_id` documents,
    hand-labeled by a compliance reviewer against a real ingested corpus."""
    records = json.loads(path.read_text(encoding="utf-8"))
    return [
        LabeledQuery(
            query_id=r["query_id"],
            query_text=r["query_text"],
            relevant_clause_ids=frozenset(r["relevant_clause_ids"]),
        )
        for r in records
    ]


@app.command()
def run(
    labeled_queries: Path = typer.Option(..., "--labeled-queries", exists=True, help="JSON file of labeled queries."),
    top_k: int = typer.Option(5, "--top-k", help="Vector search top_k passed to hybrid_search."),
    vector_only: bool = typer.Option(False, "--vector-only", help="Skip graph expansion; benchmark plain vector search."),
) -> None:
    settings = get_settings()
    queries = _load_labeled_queries(labeled_queries)
    console.print(f"[bold]Loaded {len(queries)} labeled queries from {labeled_queries}[/bold]")

    async def _run() -> None:
        if vector_only:
            async def retrieve(query_text: str) -> list[str]:
                hits = await hybrid_search(query_text, settings, top_k=top_k)
                return [r.clause_id for r in hits if r.clause_id]

            report = await evaluate_retriever(queries, retrieve)
        else:
            async with open_neo4j_session(settings) as neo4j_sess:
                async def retrieve(query_text: str) -> list[str]:
                    hits = await hybrid_search(query_text, settings, top_k=top_k, neo4j_session=neo4j_sess)
                    return [r.clause_id for r in hits if r.clause_id]

                report = await evaluate_retriever(queries, retrieve)

        table = Table(title="Per-query retrieval scores")
        table.add_column("Query")
        table.add_column("Precision", justify="right")
        table.add_column("Recall", justify="right")
        table.add_column("Reciprocal Rank", justify="right")
        for r in report.per_query:
            table.add_row(r.query_text[:60], f"{r.context_precision:.2f}", f"{r.context_recall:.2f}", f"{r.reciprocal_rank:.2f}")
        console.print(table)

        console.print(
            f"\n[bold]Mean context precision:[/bold] {report.mean_context_precision:.3f}   "
            f"[bold]Mean context recall:[/bold] {report.mean_context_recall:.3f}   "
            f"[bold]MRR:[/bold] {report.mean_reciprocal_rank:.3f}"
        )

    asyncio.run(_run())


if __name__ == "__main__":
    app()
