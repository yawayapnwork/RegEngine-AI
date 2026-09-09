"""Audit logging integration for M&A Due-Diligence comparisons.

Logs every initiated comparison to the tamper-evident hash-chained compliance audit ledger.
"""
from __future__ import annotations

import datetime as dt
import logging
from sqlalchemy.ext.asyncio import AsyncEngine

from app.ledger.models import ComplianceEvaluationEvent, EvaluationOutcome
from app.ledger.service import LedgerService
from app.mna_due_diligence.models import MNADueDiligenceReport

logger = logging.getLogger(__name__)


async def log_mna_due_diligence_audit_event(
    engine: AsyncEngine,
    report: MNADueDiligenceReport,
) -> None:
    """Appends an immutable audit event to the hash-chained compliance audit ledger.

    Records:
    - Initiating principal subject
    - Entity A and Entity B IDs
    - Snapshot hashes
    - Total findings and overall risk score
    """
    ledger_service = LedgerService(engine)
    event = ComplianceEvaluationEvent(
        broker_id=report.entity_a_id,
        transaction_id=f"mna_job_{report.job_id}",
        evaluated_at=dt.datetime.now(dt.timezone.utc),
        circular_id="MNA_DUE_DILIGENCE",
        clause_hash=f"{report.entity_a_snapshot_hash[:32]}:{report.entity_b_snapshot_hash[:32]}",
        section_reference=f"{report.entity_a_id}_VS_{report.entity_b_id}",
        rule_id="MNA_DUE_DILIGENCE_AUDIT",
        evaluation_result=EvaluationOutcome.PASS,
        details={
            "job_id": report.job_id,
            "initiator_subject": report.initiator_subject,
            "entity_a_id": report.entity_a_id,
            "entity_b_id": report.entity_b_id,
            "entity_a_snapshot_hash": report.entity_a_snapshot_hash,
            "entity_b_snapshot_hash": report.entity_b_snapshot_hash,
            "findings_count": len(report.findings),
            "overall_risk_score": report.overall_risk_score,
            "summary_counts": report.summary_counts,
        },
    )
    try:
        entry = await ledger_service.append_entry(event)
        logger.info(
            "M&A Due-Diligence audit logged to ledger: seq=%d job_id=%s",
            entry.sequence_num,
            report.job_id,
        )
    except Exception as exc:
        logger.error("Failed to append M&A due-diligence event to ledger: %s", exc)
        raise
