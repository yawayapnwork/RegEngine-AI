"""Legal knowledge graph pipeline (Neo4j): models the full web of
regulatory circulars/clauses/entities/obligations/penalties and their
relationships (SUPERSEDES, AMENDS, REFERENCES, APPLIES_TO, CONFLICTS_WITH),
and automates discovery of contradictory thresholds and unenforced
mandatory obligations across circulars.

Module map:
  client.py               Async Neo4j driver wrapper (app.graph.client.Neo4jClient).
  schema.py                Constraint/index DDL + apply_schema().
  penalty_detector.py       Regex heuristic extracting penalty language
                           from clause text (amount + penalty/fine/debarment
                           keywords) -- same "cheap deterministic
                           heuristic before anything structured" pattern
                           app.agents.graph.complexity_router already uses.
  reference_extractor.py   Extracts cross-referenced clause numbers from
                           clause text, reusing
                           app.agents.graph.complexity_router's
                           clause-number-token regex.
  sync.py                   Writes one AuditedComplianceRule (+ optional
                           clause text) into the graph -- Circular, Clause,
                           Entity, Obligation, Penalty nodes and their
                           relationships.
  conflict_detection.py    Cypher queries (+ Python wrappers) that flag
                           contradictory thresholds and unenforced
                           mandatory obligations, and MERGE the resulting
                           CONFLICTS_WITH edges back into the graph.
  visualization.py         Builds dashboard-ready {nodes, edges} JSON from
                           a Cypher result -- the shared shape every
                           app.api.graph_routes endpoint returns.
"""
