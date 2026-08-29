"""Writes one approved `AuditedComplianceRule` into the Neo4j knowledge
graph: Circular/Clause/Entity/Obligation(/Penalty) nodes and their
relationships (Requirement 1's schema).

Only APPROVED extractions are synced -- same trustworthiness filter
applied throughout this codebase wherever pipeline artifacts feed a
downstream system that isn't itself the audit stage (see
llm_finetune.dataset.format_instructions._only_trustworthy and
app.diffing.report_builder.analyze_circular_impact's identical rationale):
syncing a REJECTED or NEEDS_REVISION extraction into the graph would let
a hallucinated threshold masquerade as a real regulatory obligation for
every downstream conflict-detection query and dashboard view.

Every write uses MERGE, not CREATE -- re-syncing the same rule_id (e.g.
after a rule_version bump) updates the existing Obligation node's
properties rather than creating a duplicate, and referencing a Circular/
Clause/Entity that another rule's sync already created is idempotent.
"""
from __future__ import annotations

import logging

from neo4j import AsyncSession

from app.agents.schemas import AuditedComplianceRule, AuditVerdict, ExtractedComplianceRule
from app.compiler.naming import metric_field_name
from app.graph.penalty_detector import detect_penalty
from app.graph.reference_extractor import extract_referenced_clause_numbers
from app.regulatory.taxonomy import resolve_domain

logger = logging.getLogger(__name__)

_MERGE_CIRCULAR = """
MERGE (c:Circular {circular_number: $circular_number})
ON CREATE SET c.regulator = $regulator, c.created_at = datetime()
ON MATCH SET c.regulator = coalesce(c.regulator, $regulator)
"""

_MERGE_CLAUSE = """
MATCH (c:Circular {circular_number: $circular_number})
MERGE (cl:Clause {clause_id: $clause_id})
ON CREATE SET cl.clause_number = $clause_number, cl.circular_number = $circular_number,
               cl.section_path = $section_path, cl.obligation_type = $obligation_type,
               cl.extraction_confidence = $extraction_confidence, cl.created_at = datetime()
ON MATCH SET cl.obligation_type = $obligation_type, cl.extraction_confidence = $extraction_confidence
MERGE (c)-[:CONTAINS]->(cl)
"""

_MERGE_ENTITY = """
MERGE (e:Entity {name: $name})
ON CREATE SET e.regulator = $regulator
"""

_MERGE_CLAUSE_APPLIES_TO_ENTITY = """
MATCH (cl:Clause {clause_id: $clause_id}), (e:Entity {name: $entity_name})
MERGE (cl)-[:APPLIES_TO]->(e)
"""

_MERGE_OBLIGATION = """
MATCH (cl:Clause {clause_id: $clause_id})
MERGE (o:Obligation {obligation_id: $obligation_id})
ON CREATE SET o.rule_id = $rule_id, o.metric = $metric, o.metric_field = $metric_field,
              o.operator = $operator, o.value = $value, o.value_upper = $value_upper,
              o.unit = $unit, o.applies_to = $applies_to, o.obligation_type = $obligation_type,
              o.regulator = $regulator, o.domain = $domain, o.verbatim_evidence = $verbatim_evidence,
              o.created_at = datetime()
ON MATCH SET o.metric = $metric, o.operator = $operator, o.value = $value, o.value_upper = $value_upper,
             o.unit = $unit, o.updated_at = datetime()
MERGE (cl)-[:IMPOSES]->(o)
"""

_MERGE_OBLIGATION_APPLIES_TO_ENTITY = """
MATCH (o:Obligation {obligation_id: $obligation_id}), (e:Entity {name: $entity_name})
MERGE (o)-[:APPLIES_TO]->(e)
"""

_MERGE_PENALTY = """
MATCH (o:Obligation {obligation_id: $obligation_id})
MERGE (p:Penalty {penalty_id: $penalty_id})
ON CREATE SET p.description = $description, p.amount_text = $amount_text, p.basis_text = $basis_text
MERGE (o)-[:ENFORCED_BY]->(p)
"""

_MERGE_REFERENCED_CLAUSE_STUB = """
MERGE (referenced:Clause {clause_id: $referenced_clause_id})
ON CREATE SET referenced.clause_number = $referenced_clause_number,
              referenced.circular_number = $circular_number, referenced.is_stub = true
"""

_MERGE_REFERENCES_EDGE = """
MATCH (cl:Clause {clause_id: $clause_id}), (referenced:Clause {clause_id: $referenced_clause_id})
MERGE (cl)-[:REFERENCES]->(referenced)
"""


def _stub_clause_id(circular_number: str, clause_number: str) -> str:
    """Referenced-but-not-yet-independently-extracted clauses have no
    `source_sha256` to build a real clause_id from (that only exists once
    THAT clause is itself extracted) -- a stable, deterministic stub id
    scoped to (circular_number, clause_number) lets app.graph.sync MERGE
    the real clause_id onto this same node later without ever needing a
    reconciliation pass, PROVIDED the real extraction also happens to
    land on this exact id. Since it generally won't (real clause_ids are
    "<source_sha256>:<clause_number>"), this stub deliberately stays a
    separate, clearly-marked (`is_stub: true`) placeholder node rather
    than pretending to be the eventual real one -- see this module's
    docstring: MERGE avoids duplicates for the SAME id, it does not
    reconcile two different ids that happen to describe the same clause."""
    return f"stub:{circular_number}:{clause_number}"


async def sync_audited_rule_to_graph(
    session: AsyncSession,
    audited: AuditedComplianceRule,
    clause_text: str | None = None,
) -> None:
    """`clause_text`, when supplied, additionally populates REFERENCES
    edges and Penalty nodes (both need the raw text; `ExtractedComplianceRule`
    itself doesn't carry it -- see this module's docstring on why some
    callers, e.g. app.compiler.tasks's Celery hook, can only provide the
    rule and not the source chunk). Without it, Circular/Clause/Entity/
    Obligation are still fully synced -- REFERENCES/Penalty are additive
    enrichment, not a precondition for the core schema."""
    if audited.audit.verdict != AuditVerdict.APPROVED:
        logger.debug("Skipping graph sync for rule_id=%s: audit verdict is '%s', not approved.", audited.rule.rule_id, audited.audit.verdict.value)
        return

    rule = audited.rule
    if not rule.circular_number or not rule.clause_number:
        logger.warning("Skipping graph sync for rule_id=%s: missing circular_number/clause_number.", rule.rule_id)
        return

    clause_id = rule.rule_id  # "<source_sha256>:<clause_number>" -- see app.agents.schemas.ExtractedComplianceRule.rule_id's docstring
    regulator_value = rule.regulator.value

    await session.run(_MERGE_CIRCULAR, circular_number=rule.circular_number, regulator=regulator_value)
    await session.run(
        _MERGE_CLAUSE,
        circular_number=rule.circular_number, clause_id=clause_id, clause_number=rule.clause_number,
        section_path=rule.section_path, obligation_type=rule.obligation_type.value,
        extraction_confidence=rule.extraction_confidence,
    )

    entity_names = sorted({e.normalized_entity or e.raw_text for e in rule.target_entities if (e.normalized_entity or e.raw_text)})
    for entity_name in entity_names:
        await session.run(_MERGE_ENTITY, name=entity_name, regulator=regulator_value)
        await session.run(_MERGE_CLAUSE_APPLIES_TO_ENTITY, clause_id=clause_id, entity_name=entity_name)

    primary_entity = entity_names[0] if entity_names else None
    domain = rule.regulatory_domain or resolve_domain(rule.regulator, primary_entity)

    for idx, threshold in enumerate(rule.deterministic_logic):
        obligation_id = f"{rule.rule_id}:{idx}"
        await session.run(
            _MERGE_OBLIGATION,
            clause_id=clause_id, obligation_id=obligation_id, rule_id=rule.rule_id,
            metric=threshold.metric, metric_field=metric_field_name(threshold.metric, threshold.unit),
            operator=threshold.operator.value, value=threshold.value, value_upper=threshold.value_upper,
            unit=threshold.unit, applies_to=threshold.applies_to, obligation_type=rule.obligation_type.value,
            regulator=regulator_value, domain=domain, verbatim_evidence=threshold.verbatim_evidence,
        )
        for entity_name in entity_names:
            await session.run(_MERGE_OBLIGATION_APPLIES_TO_ENTITY, obligation_id=obligation_id, entity_name=entity_name)

        if clause_text:
            penalty = detect_penalty(clause_text)
            if penalty:
                await session.run(
                    _MERGE_PENALTY,
                    obligation_id=obligation_id, penalty_id=f"{obligation_id}:penalty",
                    description=penalty.description, amount_text=penalty.amount_text, basis_text=penalty.basis_text,
                )

    if clause_text:
        for referenced_clause_number in extract_referenced_clause_numbers(clause_text, rule.clause_number):
            referenced_clause_id = _stub_clause_id(rule.circular_number, referenced_clause_number)
            await session.run(
                _MERGE_REFERENCED_CLAUSE_STUB,
                referenced_clause_id=referenced_clause_id, referenced_clause_number=referenced_clause_number,
                circular_number=rule.circular_number,
            )
            await session.run(_MERGE_REFERENCES_EDGE, clause_id=clause_id, referenced_clause_id=referenced_clause_id)

    logger.info(
        "Synced rule_id=%s to knowledge graph: clause=%s circular=%s entities=%s obligations=%d",
        rule.rule_id, rule.clause_number, rule.circular_number, entity_names, len(rule.deterministic_logic),
    )


async def declare_supersession(session: AsyncSession, superseding_circular_number: str, superseded_circular_number: str, effective_date: str | None = None) -> None:
    """Writes a `(:Circular)-[:SUPERSEDES]->(:Circular)` edge.
    Deliberately OPERATOR-ASSERTED, never auto-inferred from text
    similarity or issue dates: "circular X supersedes circular Y" is a
    regulatory/legal fact a compliance officer determines (the same
    judgment call `app.diffing.report_builder.analyze_circular_impact`'s
    `supersedes_circular_number` parameter already requires explicitly,
    for the identical reason -- see that function's docstring)."""
    await session.run(
        """
        MATCH (new:Circular {circular_number: $new}), (old:Circular {circular_number: $old})
        MERGE (new)-[r:SUPERSEDES]->(old)
        SET r.effective_date = $effective_date, r.declared_at = datetime()
        """,
        new=superseding_circular_number, old=superseded_circular_number, effective_date=effective_date,
    )


async def declare_amendment(session: AsyncSession, amending_circular_number: str, amended_circular_number: str, effective_date: str | None = None) -> None:
    """Same operator-asserted contract as `declare_supersession`, for a
    partial amendment rather than a full replacement."""
    await session.run(
        """
        MATCH (new:Circular {circular_number: $new}), (old:Circular {circular_number: $old})
        MERGE (new)-[r:AMENDS]->(old)
        SET r.effective_date = $effective_date, r.declared_at = datetime()
        """,
        new=amending_circular_number, old=amended_circular_number, effective_date=effective_date,
    )
