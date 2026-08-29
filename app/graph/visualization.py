"""Builds the {nodes, edges} JSON shape every app.api.graph_routes
endpoint returns -- generic enough for any force-directed graph
visualization library (Cytoscape.js, react-force-graph, vis-network) to
consume directly, so the dashboard's choice of rendering library is never
coupled to this API's response shape.
"""
from __future__ import annotations

from pydantic import BaseModel, Field


class GraphNode(BaseModel):
    id: str
    label: str  # the Neo4j node label: "Circular" | "Clause" | "Entity" | "Obligation" | "Penalty"
    properties: dict = Field(default_factory=dict)


class GraphEdge(BaseModel):
    source: str
    target: str
    type: str  # the Neo4j relationship type: "CONTAINS" | "SUPERSEDES" | "AMENDS" | "REFERENCES" | "APPLIES_TO" | "IMPOSES" | "ENFORCED_BY" | "CONFLICTS_WITH"
    properties: dict = Field(default_factory=dict)


class GraphVisualization(BaseModel):
    nodes: list[GraphNode] = Field(default_factory=list)
    edges: list[GraphEdge] = Field(default_factory=list)


def _node_display_id(label: str, properties: dict) -> str:
    """Neo4j's own internal element id is an implementation detail that
    can change across a database restore/migration -- the dashboard
    instead gets a stable, human-meaningful id built from each label's
    own business key (circular_number, clause_id, name, obligation_id,
    penalty_id), matching what every Cypher query in this module already
    MERGEs on."""
    key_property = {
        "Circular": "circular_number", "Clause": "clause_id", "Entity": "name",
        "Obligation": "obligation_id", "Penalty": "penalty_id",
    }.get(label)
    if key_property and key_property in properties:
        return f"{label}:{properties[key_property]}"
    return f"{label}:{properties.get('id', id(properties))}"


def build_visualization_from_records(records: list[dict]) -> GraphVisualization:
    """`records` is the output of a Cypher query returning `nodes` and
    `relationships` collections per row (see app.graph.subgraph_queries'
    RETURN clauses, which every visualization-oriented query in this
    module is written to match) -- a Neo4j Node has `.labels` and
    `.items()`; a Relationship has `.type`, `.start_node`, `.end_node`.
    """
    nodes_by_id: dict[str, GraphNode] = {}
    edges: list[GraphEdge] = []
    seen_edges: set[tuple[str, str, str]] = set()

    for record in records:
        for node in record.get("nodes", []) or []:
            label = next(iter(node.labels), "Unknown")
            properties = dict(node.items())
            node_id = _node_display_id(label, properties)
            nodes_by_id[node_id] = GraphNode(id=node_id, label=label, properties=properties)

        for rel in record.get("relationships", []) or []:
            start_label = next(iter(rel.start_node.labels), "Unknown")
            end_label = next(iter(rel.end_node.labels), "Unknown")
            source_id = _node_display_id(start_label, dict(rel.start_node.items()))
            target_id = _node_display_id(end_label, dict(rel.end_node.items()))
            edge_key = (source_id, target_id, rel.type)
            if edge_key in seen_edges:
                continue
            seen_edges.add(edge_key)
            edges.append(GraphEdge(source=source_id, target=target_id, type=rel.type, properties=dict(rel.items())))

    return GraphVisualization(nodes=list(nodes_by_id.values()), edges=edges)
