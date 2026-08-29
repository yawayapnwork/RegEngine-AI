"""Python wrappers around cypher/conflict_detection.cypher's queries --
Requirement 2. Each query string here is kept byte-identical to the
corresponding one in that file (a comment marks which) so the two never
drift apart; the .cypher file is what a DBA/compliance engineer reads and
runs manually, this module is what the API/scheduled job calls.
"""
from __future__ import annotations

import datetime as dt

from neo4j import AsyncSession
from pydantic import BaseModel

# --- Query 2 in cypher/conflict_detection.cypher: all contradictory-threshold pairs ---
_DETECT_THRESHOLD_CONFLICTS = """
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
ORDER BY delta_pct DESC
"""

# --- Query 3: MERGE the detected conflicts as persistent edges ---
_WRITE_CONFLICT_EDGES = """
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
RETURN count(r) AS conflicts_written
"""

# --- Query 4: mandatory/prohibited obligations with no stated penalty ---
_DETECT_UNENFORCED_OBLIGATIONS = """
MATCH (cl:Clause)-[:IMPOSES]->(o:Obligation)
WHERE o.obligation_type IN ["mandatory", "prohibited"]
  AND NOT EXISTS { MATCH (o)-[:ENFORCED_BY]->(:Penalty) }
RETURN
  cl.circular_number AS circular_number, cl.clause_number AS clause_number,
  o.obligation_id AS obligation_id, o.metric AS metric, o.operator AS operator, o.value AS value, o.unit AS unit,
  o.obligation_type AS obligation_type
ORDER BY cl.circular_number, cl.clause_number
"""

# --- Query 5: entities missing an obligation common to peer entities in the same domain ---
_DETECT_ENTITY_COVERAGE_GAPS = """
MATCH (o:Obligation)-[:APPLIES_TO]->(e:Entity)
WHERE o.domain IS NOT NULL
WITH o.domain AS domain, o.metric_field AS metric_field, collect(DISTINCT e.name) AS entities_with_obligation
MATCH (all_e:Entity)<-[:APPLIES_TO]-(:Obligation {domain: domain})
WITH domain, metric_field, entities_with_obligation, collect(DISTINCT all_e.name) AS all_entities_in_domain
UNWIND all_entities_in_domain AS entity_name
WITH domain, metric_field, entities_with_obligation, entity_name
WHERE NOT entity_name IN entities_with_obligation
  AND size(entities_with_obligation) >= 2
RETURN domain, metric_field, entity_name AS entity_missing_obligation, entities_with_obligation
ORDER BY domain, metric_field
"""


class ThresholdConflict(BaseModel):
    obligation_a_id: str
    obligation_b_id: str
    entity: str
    metric_field: str
    metric: str
    circular_a: str
    clause_a: str | None
    value_a: float
    operator: str
    unit: str
    circular_b: str
    clause_b: str | None
    value_b: float
    delta_value: float
    delta_pct: float | None


class UnenforcedObligation(BaseModel):
    circular_number: str
    clause_number: str | None
    obligation_id: str
    metric: str
    operator: str
    value: float
    unit: str
    obligation_type: str


class EntityCoverageGap(BaseModel):
    domain: str
    metric_field: str
    entity_missing_obligation: str
    entities_with_obligation: list[str]


class ConflictDetectionReport(BaseModel):
    generated_at: dt.datetime
    threshold_conflicts: list[ThresholdConflict]
    unenforced_mandatory_obligations: list[UnenforcedObligation]
    entity_coverage_gaps: list[EntityCoverageGap]


async def detect_threshold_conflicts(session: AsyncSession) -> list[ThresholdConflict]:
    result = await session.run(_DETECT_THRESHOLD_CONFLICTS)
    records = await result.data()
    return [ThresholdConflict.model_validate(r) for r in records]


async def detect_unenforced_mandatory_obligations(session: AsyncSession) -> list[UnenforcedObligation]:
    result = await session.run(_DETECT_UNENFORCED_OBLIGATIONS)
    records = await result.data()
    return [UnenforcedObligation.model_validate(r) for r in records]


async def detect_entity_coverage_gaps(session: AsyncSession) -> list[EntityCoverageGap]:
    result = await session.run(_DETECT_ENTITY_COVERAGE_GAPS)
    records = await result.data()
    return [EntityCoverageGap.model_validate(r) for r in records]


async def write_conflict_edges(session: AsyncSession) -> int:
    """Persists every currently-detectable conflict as a :CONFLICTS_WITH
    edge (Query 3) -- intended to run on a schedule (e.g. after each
    ingestion batch), not on every dashboard request; app.api.graph_routes'
    conflict-listing endpoint reads these persisted edges rather than
    re-running detection synchronously per request."""
    result = await session.run(_WRITE_CONFLICT_EDGES)
    record = await result.single()
    return int(record["conflicts_written"]) if record else 0


async def build_conflict_report(session: AsyncSession) -> ConflictDetectionReport:
    return ConflictDetectionReport(
        generated_at=dt.datetime.now(dt.timezone.utc),
        threshold_conflicts=await detect_threshold_conflicts(session),
        unenforced_mandatory_obligations=await detect_unenforced_mandatory_obligations(session),
        entity_coverage_gaps=await detect_entity_coverage_gaps(session),
    )
