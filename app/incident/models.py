"""Data contracts for the breach notification engine.

Requirement 1's trigger matrix is three fixed severities with fixed
meanings (see `Severity`'s docstring) -- `app.incident.trigger_matrix`
maps real platform events onto them; this module only defines the shapes
that flow through the rest of the pipeline (event -> store -> Celery
escalation -> channel dispatch -> WebSocket dashboard).
"""
from __future__ import annotations

import datetime as dt
import uuid
from enum import Enum

from pydantic import BaseModel, Field


class Severity(str, Enum):
    """Requirement 1's trigger matrix:

    CRITICAL: a direct SEBI (or other regulator) clause violation
    actually executed in production -- e.g. an unsegregated client-fund
    transfer that OPA denied. Requires acknowledgment; escalates to
    PagerDuty/Twilio if unacknowledged within
    settings.incident_critical_ack_deadline_seconds.

    WARNING: an ambiguous rule triggered a FLAGGED decision or a blocking
    HITL flag, requiring urgent-but-not-emergency human review. Requires
    acknowledgment; escalates to email (not PagerDuty/SMS) if
    unacknowledged within settings.incident_warning_ack_deadline_seconds.

    INFO: a routine policy update auto-compiled successfully. No
    acknowledgment required, no escalation -- purely an audit/dashboard
    feed entry.
    """

    CRITICAL = "critical"
    WARNING = "warning"
    INFO = "info"


class BreachEventType(str, Enum):
    CLAUSE_VIOLATION = "clause_violation"          # CRITICAL: a DENY decision on a production transaction
    AMBIGUOUS_HITL = "ambiguous_hitl"                # WARNING: a FLAGGED transaction decision or blocking compiler HITL flag
    POLICY_COMPILED = "policy_compiled"              # INFO: a rule auto-compiled successfully
    FILING_SUBMISSION_FAILED = "filing_submission_failed"  # CRITICAL: app.regulatory_filing exhausted its retry budget submitting a SEBI/MII filing


class AckStatus(str, Enum):
    PENDING = "pending"
    ACKNOWLEDGED = "acknowledged"
    RESOLVED = "resolved"


class BreachEvent(BaseModel):
    """One row in the breach-event feed -- persisted in Redis
    (app.incident.store.BreachEventStore), broadcast to the dashboard
    over the `incident_events_channel` Redis pub/sub topic, and (for
    CRITICAL/WARNING) driven through the Celery escalation worker
    (app.incident.tasks)."""

    event_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    severity: Severity
    event_type: BreachEventType
    title: str
    description: str

    tenant_id: str | None = None
    transaction_id: str | None = None
    rule_id: str | None = None
    circular_number: str | None = None
    clause_number: str | None = None
    hitl_case_id: str | None = None

    ack_status: AckStatus = AckStatus.PENDING
    acknowledged_by: str | None = None
    acknowledged_at: dt.datetime | None = None
    escalation_stage: int = 0

    created_at: dt.datetime = Field(default_factory=lambda: dt.datetime.now(dt.timezone.utc))
    metadata: dict = Field(default_factory=dict)

    @property
    def requires_acknowledgment(self) -> bool:
        return self.severity in (Severity.CRITICAL, Severity.WARNING)
