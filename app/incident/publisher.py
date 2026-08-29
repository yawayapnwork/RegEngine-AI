"""Single entry point every trigger-matrix call site
(app.incident.trigger_matrix's three builders) goes through to actually
raise a breach event -- persistence, real-time fan-out, and escalation
scheduling all start here, so a call site only ever has to build the
`BreachEvent` and call `raise_breach_event`; it never has to know about
Redis pub/sub or Celery itself.
"""
from __future__ import annotations

import logging

import redis.asyncio as redis

from app.config import Settings
from app.incident.models import BreachEvent
from app.incident.store import BreachEventStore

logger = logging.getLogger(__name__)


async def raise_breach_event(event: BreachEvent, redis_client: redis.Redis, settings: Settings) -> None:
    store = BreachEventStore(redis_client, settings.incident_key_prefix)
    await store.save(event)

    # Real-time dashboard fan-out (Requirement 3) -- every FastAPI
    # worker's BreachEventBroadcastSubscriber picks this up and pushes it
    # to its own locally-connected WebSocket clients.
    try:
        await redis_client.publish(settings.incident_events_channel, event.model_dump_json())
    except Exception:  # noqa: BLE001 - a pub/sub publish failure must not prevent escalation from still being scheduled
        logger.exception("Failed to publish breach event %s to dashboard channel; escalation will still proceed.", event.event_id)

    # Multi-stage escalation (Requirement 2) -- only events that require
    # acknowledgment (CRITICAL/WARNING) get scheduled; an INFO event's
    # only job was the dashboard push above.
    if event.requires_acknowledgment:
        from app.incident.tasks import process_escalation_stage_task  # deferred: avoids a Celery app import at module load for pure-async callers (e.g. tests) that never touch the escalation path

        process_escalation_stage_task.delay(event.event_id, 0)

    logger.info(
        "Breach event raised: id=%s severity=%s type=%s title=%r",
        event.event_id, event.severity.value, event.event_type.value, event.title,
    )
