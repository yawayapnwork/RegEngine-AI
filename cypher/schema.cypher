// =============================================================================
// RegEngine AI Legal Knowledge Graph -- Schema (Neo4j 5.x)
// =============================================================================
// Node labels
// -----------
//   :Circular    One regulatory document (SEBI circular, RBI Master
//                Direction, IRDAI regulation, ...). Properties:
//                  circular_number (unique key), regulator, document_type,
//                  issue_date, title
//
//   :Clause      One clause/section within a Circular. Properties:
//                  clause_id (unique key, = "<source_sha256>:<clause_number>"
//                  matching ExtractedComplianceRule.rule_id's own convention),
//                  clause_number, circular_number, section_path,
//                  obligation_type, extraction_confidence
//
//   :Entity      A regulated entity type (e.g. "Stockbroker", "NBFC").
//                Properties: name (unique key), regulator
//
//   :Obligation  One deterministic, machine-checkable requirement --
//                one node per NumericalThreshold extracted from a Clause.
//                Properties: obligation_id (unique key,
//                  "<rule_id>:<threshold_index>"), rule_id, metric,
//                  metric_field (app.compiler.naming.metric_field_name --
//                  THE join key conflict detection matches on across
//                  circulars), operator, value, value_upper, unit,
//                  applies_to, obligation_type, regulator, domain,
//                  verbatim_evidence
//
//   :Penalty     A stated consequence for non-compliance, when the source
//                clause specifies one (app.graph.penalty_detector).
//                Properties: penalty_id (unique key), description, amount,
//                unit, basis_text
//
// Relationships
// -------------
//   (:Circular)-[:CONTAINS]->(:Clause)
//   (:Circular)-[:SUPERSEDES {effective_date}]->(:Circular)      -- operator-asserted, see sync.py's docstring
//   (:Circular)-[:AMENDS {effective_date}]->(:Circular)          -- operator-asserted
//   (:Clause)-[:REFERENCES]->(:Clause)                            -- cross-reference detected in clause text
//   (:Clause)-[:IMPOSES]->(:Obligation)
//   (:Clause)-[:APPLIES_TO]->(:Entity)
//   (:Obligation)-[:APPLIES_TO]->(:Entity)                        -- obligation-level scoping, may differ from clause-level
//   (:Obligation)-[:ENFORCED_BY]->(:Penalty)
//   (:Obligation)-[:CONFLICTS_WITH {reason, delta_value, delta_pct, detected_at}]->(:Obligation)
//                                                                  -- DERIVED, written by app.graph.conflict_detection,
//                                                                     never by the sync pipeline
// =============================================================================

// --- Uniqueness constraints (also create a backing index) ---
CREATE CONSTRAINT circular_number_unique IF NOT EXISTS
FOR (c:Circular) REQUIRE c.circular_number IS UNIQUE;

CREATE CONSTRAINT clause_id_unique IF NOT EXISTS
FOR (cl:Clause) REQUIRE cl.clause_id IS UNIQUE;

CREATE CONSTRAINT entity_name_unique IF NOT EXISTS
FOR (e:Entity) REQUIRE e.name IS UNIQUE;

CREATE CONSTRAINT obligation_id_unique IF NOT EXISTS
FOR (o:Obligation) REQUIRE o.obligation_id IS UNIQUE;

CREATE CONSTRAINT penalty_id_unique IF NOT EXISTS
FOR (p:Penalty) REQUIRE p.penalty_id IS UNIQUE;

// --- Supporting indexes for conflict detection / lookup performance ---

// Conflict detection's core join: obligations sharing a metric_field.
CREATE INDEX obligation_metric_field IF NOT EXISTS
FOR (o:Obligation) ON (o.metric_field);

CREATE INDEX obligation_regulator_domain IF NOT EXISTS
FOR (o:Obligation) ON (o.regulator, o.domain);

CREATE INDEX clause_circular_number IF NOT EXISTS
FOR (cl:Clause) ON (cl.circular_number);

CREATE INDEX clause_clause_number IF NOT EXISTS
FOR (cl:Clause) ON (cl.clause_number);

CREATE INDEX circular_regulator IF NOT EXISTS
FOR (c:Circular) ON (c.regulator);
