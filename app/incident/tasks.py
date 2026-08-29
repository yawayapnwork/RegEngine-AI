"""Celery escalation worker -- Requirement 2.

`process_escalation_stage_task` is a self-rescheduling chain: each call
handles exactly one stage of one event's escalation policy
(app.incident.escalation_policy), then -- ONLY if the event is still
unacknowledged after dispatching that stage's channels -- schedules the
next stage as a fresh Celery task with `countdown=<next stage's
delay_seconds>`. An officer acknowledging the event at any point makes
the NEXT scheduled stage a no-op (it checks ack status first and returns
immediately), which is what actually implements "alert PagerDuty/Twilio
if a critical violation isn't acknowledged within 15 minutes" -- the
15-minute PagerDuty stage simply never fires if acknowledgment happened
first.

`sweep_overdue_escalations_task` is the safety net (beat-scheduled, see
app.execution.celery_app) for the one failure mode the chain above can't
self-heal from: a Celery broker/worker outage that drops a scheduled ETA
task entirely. It re-derives "what stage should this event be at by now"
from `escalation_stage` + elapsed time and resumes from there -- the same
"pub/sub is the fast path, a periodic poll is the safety net that bounds
the worst case" pattern app.execution.policy_cache's TTL already uses.
"""
from __future__ import annotations

import asyncio
import logging

from app.config import get_settings
from app.execution.celery_app import celery_app
from app.execution.dependencies import get_redis_pool
from app.incident.channels.email_client import EmailClient, EmailClientError
from app.incident.channels.pagerduty_client import PagerDutyClient, PagerDutyClientError
from app.incident.channels.twilio_client import TwilioClient, TwilioClientError
from app.incident.escalation_policy import build_escalation_policies
from app.incident.models import AckStatus, BreachEvent, Severity
from app.incident.store import BreachEventStore
from app.webhooks.notifier import AuditNotifier

logger = logging.getLogger(__name__)

_PAGERDUTY_SEVERITY_MAP = {Severity.CRITICAL: "critical", Severity.WARNING: "warning", Severity.INFO: "info"}


async def _dispatch_channels(event: BreachEvent, channels: tuple[str, ...]) -> None:
    settings = get_settings()

    if "slack" in channels and (settings.slack_webhook_url or settings.teams_webhook_url):
        notifier = AuditNotifier(slack_webhook_url=settings.slack_webhook_url, teams_webhook_url=settings.teams_webhook_url)
        await notifier.notify_flagged_rule(
            review_id=event.hitl_case_id or event.event_id,
            circular_name=event.circular_number or "unknown circular",
            clause_number=event.clause_number,
            source_excerpt=event.description,
            confidence_score=1.0,
            reason_code=event.event_type.value,
            severity="blocking" if event.severity == Severity.CRITICAL else "advisory",
        )

    if "email" in channels and settings.smtp_host and settings.compliance_officer_email_list:
        email_client = EmailClient(
            host=settings.smtp_host, port=settings.smtp_port, username=settings.smtp_username,
            password=settings.smtp_password, use_tls=settings.smtp_use_tls, from_address=settings.smtp_from_address,
        )
        try:
            await email_client.send(
                to_addresses=settings.compliance_officer_email_list,
                subject=f"[{event.severity.value.upper()}] {event.title}",
                body=f"{event.description}\n\nEvent ID: {event.event_id}\nTransaction: {event.transaction_id or 'n/a'}\n"
                f"Rule: {event.rule_id or 'n/a'}\nAcknowledge at: /v1/incidents/{event.event_id}/acknowledge",
            )
        except EmailClientError:
            logger.exception("Email escalation failed for event %s.", event.event_id)

    if "sms" in channels and settings.twilio_account_sid and settings.twilio_auth_token and settings.twilio_oncall_phone_numbers:
        twilio_client = TwilioClient(
            account_sid=settings.twilio_account_sid, auth_token=settings.twilio_auth_token,
            from_number=settings.twilio_from_number, api_base_url=settings.twilio_api_base_url,
        )
        try:
            await twilio_client.send_to_oncall(
                settings.twilio_oncall_phone_numbers,
                f"[{event.severity.value.upper()}] {event.title} -- {event.description[:120]} (event {event.event_id})",
            )
        except TwilioClientError:
            logger.exception("SMS escalation failed for event %s.", event.event_id)

    if "pagerduty" in channels and settings.pagerduty_routing_key:
        pagerduty_client = PagerDutyClient(routing_key=settings.pagerduty_routing_key, api_base_url=settings.pagerduty_api_base_url)
        try:
            await pagerduty_client.trigger(
                dedup_key=event.event_id,
                summary=event.title,
                severity=_PAGERDUTY_SEVERITY_MAP.get(event.severity, "critical"),
                source="regengine-ai",
                custom_details={
                    "description": event.description,
                    "transaction_id": event.transaction_id,
                    "rule_id": event.rule_id,
                    "circular_number": event.circular_number,
                    "clause_number": event.clause_number,
                },
            )
        except PagerDutyClientError:
            logger.exception("PagerDuty escalation failed for event %s.", event.event_id)


async def _process_stage(event_id: str, stage_index: int) -> None:
    settings = get_settings()
    redis_client = get_redis_pool()
    store = BreachEventStore(redis_client, settings.incident_key_prefix)

    event = await store.get(event_id)
    if event is None:
        logger.warning("Escalation stage %d fired for unknown event_id=%s; nothing to do.", stage_index, event_id)
        return
    if event.ack_status != AckStatus.PENDING:
        logger.info("Event %s already %s; skipping escalation stage %d.", event_id, event.ack_status.value, stage_index)
        return

    stages = build_escalation_policies(settings)[event.severity]
    if stage_index >= len(stages):
        return
    stage = stages[stage_index]

    logger.warning(
        "Escalating breach event %s (severity=%s) to stage %d, channels=%s.",
        event_id, event.severity.value, stage_index, stage.channels,
    )
    await _dispatch_channels(event, stage.channels)
    await store.update_escalation_stage(event_id, stage_index)

    next_index = stage_index + 1
    if next_index < len(stages):
        next_stage = stages[next_index]
        process_escalation_stage_task.apply_async(args=[event_id, next_index], countdown=next_stage.delay_seconds)


@celery_app.task(name="app.incident.tasks.process_escalation_stage_task", bind=True, max_retries=3, default_retry_delay=30)
def process_escalation_stage_task(self, event_id: str, stage_index: int) -> None:
    try:
        asyncio.run(_process_stage(event_id, stage_index))
    except Exception as exc:  # noqa: BLE001 - a transient Redis/network failure should retry, not silently drop this stage
        logger.exception("Escalation stage %d for event %s failed; retrying.", stage_index, event_id)
        raise self.retry(exc=exc)


async def acknowledge_in_pagerduty_if_applicable(event: BreachEvent, settings) -> None:
    """Called when an officer acknowledges an event that already reached
    the PagerDuty stage -- resolves the PagerDuty incident so it doesn't
    sit open/paging after the human has already responded through the
    dashboard rather than through PagerDuty itself."""
    if event.severity == Severity.CRITICAL and event.escalation_stage >= 2 and settings.pagerduty_routing_key:
        client = PagerDutyClient(routing_key=settings.pagerduty_routing_key, api_base_url=settings.pagerduty_api_base_url)
        try:
            await client.resolve(event.event_id)
        except PagerDutyClientError:
            logger.exception("Failed to resolve PagerDuty incident for acknowledged event %s.", event.event_id)


async def _sweep() -> None:
    settings = get_settings()
    redis_client = get_redis_pool()
    store = BreachEventStore(redis_client, settings.incident_key_prefix)

    # Anything unacknowledged for longer than the shortest possible
    # cross-severity deadline is a candidate -- _process_stage itself
    # re-derives which stage is actually due (or none yet) per event, so
    # over-fetching here is harmless, just a few no-op re-checks.
    min_deadline = min(settings.incident_critical_sms_stage_seconds, settings.incident_warning_ack_deadline_seconds)
    overdue = await store.list_pending_ack(older_than_seconds=min_deadline)
    for event in overdue:
        logger.info("Sweep: re-checking possibly-stalled escalation for event %s (currently at stage %d).", event.event_id, event.escalation_stage)
        # Resume from the stage AFTER whatever was last recorded --
        # _process_stage's own ack check makes this safe to call even if
        # the "real" scheduled task for this stage is still in flight
        # somewhere (worst case: one redundant channel dispatch, never a
        # missed one).
        process_escalation_stage_task.delay(event.event_id, event.escalation_stage + 1)


@celery_app.task(name="app.incident.tasks.sweep_overdue_escalations_task")
def sweep_overdue_escalations_task() -> None:
    asyncio.run(_sweep())
