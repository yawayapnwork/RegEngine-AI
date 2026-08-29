"""Breach notification engine API: the WebSocket dashboard feed
(Requirement 3) and the REST surface a compliance officer's browser/portal
uses alongside it (initial-load history, acknowledge).

  WS   /v1/incidents/ws/dashboard?token=<JWT>
       Live breach-event stream for the React compliance dashboard. Every
       BreachEvent raised anywhere in the platform (any FastAPI replica,
       any Celery worker) arrives here in real time via
       app.incident.websocket_manager's Redis-pub/sub-fed broadcast.

  GET  /v1/incidents
       Recent breach events (any severity) -- what a dashboard client
       renders immediately on load, before/alongside the live WS stream.

  POST /v1/incidents/{event_id}/acknowledge
       Stops further escalation for a CRITICAL/WARNING event. If the
       event had already reached the PagerDuty stage, also resolves the
       corresponding PagerDuty incident.

WebSocket auth: browsers cannot set an Authorization header on the
handshake request, so the JWT is passed as a query parameter instead --
the one place in this codebase a bearer token travels outside a header.
Query parameters can end up in server access logs, which is an accepted,
scoped trade-off here (dashboard-viewer tokens, not broker credentials)
common to WebSocket auth generally; a stricter deployment could instead
have the client send the token as its first WS message and authenticate
before accepting any further traffic.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, WebSocket, WebSocketDisconnect, status

from app.config import Settings, get_settings
from app.execution.dependencies import get_redis_pool
from app.incident.dependencies import get_dashboard_connection_manager
from app.incident.models import BreachEvent
from app.incident.store import BreachEventStore
from app.incident.tasks import acknowledge_in_pagerduty_if_applicable
from app.incident.websocket_manager import BreachDashboardConnectionManager
from app.security.auth import authenticate_token
from app.security.dependencies import get_current_principal, require_roles
from app.security.models import Principal, Role

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/incidents", tags=["Breach Notification Engine"])

_ALLOWED = require_roles(Role.COMPLIANCE_OFFICER, Role.SYSTEM_ADMIN)


@router.websocket("/ws/dashboard")
async def dashboard_websocket(
    websocket: WebSocket,
    token: str = Query(...),
    manager: BreachDashboardConnectionManager = Depends(get_dashboard_connection_manager),
    settings: Settings = Depends(get_settings),
) -> None:
    principal = await authenticate_token(token, settings)
    if principal is None or not principal.has_role(Role.COMPLIANCE_OFFICER, Role.SYSTEM_ADMIN):
        # Reject BEFORE accept() -- an unauthenticated client should never
        # see a successful handshake, only an immediate close.
        await websocket.close(code=4401)
        return

    await manager.connect(websocket)
    try:
        while True:
            # This endpoint is server-push only; any client message is
            # drained and ignored rather than acted on (a WebSocket ping/
            # keepalive frame from some clients arrives as a text message
            # at this layer) -- receiving is only here to detect
            # disconnects promptly.
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        await manager.disconnect(websocket)


@router.get("/", response_model=list[BreachEvent], dependencies=[Depends(_ALLOWED)])
async def list_recent_incidents(limit: int = 100, settings: Settings = Depends(get_settings)) -> list[BreachEvent]:
    store = BreachEventStore(get_redis_pool(), settings.incident_key_prefix)
    return await store.list_recent(limit)


@router.post("/{event_id}/acknowledge", response_model=BreachEvent, dependencies=[Depends(_ALLOWED)])
async def acknowledge_incident(
    event_id: str,
    principal: Principal = Depends(get_current_principal),
    settings: Settings = Depends(get_settings),
) -> BreachEvent:
    store = BreachEventStore(get_redis_pool(), settings.incident_key_prefix)
    event = await store.acknowledge(event_id, acknowledged_by=principal.subject)
    if event is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"No breach event '{event_id}'.")

    await acknowledge_in_pagerduty_if_applicable(event, settings)
    logger.info("Breach event %s acknowledged by %s.", event_id, principal.subject)
    return event
