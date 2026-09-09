"""Compliance Case-Law Memory Agent subsystem.

Provides semantic indexing and tenant-isolated retrieval of approved HITL compliance
decisions to assist compliance officers during regulatory clause reviews.

Core interfaces:
- CaseLawMemoryAgent: Agent synthesizing current regulatory text, precedent, and guidance.
- CaseLawStore: Qdrant-backed vector storage with strict tenant partitioning.
- index_approved_hitl_review: Indexes resolved HITL reviews into precedent memory.
"""
from __future__ import annotations

from app.case_law.agent import CaseLawMemoryAgent
from app.case_law.indexer import index_approved_hitl_review
from app.case_law.models import (
    CaseLawAnalysisResult,
    PrecedentMatch,
    PrecedentQuery,
    PrecedentRecord,
)
from app.case_law.store import CaseLawStore, get_qdrant_client
from app.config import Settings, get_settings


def get_case_law_store(settings: Settings | None = None) -> CaseLawStore:
    return CaseLawStore(settings=settings or get_settings())


def get_case_law_agent(
    store: CaseLawStore | None = None, settings: Settings | None = None
) -> CaseLawMemoryAgent:
    settings = settings or get_settings()
    store = store or get_case_law_store(settings)
    return CaseLawMemoryAgent(store=store, settings=settings)


__all__ = [
    "CaseLawAnalysisResult",
    "CaseLawMemoryAgent",
    "CaseLawStore",
    "PrecedentMatch",
    "PrecedentQuery",
    "PrecedentRecord",
    "get_case_law_agent",
    "get_case_law_store",
    "get_qdrant_client",
    "index_approved_hitl_review",
]
