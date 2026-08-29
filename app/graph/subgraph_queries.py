"""Cypher queries feeding the visual-analysis API (Requirement 3). Each
query's RETURN clause is shaped as `nodes`/`relationships` collections
so `app.graph.visualization.build_visualization_from_records` can consume
any of them identically.
"""
from __future__ import annotations

# One circular's full subgraph: its clauses, the entities/obligations/
# penalties those clauses impose, and any REFERENCES edges reaching
# clauses in OTHER circulars (bounded to depth 1 out from this circular's
# own clauses, so the response stays a legible neighborhood rather than
# the whole graph).
CIRCULAR_SUBGRAPH = """
MATCH (c:Circular {circular_number: $circular_number})
OPTIONAL MATCH (c)-[contains:CONTAINS]->(cl:Clause)
OPTIONAL MATCH (cl)-[imposes:IMPOSES]->(o:Obligation)
OPTIONAL MATCH (cl)-[applies_to_clause:APPLIES_TO]->(e1:Entity)
OPTIONAL MATCH (o)-[obl_applies_to:APPLIES_TO]->(e2:Entity)
OPTIONAL MATCH (o)-[enforced_by:ENFORCED_BY]->(p:Penalty)
OPTIONAL MATCH (cl)-[references:REFERENCES]->(ref_cl:Clause)
RETURN
  collect(DISTINCT c) + collect(DISTINCT cl) + collect(DISTINCT o) +
  collect(DISTINCT e1) + collect(DISTINCT e2) + collect(DISTINCT p) + collect(DISTINCT ref_cl) AS nodes,
  collect(DISTINCT contains) + collect(DISTINCT imposes) + collect(DISTINCT applies_to_clause) +
  collect(DISTINCT obl_applies_to) + collect(DISTINCT enforced_by) + collect(DISTINCT references) AS relationships
"""

# Every obligation applying to one Entity, across every circular that
# imposes one -- "show me everything that applies to Stockbroker."
ENTITY_SUBGRAPH = """
MATCH (e:Entity {name: $entity_name})
OPTIONAL MATCH (o:Obligation)-[applies_to:APPLIES_TO]->(e)
OPTIONAL MATCH (cl:Clause)-[imposes:IMPOSES]->(o)
OPTIONAL MATCH (c:Circular)-[contains:CONTAINS]->(cl)
OPTIONAL MATCH (o)-[enforced_by:ENFORCED_BY]->(p:Penalty)
RETURN
  collect(DISTINCT e) + collect(DISTINCT o) + collect(DISTINCT cl) + collect(DISTINCT c) + collect(DISTINCT p) AS nodes,
  collect(DISTINCT applies_to) + collect(DISTINCT imposes) + collect(DISTINCT contains) + collect(DISTINCT enforced_by) AS relationships
"""

# The conflict-focused view: every Obligation pair linked by a
# CONFLICTS_WITH edge (already written by
# app.graph.conflict_detection.write_conflict_edges), plus enough
# context (their Clause/Circular/Entity) to make the conflict legible on
# a graph render without a second lookup.
CONFLICTS_SUBGRAPH = """
MATCH (o1:Obligation)-[conflict:CONFLICTS_WITH]-(o2:Obligation)
MATCH (cl1:Clause)-[imposes1:IMPOSES]->(o1)
MATCH (cl2:Clause)-[imposes2:IMPOSES]->(o2)
MATCH (c1:Circular)-[contains1:CONTAINS]->(cl1)
MATCH (c2:Circular)-[contains2:CONTAINS]->(cl2)
OPTIONAL MATCH (o1)-[a1:APPLIES_TO]->(e:Entity)<-[a2:APPLIES_TO]-(o2)
RETURN
  collect(DISTINCT o1) + collect(DISTINCT o2) + collect(DISTINCT cl1) + collect(DISTINCT cl2) +
  collect(DISTINCT c1) + collect(DISTINCT c2) + collect(DISTINCT e) AS nodes,
  collect(DISTINCT conflict) + collect(DISTINCT imposes1) + collect(DISTINCT imposes2) +
  collect(DISTINCT contains1) + collect(DISTINCT contains2) + collect(DISTINCT a1) + collect(DISTINCT a2) AS relationships
"""
