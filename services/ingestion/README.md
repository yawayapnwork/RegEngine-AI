# Ingestion Service

FastAPI service responsible for pulling SEBI circulars, extracting
layout-aware text via Apache Tika, chunking clauses, and indexing
embeddings into Qdrant.

Run locally: `uvicorn app.main:app --reload --port 8001`
