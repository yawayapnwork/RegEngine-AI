"""Multi-stage escalation policies -- Requirement 2's "escalate on-call
compliance officers via PagerDuty/Twilio if a critical violation isn't
acknowledged within 15 minutes," generalized to every severity that
requires acknowledgment.

Each `EscalationStage` fires `delay_seconds` after the PREVIOUS stage
(stage 0 always fires at delay_seconds=0, i.e. immediately) -- see
app.incident.tasks for how each stage is scheduled as its own Celery ETA
task that first checks whether the event was acknowledged before firing,
so an officer acknowledging at minute 3 correctly prevents the 5-minute
SMS stage and the 15-minute PagerDuty stage from ever firing.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.config import Settings
from app.incident.models import Severity


@dataclass(frozen=True)
class EscalationStage:
    stage_index: int
    delay_seconds: int  # relative to the PREVIOUS stage, not to event creation
    channels: tuple[str, ...]  # "slack" | "email" | "sms" | "pagerduty"


# The dashboard WebSocket feed is deliberately NOT one of these channels:
# every event (including INFO, which has no escalation stages at all) is
# pushed to the dashboard immediately and unconditionally by
# app.incident.publisher.raise_breach_event, once, regardless of
# severity -- it is a permanent parallel feed, not an escalation rung.
# Modeling it as a stage-0 channel here would either duplicate that push
# or require stage 0 to special-case it; keeping it wholly outside the
# escalation-stage model avoids both.
def build_escalation_policies(settings: Settings) -> dict[Severity, list[EscalationStage]]:
    """Built from settings (not module-level constants) so the exact
    timing/channel matrix is environment-configurable without a code
    change -- e.g. a smaller/24x7 compliance team might want the SMS
    stage to fire immediately rather than at 5 minutes."""
    return {
        Severity.CRITICAL: [
            EscalationStage(0, 0, ("slack", "email")),
            EscalationStage(1, settings.incident_critical_sms_stage_seconds, ("sms",)),
            EscalationStage(
                2,
                settings.incident_critical_ack_deadline_seconds - settings.incident_critical_sms_stage_seconds,
                ("pagerduty",),
            ),
        ],
        Severity.WARNING: [
            EscalationStage(0, 0, ("slack",)),
            EscalationStage(1, settings.incident_warning_ack_deadline_seconds, ("email",)),
        ],
        Severity.INFO: [],
    }
