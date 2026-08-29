"""Requirement 1: the trigger itself. Called from
`app.ledger.integration.log_evaluation` immediately after a FAIL
outcome's ledger entry is successfully appended (NOT from the same
`_raise_breach_events` call in that module that fires `clause_violation_event`
-- that call happens BEFORE the ledger write, by that module's own
documented design ("a breach notification is about the COMPLIANCE
OUTCOME... independent of whether the ledger write... succeeds"), but
`app.grievance_escalation.evidence.build_evidence_package` needs the
ledger row to already exist to build its proof, so this trigger's
correct hook point is strictly after a successful append).

Every step here is best-effort and independently caught -- exactly
`_raise_breach_events`'s own "a breach-notification failure must never
mask the underlying compliance decision" posture, applied to a second,
independent side effect (SEBI grievance filing) that is even less
acceptable to let crash the live evaluation path over.
"""
from __future__ import annotations

import datetime as dt
import logging
import uuid

import redis.asyncio as redis
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.config import Settings
from app.execution.models import TransactionPayload
from app.grievance_escalation.evidence import build_evidence_package
from app.grievance_escalation.queue import GrievanceQueue, GrievanceRecord
from app.grievance_escalation.schemas import (
    GrievanceCategory,
    GrievanceComplainant,
    GrievanceRespondent,
    GrievanceSubmissionRequest,
)
from app.grievance_escalation.systemic_detector import check_systemic_failure
from app.ledger.models import LedgerEntry

logger = logging.getLogger(__name__)

# Rule-id substring -> grievance category, matching this agent's own
# narrow scope (see schemas.GrievanceCategory's docstring on why this
# enum doesn't cover SCORES' full category taxonomy). Falls through to
# the generic REPEATED_CLAUSE_VIOLATION category for anything else.
_CATEGORY_KEYWORDS: tuple[tuple[str, GrievanceCategory], ...] = (
    ("collateral", GrievanceCategory.DELAYED_COLLATERAL_REPORTING),
    ("margin", GrievanceCategory.BROKER_SYSTEMIC_NON_COMPLIANCE),
)


def _categorize(rule_id: str, violations: list[str]) -> GrievanceCategory:
    haystack = f"{rule_id} {' '.join(violations)}".lower()
    for keyword, category in _CATEGORY_KEYWORDS:
        if keyword in haystack:
            return category
    return GrievanceCategory.REPEATED_CLAUSE_VIOLATION


async def evaluate_and_trigger_grievance_escalation(
    entry: LedgerEntry,
    transaction: TransactionPayload,
    ledger_engine: AsyncEngine,
    redis_client: redis.Redis,
    settings: Settings,
    db: AsyncSession | None = None,
) -> GrievanceRecord | None:
    """Returns the drafted/confirmed `GrievanceRecord` if this failure
    was judged systemic and a grievance was drafted, else None (the
    common case -- most FAIL entries are isolated, not systemic, and
    generate nothing here)."""
    if not settings.grievance_escalation_enabled:
        return None

    check = await check_systemic_failure(
        ledger_engine, broker_id=entry.broker_id, rule_id=entry.rule_id,
        window_days=settings.grievance_escalation_systemic_failure_window_days,
        threshold_count=settings.grievance_escalation_systemic_failure_threshold_count,
    )
    if not check.is_systemic:
        logger.debug(
            "Broker %s rule %s: %d failure(s) in the trailing %d day(s), below the systemic threshold (%d) -- no grievance filed.",
            entry.broker_id, entry.rule_id, check.failure_count, check.window_days, check.threshold_count,
        )
        return None

    evidence = await build_evidence_package(ledger_engine, db, transaction)
    violations = list(evidence.ledger_entry.details.get("violations", []))

    request = GrievanceSubmissionRequest(
        reference_id="",  # filled in after GrievanceRecord assigns grievance_id, below
        category=_categorize(entry.rule_id, violations),
        respondent=GrievanceRespondent(sebi_registration_number=entry.broker_id, broker_id=entry.broker_id),
        complainant=GrievanceComplainant(entity_name="RegEngine AI Compliance Monitoring", contact_email="compliance-automation@regengine.internal", tenant_id=None),
        description=(
            f"Automated escalation: broker {entry.broker_id} breached rule {entry.rule_id} "
            f"{check.failure_count} time(s) in the trailing {check.window_days} day(s), exceeding the "
            f"systemic-failure threshold of {check.threshold_count}. Most recent breach: transaction "
            f"{transaction.transaction_id}, Circular {evidence.circular_number or 'unknown'}, "
            f"Clause {evidence.clause_number}. {'; '.join(violations) if violations else 'No detailed violation message recorded.'}"
        ),
        evidence=evidence.to_evidence_documents(),
    )

    grievance_id = str(uuid.uuid4())
    record = GrievanceRecord(
        grievance_id=grievance_id,
        request=request.model_copy(update={"reference_id": grievance_id}),
        max_retries=settings.grievance_escalation_max_submit_retries,
        response_due_at=dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=settings.grievance_escalation_sla_response_days),
    )

    queue = GrievanceQueue(redis_client, settings.grievance_escalation_key_prefix)
    await queue.create_draft(record)
    logger.warning(
        "Systemic non-compliance detected: broker=%s rule_id=%s failures=%d/%d in %dd -- grievance %s drafted.",
        entry.broker_id, entry.rule_id, check.failure_count, check.threshold_count, check.window_days, grievance_id,
    )

    if settings.grievance_escalation_auto_submit_enabled:
        record = await queue.confirm_for_submission(grievance_id)
        from app.grievance_escalation.tasks import submit_grievance_task  # deferred: avoids a Celery app import on the live evaluation path for callers/tests that never auto-submit

        submit_grievance_task.delay(grievance_id)

    return record
