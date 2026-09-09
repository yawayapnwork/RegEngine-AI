"""Qdrant-backed vector storage and search for compliance case-law precedents.

Enforces:
- Deterministic point IDs derived from precedent ID.
- Strict tenant/entity boundary filtering on all queries.
- Provenance preservation across all indexed records.
"""
from __future__ import annotations

import datetime as dt
import logging
import uuid
from typing import Any

from qdrant_client import AsyncQdrantClient, models

from app.case_law.models import PrecedentMatch, PrecedentQuery, PrecedentRecord
from app.config import Settings, get_settings
from app.vectorstore.embeddings import embed_texts

logger = logging.getLogger(__name__)

_PRECEDENT_NAMESPACE = uuid.UUID("9c4b1d6f-7e8a-4c2b-9e1f-3a5b7c8d9e0f")


def _point_id_for(precedent_id: str) -> str:
    return str(uuid.uuid5(_PRECEDENT_NAMESPACE, precedent_id))


def get_qdrant_client(settings: Settings | None = None) -> AsyncQdrantClient:
    settings = settings or get_settings()
    return AsyncQdrantClient(
        url=settings.qdrant_url,
        api_key=settings.qdrant_api_key,
        timeout=settings.qdrant_timeout_seconds,
    )


class CaseLawStore:
    """Manages indexing and tenant-isolated retrieval of approved compliance precedents in Qdrant."""

    def __init__(self, settings: Settings | None = None, client: AsyncQdrantClient | None = None) -> None:
        self.settings = settings or get_settings()
        self._client = client

    def _get_client(self) -> AsyncQdrantClient:
        if self._client is not None:
            return self._client
        return get_qdrant_client(self.settings)

    async def ensure_collection(self, recreate: bool = False) -> None:
        """Ensures that the case-law precedents collection and payload indexes exist."""
        client = self._get_client()
        collection_name = self.settings.case_law_qdrant_collection
        exists = await client.collection_exists(collection_name)
        if exists and recreate:
            await client.delete_collection(collection_name)
            exists = False

        if not exists:
            await client.create_collection(
                collection_name=collection_name,
                vectors_config=models.VectorParams(
                    size=self.settings.embedding_dim,
                    distance=models.Distance.COSINE,
                ),
            )
            # Create payload indexes to support tenant isolation and audit queries
            for field_name, schema in (
                ("tenant_id", models.PayloadSchemaType.KEYWORD),
                ("is_shared", models.PayloadSchemaType.BOOL),
                ("clause_sha256", models.PayloadSchemaType.KEYWORD),
                ("circular_number", models.PayloadSchemaType.KEYWORD),
                ("approval_timestamp", models.PayloadSchemaType.DATETIME),
                ("decision", models.PayloadSchemaType.KEYWORD),
            ):
                await client.create_payload_index(
                    collection_name=collection_name,
                    field_name=field_name,
                    field_schema=schema,
                )
            logger.info("Created case-law memory collection '%s'", collection_name)

    async def index_precedent(self, precedent: PrecedentRecord) -> str:
        """Indexes an approved precedent into Qdrant.

        Safety gates:
        - Refuses to index decisions other than APPROVED / RESOLVED.
        - Enforces non-empty tenant_id and clause text.
        """
        if precedent.decision.upper() not in ("APPROVED", "RESOLVED"):
            raise ValueError(
                f"Cannot index decision '{precedent.decision}' as authoritative precedent. "
                "Only APPROVED or RESOLVED HITL decisions may enter case-law memory."
            )
        if not precedent.tenant_id or not precedent.tenant_id.strip():
            raise ValueError("Precedent must specify a valid tenant_id for tenant isolation.")
        if not precedent.original_clause_text or not precedent.original_clause_text.strip():
            raise ValueError("Precedent must contain non-empty original_clause_text.")

        client = self._get_client()
        await self.ensure_collection(recreate=False)

        # Generate embedding vector
        [vector] = await embed_texts([precedent.original_clause_text], self.settings)

        payload = precedent.model_dump(mode="json")
        point_id = _point_id_for(precedent.precedent_id)

        point = models.PointStruct(
            id=point_id,
            vector=vector,
            payload=payload,
        )

        await client.upsert(
            collection_name=self.settings.case_law_qdrant_collection,
            points=[point],
            wait=True,
        )
        logger.info(
            "Indexed precedent '%s' (review_id=%s, tenant_id=%s, circular=%s)",
            precedent.precedent_id,
            precedent.review_id,
            precedent.tenant_id,
            precedent.circular_number,
        )
        return point_id

    async def search_precedents(self, query: PrecedentQuery) -> list[PrecedentMatch]:
        """Retrieves top-k semantically similar approved precedents under strict tenant isolation.

        Tenant Isolation Guarantee:
        - Returns ONLY records matching query.tenant_id, or shared baseline records if allow_shared=True.
        - Under NO circumstances can tenant A see tenant B's private precedents.
        """
        if not query.tenant_id or not query.tenant_id.strip():
            raise ValueError("Tenant ID is required for case-law search to enforce isolation.")

        client = self._get_client()
        collection_name = self.settings.case_law_qdrant_collection
        if not await client.collection_exists(collection_name):
            return []

        # Generate embedding for query text
        [vector] = await embed_texts([query.query_text], self.settings)

        # Tenant boundary filter
        tenant_conditions: list[Any] = [
            models.FieldCondition(key="tenant_id", match=models.MatchValue(value=query.tenant_id))
        ]
        if query.allow_shared:
            tenant_conditions.append(models.FieldCondition(key="is_shared", match=models.MatchValue(value=True)))

        must_conditions: list[Any] = [
            models.Filter(should=tenant_conditions),
            models.FieldCondition(key="decision", match=models.MatchValue(value="APPROVED")),
        ]

        if query.max_age_days is not None:
            cutoff_dt = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=query.max_age_days)
            must_conditions.append(
                models.FieldCondition(
                    key="approval_timestamp",
                    range=models.DatetimeRange(gte=cutoff_dt),
                )
            )

        search_filter = models.Filter(must=must_conditions)

        results = await client.query_points(
            collection_name=collection_name,
            query=vector,
            limit=query.top_k,
            query_filter=search_filter,
            score_threshold=query.min_similarity,
        )

        matches: list[PrecedentMatch] = []
        for point in results.points:
            try:
                rec = PrecedentRecord.model_validate(point.payload)
                # Format a crisp, auditable provenance summary
                ts_str = rec.approval_timestamp.strftime("%Y-%m-%d %H:%M UTC") if hasattr(rec.approval_timestamp, "strftime") else str(rec.approval_timestamp)
                prov = (
                    f"Circular {rec.circular_number} | Clause {rec.clause_number or 'unscoped'} "
                    f"(SHA: {rec.clause_sha256[:8]}...) | Approved by: {rec.compliance_officer_id or 'Officer'} "
                    f"at {ts_str} | Rule v{rec.approved_rule_version or 1}"
                )
                matches.append(
                    PrecedentMatch(
                        precedent=rec,
                        similarity_score=float(point.score or 0.0),
                        provenance_summary=prov,
                    )
                )
            except Exception as err:
                logger.warning("Failed to deserialize precedent point %s: %s", point.id, err)

        # Sort matches by similarity score descending
        matches.sort(key=lambda m: m.similarity_score, reverse=True)
        return matches

    async def delete_precedent(self, precedent_id: str) -> None:
        """Deletes a precedent by its precedent ID."""
        client = self._get_client()
        collection_name = self.settings.case_law_qdrant_collection
        if await client.collection_exists(collection_name):
            point_id = _point_id_for(precedent_id)
            await client.delete(
                collection_name=collection_name,
                points_selector=models.PointIdsList(points=[point_id]),
                wait=True,
            )
