// =============================================================================
// RegEngine AI Legal Knowledge Graph -- Conflict & Gap Detection Queries
// =============================================================================
// Every query here is READ-ONLY except the last (write_conflict_edges),
// which MERGEs :CONFLICTS_WITH edges so a dashboard can query "show me
// the graph's current known conflicts" cheaply (a relationship lookup)
// instead of re-running the detection logic on every page load.
// =============================================================================

// -----------------------------------------------------------------------
// Query 1: Contradictory thresholds across DIFFERENT circulars for the
// SAME metric and SAME entity -- Requirement 2's literal example
// ("Circular A requiring 20% margin while Master Circular B specifies
// 15% for the same asset class").
//
// Excludes pairs already linked by a declared SUPERSEDES/AMENDS
// relationship (app.graph.sync.declare_supersession/declare_amendment) --
// an old circular's threshold being different from its own superseding
// master circular's threshold is not a CONFLICT, it is regulatory
// history working as intended. Only two circulars with EQUAL standing
// (neither supersedes the other) disagreeing on the same metric for the
// same entity is a genuine conflict.
// -----------------------------------------------------------------------
:param metric_field => "upfront_margin_pct";

MATCH (o1:Obligation {metric_field: $metric_field})-[:APPLIES_TO]->(e:Entity)<-[:APPLIES_TO]-(o2:Obligation {metric_field: $metric_field})
WHERE o1.obligation_id < o2.obligation_id            // each unordered pair once
  AND o1.rule_id <> o2.rule_id
  AND o1.operator = o2.operator                      // same comparison direction -- ">=20%" vs "<=20%" is a different obligation shape, not a threshold contradiction to compare this way
  AND abs(o1.value - o2.value) > 0.01
MATCH (c1:Clause)-[:IMPOSES]->(o1)
MATCH (c2:Clause)-[:IMPOSES]->(o2)
WHERE NOT EXISTS {
        MATCH (a:Circular {circular_number: c1.circular_number})-[:SUPERSEDES|AMENDS]->(b:Circular {circular_number: c2.circular_number})
      }
  AND NOT EXISTS {
        MATCH (a:Circular {circular_number: c2.circular_number})-[:SUPERSEDES|AMENDS]->(b:Circular {circular_number: c1.circular_number})
      }
RETURN
  e.name                    AS entity,
  o1.metric                 AS metric,
  c1.circular_number        AS circular_a,
  c1.clause_number          AS clause_a,
  o1.value                  AS value_a,
  o1.unit                   AS unit_a,
  c2.circular_number        AS circular_b,
  c2.clause_number          AS clause_b,
  o2.value                  AS value_b,
  abs(o1.value - o2.value)               AS delta_value,
  abs(o1.value - o2.value) / o1.value * 100.0 AS delta_pct
ORDER BY delta_pct DESC;


// -----------------------------------------------------------------------
// Query 2: ALL contradictory-threshold pairs across the whole graph (not
// scoped to one metric_field) -- the batch version of Query 1, what
// app.graph.conflict_detection.detect_threshold_conflicts runs.
// -----------------------------------------------------------------------
MATCH (o1:Obligation)-[:APPLIES_TO]->(e:Entity)<-[:APPLIES_TO]-(o2:Obligation)
WHERE o1.metric_field = o2.metric_field
  AND o1.obligation_id < o2.obligation_id
  AND o1.rule_id <> o2.rule_id
  AND o1.operator = o2.operator
  AND abs(o1.value - o2.value) > 0.01
MATCH (c1:Clause)-[:IMPOSES]->(o1)
MATCH (c2:Clause)-[:IMPOSES]->(o2)
WHERE NOT EXISTS {
        MATCH (a:Circular {circular_number: c1.circular_number})-[:SUPERSEDES|AMENDS]->(b:Circular {circular_number: c2.circular_number})
      }
  AND NOT EXISTS {
        MATCH (a:Circular {circular_number: c2.circular_number})-[:SUPERSEDES|AMENDS]->(b:Circular {circular_number: c1.circular_number})
      }
RETURN
  o1.obligation_id AS obligation_a_id, o2.obligation_id AS obligation_b_id,
  e.name AS entity, o1.metric_field AS metric_field, o1.metric AS metric,
  c1.circular_number AS circular_a, c1.clause_number AS clause_a, o1.value AS value_a, o1.operator AS operator, o1.unit AS unit,
  c2.circular_number AS circular_b, c2.clause_number AS clause_b, o2.value AS value_b,
  abs(o1.value - o2.value) AS delta_value,
  CASE WHEN o1.value <> 0 THEN abs(o1.value - o2.value) / abs(o1.value) * 100.0 ELSE null END AS delta_pct
ORDER BY delta_pct DESC;


// -----------------------------------------------------------------------
// Query 3: MERGE the conflicts found by Query 2 as persistent
// :CONFLICTS_WITH edges, so a dashboard reads them cheaply. Idempotent --
// re-running this after new circulars are synced only adds NEW conflict
// edges; it never duplicates one already recorded for the same pair.
// Intended to run on a schedule (e.g. after every ingestion batch), not
// on every dashboard page load.
// -----------------------------------------------------------------------
MATCH (o1:Obligation)-[:APPLIES_TO]->(e:Entity)<-[:APPLIES_TO]-(o2:Obligation)
WHERE o1.metric_field = o2.metric_field
  AND o1.obligation_id < o2.obligation_id
  AND o1.rule_id <> o2.rule_id
  AND o1.operator = o2.operator
  AND abs(o1.value - o2.value) > 0.01
MATCH (c1:Clause)-[:IMPOSES]->(o1)
MATCH (c2:Clause)-[:IMPOSES]->(o2)
WHERE NOT EXISTS {
        MATCH (a:Circular {circular_number: c1.circular_number})-[:SUPERSEDES|AMENDS]->(b:Circular {circular_number: c2.circular_number})
      }
  AND NOT EXISTS {
        MATCH (a:Circular {circular_number: c2.circular_number})-[:SUPERSEDES|AMENDS]->(b:Circular {circular_number: c1.circular_number})
      }
MERGE (o1)-[r:CONFLICTS_WITH]-(o2)
SET r.reason = "same metric_field ('" + o1.metric_field + "') and entity ('" + e.name + "'), different thresholds across non-superseding circulars",
    r.delta_value = abs(o1.value - o2.value),
    r.delta_pct = CASE WHEN o1.value <> 0 THEN abs(o1.value - o2.value) / abs(o1.value) * 100.0 ELSE null END,
    r.detected_at = datetime()
RETURN count(r) AS conflicts_written;


// -----------------------------------------------------------------------
// Query 4: Regulatory GAP detection -- a MANDATORY or PROHIBITED
// obligation with no stated Penalty anywhere in the graph. Not every such
// obligation is truly a gap (many SEBI circulars defer penalties to a
// separate, generic penalty regulation never itself ingested as
// structured Obligations) -- this is a WORKLIST for human review, not an
// automatic verdict, exactly the same "surface for human judgment, never
// auto-decide" posture app.compiler.hitl already takes for ambiguous
// extractions.
// -----------------------------------------------------------------------
MATCH (cl:Clause)-[:IMPOSES]->(o:Obligation)
WHERE o.obligation_type IN ["mandatory", "prohibited"]
  AND NOT EXISTS { MATCH (o)-[:ENFORCED_BY]->(:Penalty) }
RETURN
  cl.circular_number AS circular_number, cl.clause_number AS clause_number,
  o.obligation_id AS obligation_id, o.metric AS metric, o.operator AS operator, o.value AS value, o.unit AS unit,
  o.obligation_type AS obligation_type
ORDER BY cl.circular_number, cl.clause_number;


// -----------------------------------------------------------------------
// Query 5: Regulatory GAP detection -- an Entity that has SOME
// obligations for a given regulatory domain but is missing an obligation
// on a metric_field every OTHER entity in the same domain has. Surfaces
// candidates like "every other Stockbroker-adjacent entity in the
// broking domain has an upfront_margin_pct obligation; DepositoryParticipant
// does not -- was this deliberate, or an ingestion gap?"
// -----------------------------------------------------------------------
MATCH (o:Obligation)-[:APPLIES_TO]->(e:Entity)
WHERE o.domain IS NOT NULL
WITH o.domain AS domain, o.metric_field AS metric_field, collect(DISTINCT e.name) AS entities_with_obligation
MATCH (all_e:Entity)<-[:APPLIES_TO]-(:Obligation {domain: domain})
WITH domain, metric_field, entities_with_obligation, collect(DISTINCT all_e.name) AS all_entities_in_domain
UNWIND all_entities_in_domain AS entity_name
WITH domain, metric_field, entities_with_obligation, entity_name
WHERE NOT entity_name IN entities_with_obligation
  AND size(entities_with_obligation) >= 2   // require the metric to be common practice (>=2 entities already have it) before flagging an absence as notable
RETURN domain, metric_field, entity_name AS entity_missing_obligation, entities_with_obligation
ORDER BY domain, metric_field;
