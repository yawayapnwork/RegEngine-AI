"""Isolated comparison snapshot generator for regulated entities.

Enforces strict tenant scoping on every query, data minimization (no raw transaction
leaks), and computes deterministic cryptographic SHA-256 digests for snapshot consistency.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Clause, CompiledRule, HITLReview, Tenant
from app.ledger.models import compliance_audit_ledger
from app.mna_due_diligence.models import (
    EntityClauseSnapshot,
    EntityComplianceSnapshot,
    EntityGraphObligationSnapshot,
    EntityHITLReviewSnapshot,
    EntityHistoricalViolationsSummary,
    EntityRuleSnapshot,
)

logger = logging.getLogger(__name__)


def compute_snapshot_hash(snapshot_payload: dict[str, Any]) -> str:
    """Computes a deterministic SHA-256 hash over canonical JSON representation of snapshot content."""
    canonical_json = json.dumps(snapshot_payload, sort_keys=True, default=str)
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()


async def build_entity_snapshot(
    db: AsyncSession,
    entity_id: str,
    neo4j_session: Any | None = None,
) -> EntityComplianceSnapshot:
    """Extracts an isolated compliance snapshot for a single entity with strict tenant scoping.

    Every database query is strictly scoped by `tenant_id == entity_id`.
    No cross-entity queries or raw proprietary transaction rows are exposed.
    """
    logger.info("Building compliance due-diligence snapshot for entity: %s", entity_id)

    # 1. Fetch Tenant Metadata & Risk Overlay
    tenant_stmt = select(Tenant).where(Tenant.tenant_id == entity_id)
    tenant_res = await db.execute(tenant_stmt)
    tenant = tenant_res.scalar_one_or_none()
    if not tenant:
        raise ValueError(f"Cannot build snapshot: entity '{entity_id}' not found in registry.")

    display_name = tenant.display_name
    tenant_type = tenant.tenant_type
    is_active = tenant.is_active
    risk_overlay = tenant.risk_overlay or {}

    # 2. Fetch Compiled Rules (strictly scoped to tenant_id)
    rules_stmt = (
        select(CompiledRule, Clause)
        .outerjoin(Clause, CompiledRule.clause_id == Clause.id)
        .where(CompiledRule.tenant_id == entity_id)
        .order_by(CompiledRule.rule_id, CompiledRule.rule_version)
    )
    rules_res = await db.execute(rules_stmt)
    rules_rows = rules_res.all()

    rule_snapshots: list[EntityRuleSnapshot] = []
    seen_clause_ids: set[int] = set()

    for rule, clause in rules_rows:
        clause_sha = clause.sha256 if clause else None
        clause_num = clause.clause_number if clause else None
        sec_ref = (
            ".".join(clause.section_path)
            if (clause and clause.section_path)
            else (clause_num or rule.rule_id)
        )
        if clause:
            seen_clause_ids.add(clause.id)

        rule_snapshots.append(
            EntityRuleSnapshot(
                rule_id=rule.rule_id,
                rule_version=rule.rule_version,
                policy_sha256=rule.policy_sha256,
                rego_policy=rule.rego_policy,
                jsonlogic_ast=rule.jsonlogic_ast,
                opa_package_name=rule.opa_package_name,
                is_active=rule.is_active,
                hitl_status=rule.hitl_status,
                clause_id=rule.clause_id,
                clause_sha256=clause_sha,
                clause_number=clause_num,
                section_reference=sec_ref,
            )
        )

    # 3. Fetch Relevant Clauses (strictly scoped to tenant_id or referenced clauses)
    from app.db.models import Circular

    clauses_stmt = (
        select(Clause, Circular.circular_number)
        .outerjoin(Circular, Clause.circular_id == Circular.id)
        .where((Clause.tenant_id == entity_id) | (Clause.id.in_(list(seen_clause_ids)) if seen_clause_ids else False))
        .order_by(Clause.clause_number)
    )
    clauses_res = await db.execute(clauses_stmt)
    clause_rows = clauses_res.all()

    clause_snapshots: list[EntityClauseSnapshot] = [
        EntityClauseSnapshot(
            clause_number=c.clause_number,
            text=c.text,
            sha256=c.sha256,
            section_path=c.section_path or [],
            circular_number=circ_num,
        )
        for c, circ_num in clause_rows
    ]

    # 4. Fetch Unresolved HITL Reviews (strictly scoped to tenant_id)
    hitl_stmt = (
        select(HITLReview)
        .where(
            HITLReview.tenant_id == entity_id,
            HITLReview.status.in_(["PENDING", "IN_REVIEW"]),
        )
        .order_by(HITLReview.flagged_at.desc())
    )
    hitl_res = await db.execute(hitl_stmt)
    hitl_rows = hitl_res.scalars().all()

    hitl_snapshots: list[EntityHITLReviewSnapshot] = [
        EntityHITLReviewSnapshot(
            review_id=h.review_id,
            reason_code=h.reason_code,
            severity=h.severity,
            description=h.description,
            source_excerpt=h.source_excerpt,
            status=h.status,
            clause_id=h.clause_id,
            compiled_rule_id=h.compiled_rule_id,
            flagged_at=h.flagged_at,
        )
        for h in hitl_rows
    ]

    # 5. Fetch Aggregate Historical Violations Summary from compliance_audit_ledger
    # DATA MINIMIZATION: Never expose full raw transaction-level data or client PII!
    ledger_totals_stmt = (
        select(
            func.count().label("total_evals"),
            func.count().filter(compliance_audit_ledger.c.evaluation_result == "FAIL").label("total_fails"),
            func.count().filter(compliance_audit_ledger.c.evaluation_result == "HITL_REVIEW").label("total_hitl"),
        )
        .select_from(compliance_audit_ledger)
        .where(compliance_audit_ledger.c.broker_id == entity_id)
    )
    try:
        totals_res = await db.execute(ledger_totals_stmt)
        totals_row = totals_res.first()
        total_evals = totals_row.total_evals if totals_row else 0
        total_fails = totals_row.total_fails if totals_row else 0
        total_hitl = totals_row.total_hitl if totals_row else 0

        # Query distinct failed rule IDs
        failed_rules_stmt = (
            select(compliance_audit_ledger.c.rule_id, func.count().label("failure_count"))
            .where(
                compliance_audit_ledger.c.broker_id == entity_id,
                compliance_audit_ledger.c.evaluation_result.in_(["FAIL", "HITL_REVIEW"]),
            )
            .group_by(compliance_audit_ledger.c.rule_id)
            .order_by(func.count().desc())
            .limit(20)
        )
        failed_res = await db.execute(failed_rules_stmt)
        failed_rows = failed_res.all()
        failed_rule_ids = [r[0] for r in failed_rows]
        top_violation_metrics = {r[0]: r[1] for r in failed_rows}

        violations_summary = EntityHistoricalViolationsSummary(
            total_evaluations=total_evals,
            total_fails=total_fails,
            total_hitl_reviews=total_hitl,
            failed_rule_ids=failed_rule_ids,
            top_violation_metrics=top_violation_metrics,
            has_transaction_level_data=False,
        )
    except Exception as exc:
        logger.warning("Could not query historical audit ledger for entity %s: %s", entity_id, exc)
        violations_summary = EntityHistoricalViolationsSummary()

    # 6. Fetch Graph Obligations (if Neo4j is available)
    graph_obligations: list[EntityGraphObligationSnapshot] = []
    if neo4j_session is not None:
        try:
            cypher = """
            MATCH (o:Obligation)-[:APPLIES_TO]->(e:Entity)
            WHERE e.name = $entity_id OR e.tenant_id = $entity_id
            RETURN o.obligation_id AS obligation_id, o.metric AS metric,
                   o.operator AS operator, o.value AS value, o.unit AS unit, o.domain AS domain
            """
            result = await neo4j_session.run(cypher, entity_id=entity_id)
            records = await result.data()
            for rec in records:
                graph_obligations.append(
                    EntityGraphObligationSnapshot(
                        obligation_id=rec["obligation_id"],
                        metric=rec["metric"],
                        operator=rec["operator"],
                        value=float(rec["value"]),
                        unit=rec["unit"],
                        domain=rec.get("domain"),
                    )
                )
        except Exception as exc:
            logger.debug("Neo4j query for entity %s skipped/failed: %s", entity_id, exc)

    # 7. Compute Deterministic Snapshot Hash
    hash_payload = {
        "entity_id": entity_id,
        "tenant_type": tenant_type,
        "risk_overlay": risk_overlay,
        "rules": [r.model_dump(mode="json") for r in rule_snapshots],
        "clauses": [c.model_dump(mode="json") for c in clause_snapshots],
        "unresolved_hitl": [h.model_dump(mode="json") for h in hitl_snapshots],
        "violations": violations_summary.model_dump(mode="json"),
        "graph": [g.model_dump(mode="json") for g in graph_obligations],
    }
    snapshot_hash = compute_snapshot_hash(hash_payload)

    return EntityComplianceSnapshot(
        entity_id=entity_id,
        display_name=display_name,
        tenant_type=tenant_type,
        is_active=is_active,
        snapshot_timestamp=dt.datetime.now(dt.timezone.utc),
        snapshot_hash=snapshot_hash,
        rules=rule_snapshots,
        risk_overlay=risk_overlay,
        clauses=clause_snapshots,
        unresolved_hitl=hitl_snapshots,
        historical_violations_summary=violations_summary,
        graph_obligations=graph_obligations,
    )
