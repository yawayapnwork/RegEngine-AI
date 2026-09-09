# Ingestion Service

FastAPI service responsible for pulling SEBI circulars, extracting
layout-aware text via Unstructured (with Apache Tika and OCR fallbacks),
chunking clauses, and indexing embeddings into Qdrant.

Run locally: `uvicorn app.main:app --reload --port 8001`
