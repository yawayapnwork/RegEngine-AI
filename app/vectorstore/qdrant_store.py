"""Qdrant indexing for clause chunks.

Uses the official async Qdrant client. Point IDs are deterministic (derived
from the chunk's SHA-256) so re-indexing the same circular is idempotent —
re-running the pipeline on an unchanged clause upserts to the same point
rather than creating a duplicate.
"""
from __future__ import annotations

import logging
import uuid

from qdrant_client import AsyncQdrantClient, models

from app.config import Settings
from app.models import ClauseChunk, IndexResponse
from app.parsing.exceptions import IndexingError
from app.vectorstore.embeddings import embed_texts

logger = logging.getLogger(__name__)

# Deterministic namespace for deriving point IDs from chunk SHA-256 hashes.
_POINT_ID_NAMESPACE = uuid.UUID("6f1b2f1e-4b9a-4a4a-8a6a-2f6b1c9d5e3a")


def _point_id_for(sha256_hex: str) -> str:
    return str(uuid.uuid5(_POINT_ID_NAMESPACE, sha256_hex))


def _get_client(settings: Settings) -> AsyncQdrantClient:
    return AsyncQdrantClient(
        url=settings.qdrant_url,
        api_key=settings.qdrant_api_key,
        timeout=settings.qdrant_timeout_seconds,
    )


async def ensure_collection(client: AsyncQdrantClient, settings: Settings, recreate: bool = False) -> None:
    try:
        exists = await client.collection_exists(settings.qdrant_collection)
        if exists and recreate:
            await client.delete_collection(settings.qdrant_collection)
            exists = False
        if not exists:
            await client.create_collection(
                collection_name=settings.qdrant_collection,
                vectors_config=models.VectorParams(
                    size=settings.embedding_dim,
                    distance=models.Distance.COSINE,
                ),
            )
            # Payload indexes accelerate metadata-filtered search
            # (e.g. "clauses from circular X issued after date Y").
            for field_name, schema in (
                ("circular_number", models.PayloadSchemaType.KEYWORD),
                ("clause_number", models.PayloadSchemaType.KEYWORD),
                ("sha256", models.PayloadSchemaType.KEYWORD),
                ("issue_date", models.PayloadSchemaType.DATETIME),
            ):
                await client.create_payload_index(
                    collection_name=settings.qdrant_collection,
                    field_name=field_name,
                    field_schema=schema,
                )
    except Exception as exc:  # noqa: BLE001
        raise IndexingError(f"Failed to ensure Qdrant collection '{settings.qdrant_collection}': {exc!r}") from exc


def _chunk_payload(chunk: ClauseChunk) -> dict:
    return {
        "chunk_id": chunk.chunk_id,
        "sha256": chunk.sha256,
        "text": chunk.text,
        "clause_number": chunk.clause_number,
        "section_path": chunk.section_path,
        "section_title": chunk.section_title,
        "page_start": chunk.page_start,
        "page_end": chunk.page_end,
        "footnotes": chunk.footnotes,
        "contains_table": chunk.contains_table,
        "circular_number": chunk.circular_number,
        "issue_date": chunk.issue_date.isoformat() if chunk.issue_date else None,
        "source_filename": chunk.source_filename,
    }


async def index_chunks(
    chunks: list[ClauseChunk],
    settings: Settings,
    *,
    recreate_collection: bool = False,
) -> IndexResponse:
    if not chunks:
        return IndexResponse(collection=settings.qdrant_collection, upserted=0, skipped_duplicates=0)

    client = _get_client(settings)
    try:
        await ensure_collection(client, settings, recreate=recreate_collection)

        # Dedup within this batch by SHA-256 before embedding (avoids paying
        # for embeddings on identical clauses, e.g. re-uploaded circulars).
        seen: dict[str, ClauseChunk] = {}
        skipped = 0
        for c in chunks:
            if c.sha256 in seen:
                skipped += 1
                continue
            seen[c.sha256] = c
        unique_chunks = list(seen.values())

        vectors = await embed_texts([c.text for c in unique_chunks], settings)
        if len(vectors) != len(unique_chunks):
            raise IndexingError("Embedding count does not match chunk count; refusing to index.")

        points = [
            models.PointStruct(
                id=_point_id_for(chunk.sha256),
                vector=vector,
                payload=_chunk_payload(chunk),
            )
            for chunk, vector in zip(unique_chunks, vectors, strict=True)
        ]

        batch_size = settings.qdrant_upsert_batch_size
        upserted = 0
        for i in range(0, len(points), batch_size):
            batch = points[i : i + batch_size]
            try:
                await client.upsert(collection_name=settings.qdrant_collection, points=batch, wait=True)
                upserted += len(batch)
            except Exception as exc:  # noqa: BLE001
                raise IndexingError(
                    f"Qdrant upsert failed on batch {i // batch_size} ({len(batch)} points): {exc!r}"
                ) from exc

        return IndexResponse(collection=settings.qdrant_collection, upserted=upserted, skipped_duplicates=skipped)
    finally:
        await client.close()
