"""Hybrid Graph-RAG retrieval: combines Qdrant dense-vector search
(app.vectorstore) with Neo4j knowledge-graph traversal (app.graph) so a
query returns not just the clauses closest in embedding space but also
the clauses those hits supersede, are amended by, or reference --
relationships pure vector similarity cannot see (two clauses that
replace each other often read nothing alike).

Gated behind `settings.hybrid_retrieval_enabled` (see app.config) --
off by default, and additive: `app.vectorstore.qdrant_store`'s existing
vector-only search is completely unaffected by this package.
"""
