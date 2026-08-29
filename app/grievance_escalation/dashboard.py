"""Requirement 3's "update the internal HITL compliance dashboard":
reuses `app.incident`'s EXISTING real-time dashboard pipeline
(`app.incident.publisher.raise_breach_event` -> Redis pub/sub ->
`app.incident.websocket_manager.BreachEventBroadcastSubscriber` -> every
FastAPI worker's connected WebSocket clients, plus persistence via
`app.incident.store.BreachEventStore` for `GET /v1/incidents`) rather
than building a second, parallel dashboard channel -- see this
package's design notes on why `BreachEventType` gained two new values
(`GRIEVANCE_FILED`, `GRIEVANCE_STATUS_UPDATE`) instead.
"""
from __future__ import annotations

import redis.asyncio as redis

from app.config import Settings
from app.grievance_escalation.queue import GrievanceRecord
from app.incident.models import BreachEvent, BreachEventType, Severity
from app.incident.publisher import raise_breach_event


async def notify_grievance_filed(record: GrievanceRecord, redis_client: redis.Redis, settings: Settings) -> None:
    event = BreachEvent(
        severity=Severity.WARNING,
        event_type=BreachEventType.GRIEVANCE_FILED,
        title=f"SCORES grievance drafted: {record.respondent.broker_id}",
        description=record.request.description,
        tenant_id=record.respondent.broker_id,
        transaction_id=None,
        rule_id=None,
        circular_number=None,
        clause_number=None,
        metadata={"grievance_id": record.grievance_id, "category": record.request.category.value, "response_due_at": record.response_due_at.isoformat()},
    )
    await raise_breach_event(event, redis_client, settings)


async def notify_grievance_status_changed(record: GrievanceRecord, redis_client: redis.Redis, settings: Settings) -> None:
    """Requirement 3: pushed every time a poll observes a status change
    -- see app.grievance_escalation.tasks.poll_grievance_status_task,
    the only call site (a poll that finds NO change never calls this,
    so the dashboard/incident history isn't spammed with "still
    submitted" no-op updates every polling interval)."""
    event = BreachEvent(
        severity=Severity.INFO,
        event_type=BreachEventType.GRIEVANCE_STATUS_UPDATE,
        title=f"SCORES grievance {record.scores_reference_number or record.grievance_id}: {record.scores_status.value if record.scores_status else 'unknown'}",
        description=record.resolution_summary or "No resolution summary provided by SCORES.",
        tenant_id=record.respondent.broker_id,
        transaction_id=None,
        rule_id=None,
        circular_number=None,
        clause_number=None,
        metadata={
            "grievance_id": record.grievance_id, "scores_reference_number": record.scores_reference_number,
            "status": record.status.value, "is_overdue": record.is_overdue,
            "response_due_at": record.response_due_at.isoformat(),
        },
    )
    await raise_breach_event(event, redis_client, settings)
