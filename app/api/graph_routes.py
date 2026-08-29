"""Visual Analysis API (Requirement 3): graph-structure endpoints for
interactive rendering on the compliance dashboard, plus the conflict/gap
detection endpoints (Requirement 2) that back the dashboard's "known
issues" view.

Restricted to Compliance_Officer / System_Admin -- this is knowledge-
graph exploration/audit tooling, not something a Broker_API_Client needs.
Returns 503 (not 500) when `settings.neo4j_sync_enabled` is off, matching
app.api.sandbox_routes' precedent for a feature that can be disabled
without a code deploy.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status

from app.config import Settings, get_settings
from app.graph import client as graph_client
from app.graph.conflict_detection import (
    ConflictDetectionReport,
    build_conflict_report,
    write_conflict_edges,
)
from app.graph.subgraph_queries import CIRCULAR_SUBGRAPH, CONFLICTS_SUBGRAPH, ENTITY_SUBGRAPH
from app.graph.visualization import GraphVisualization, build_visualization_from_records
from app.security.dependencies import require_roles
from app.security.models import Role

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/graph", tags=["Legal Knowledge Graph"])

_ALLOWED = require_roles(Role.COMPLIANCE_OFFICER, Role.SYSTEM_ADMIN)


def _require_enabled(settings: Settings) -> None:
    if not settings.neo4j_sync_enabled:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="The legal knowledge graph is not enabled on this deployment.")


async def _run_subgraph_query(settings: Settings, query: str, **params) -> GraphVisualization:
    async with graph_client.session(settings) as session:
        result = await session.run(query, **params)
        records = await result.data()
    return build_visualization_from_records(records)


@router.get("/circulars/{circular_number}/subgraph", response_model=GraphVisualization, dependencies=[Depends(_ALLOWED)])
async def get_circular_subgraph(circular_number: str, settings: Settings = Depends(get_settings)) -> GraphVisualization:
    """A circular's full neighborhood: its clauses, the obligations/
    entities/penalties those clauses impose, and any clauses it
    references (or is referenced by)."""
    _require_enabled(settings)
    viz = await _run_subgraph_query(settings, CIRCULAR_SUBGRAPH, circular_number=circular_number)
    if not viz.nodes:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"No circular '{circular_number}' found in the knowledge graph.")
    return viz


@router.get("/entities/{entity_name}/subgraph", response_model=GraphVisualization, dependencies=[Depends(_ALLOWED)])
async def get_entity_subgraph(entity_name: str, settings: Settings = Depends(get_settings)) -> GraphVisualization:
    """Every obligation applying to one regulated entity type, across
    every circular that imposes one -- "show me everything that applies
    to Stockbroker.\""""
    _require_enabled(settings)
    viz = await _run_subgraph_query(settings, ENTITY_SUBGRAPH, entity_name=entity_name)
    if not viz.nodes:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"No entity '{entity_name}' found in the knowledge graph.")
    return viz


@router.get("/conflicts/subgraph", response_model=GraphVisualization, dependencies=[Depends(_ALLOWED)])
async def get_conflicts_subgraph(settings: Settings = Depends(get_settings)) -> GraphVisualization:
    """Every currently-persisted CONFLICTS_WITH pair (written by the
    scheduled app.graph.conflict_detection.write_conflict_edges run),
    with enough surrounding context (clause/circular/entity) to render
    each conflict legibly without a second lookup."""
    _require_enabled(settings)
    return await _run_subgraph_query(settings, CONFLICTS_SUBGRAPH)


@router.get("/conflicts/report", response_model=ConflictDetectionReport, dependencies=[Depends(_ALLOWED)])
async def get_conflict_report(settings: Settings = Depends(get_settings)) -> ConflictDetectionReport:
    """Runs Requirement 2's detection queries live (threshold conflicts,
    unenforced mandatory obligations, entity coverage gaps) and returns
    the full typed report -- for the dashboard's "run analysis now"
    action, distinct from /conflicts/subgraph's cheap read of
    already-persisted edges."""
    _require_enabled(settings)
    async with graph_client.session(settings) as session:
        return await build_conflict_report(session)


@router.post("/conflicts/detect", status_code=status.HTTP_202_ACCEPTED, dependencies=[Depends(_ALLOWED)])
async def trigger_conflict_detection(settings: Settings = Depends(get_settings)) -> dict:
    """Persists Requirement 2's threshold-conflict findings as
    :CONFLICTS_WITH edges (see app.graph.conflict_detection.write_conflict_edges's
    docstring for why this is a separate, schedulable write rather than
    something every read endpoint re-runs)."""
    _require_enabled(settings)
    async with graph_client.session(settings) as session:
        count = await write_conflict_edges(session)
    return {"conflicts_written": count}
