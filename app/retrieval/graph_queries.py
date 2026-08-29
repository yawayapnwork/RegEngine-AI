"""Neo4j GraphRAG query definitions: given a set of clauses a vector
search already found relevant, find the clauses connected to them by a
regulatory-dependency edge (SUPERSEDES, AMENDS, REFERENCES) that a pure
embedding-similarity search would likely miss or under-rank -- two
clauses that replace each other often read nothing alike (a threshold
changing from 20% to 15% is one number apart, not one embedding
apart).

Query shape follows app.graph.subgraph_queries's established
convention (parameterized Cypher constants, `nodes`/`relationships` or,
here, a flatter per-hit shape purpose-built for
app.retrieval.hybrid_search rather than app.graph.visualization).
"""
from __future__ import annotations

# Starting from a set of clause_ids a vector search already returned,
# walk outward up to $max_depth hops across SUPERSEDES/AMENDS/REFERENCES
# edges in EITHER direction (a clause that supersedes one of our hits is
# just as relevant as one our hits supersede), returning each reached
# clause once with the shortest path that reached it. `$max_depth` is
# bound by settings.hybrid_retrieval_graph_depth -- see that setting's
# docstring in app.config for why it stays small.
DEPENDENCY_EXPANSION = """
UNWIND $clause_ids AS start_id
MATCH (start:Clause {clause_id: start_id})
MATCH path = (start)-[rels:SUPERSEDES|AMENDS|REFERENCES*1..%(max_depth)d]-(reached:Clause)
WHERE reached.clause_id <> start_id
WITH reached, start_id, rels, path,
     [r IN rels | type(r)] AS relationship_types,
     [r IN rels | coalesce(r.auto_detected, false)] AS auto_detected_flags
ORDER BY length(path) ASC
WITH reached, collect({
       source_clause_id: start_id,
       relationship_types: relationship_types,
       hop_count: length(path),
       any_auto_detected: any(f IN auto_detected_flags WHERE f = true)
     })[0] AS best_path
RETURN DISTINCT
  reached.clause_id AS clause_id,
  reached.clause_number AS clause_number,
  reached.circular_number AS circular_number,
  coalesce(reached.is_stub, false) AS is_stub,
  best_path.source_clause_id AS source_clause_id,
  best_path.relationship_types AS relationship_types,
  best_path.hop_count AS hop_count,
  best_path.any_auto_detected AS any_auto_detected
LIMIT $max_hits
"""


def dependency_expansion_query(max_depth: int) -> str:
    """`max_depth` controls a variable-length-path bound, which Neo4j
    requires as a literal in the query text rather than a bound
    parameter -- interpolated here (not via an f-string at import time)
    specifically so it stays a small, code-controlled int
    (settings.hybrid_retrieval_graph_depth), never user-supplied text."""
    return DEPENDENCY_EXPANSION % {"max_depth": max_depth}


# Full forward supersession/amendment chain starting at one clause --
# "everything this clause (transitively) replaces" -- irrespective of
# whether those edges are operator-asserted at the circular level or
# auto-detected at the clause level (app.graph.supersession_extractor
# writes the latter with auto_detected=true; both are real graph edges
# either way).
SUPERSESSION_CHAIN_FORWARD = """
MATCH (start:Clause {clause_id: $clause_id})
MATCH path = (start)-[:SUPERSEDES|AMENDS*1..%(max_depth)d]->(replaced:Clause)
RETURN DISTINCT
  replaced.clause_id AS clause_id, replaced.clause_number AS clause_number,
  replaced.circular_number AS circular_number, length(path) AS hop_count
ORDER BY hop_count ASC
"""


def supersession_chain_forward_query(max_depth: int) -> str:
    return SUPERSESSION_CHAIN_FORWARD % {"max_depth": max_depth}


# The reverse direction -- "everything that has (transitively) replaced
# this clause" -- e.g. checking whether an old base-regulation clause a
# broker's system still references is actually stale.
SUPERSESSION_CHAIN_REVERSE = """
MATCH (start:Clause {clause_id: $clause_id})
MATCH path = (replacing:Clause)-[:SUPERSEDES|AMENDS*1..%(max_depth)d]->(start)
RETURN DISTINCT
  replacing.clause_id AS clause_id, replacing.clause_number AS clause_number,
  replacing.circular_number AS circular_number, length(path) AS hop_count
ORDER BY hop_count ASC
"""


def supersession_chain_reverse_query(max_depth: int) -> str:
    return SUPERSESSION_CHAIN_REVERSE % {"max_depth": max_depth}
