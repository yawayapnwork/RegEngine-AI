"""Real-time dashboard feed: a per-process WebSocket connection manager,
fed by a Redis pub/sub subscriber -- structurally identical to
app.execution.policy_hot_reload.PolicyHotReloadSubscriber (same
reconnect-with-backoff loop, same "one subscriber per FastAPI worker
process" rationale), because the underlying problem is the same one:
an event published from ANY process (a Celery worker, another FastAPI
replica) must reach WebSocket clients connected to THIS process, and
there is no way to reach into another process's open sockets except by
having every process independently subscribe to the same channel.
"""
from __future__ import annotations

import asyncio
import logging

import redis.asyncio as redis
from fastapi import WebSocket

from app.incident.models import BreachEvent

logger = logging.getLogger(__name__)

_RECONNECT_BACKOFF_SECONDS = (1, 2, 5, 10, 30)


class BreachDashboardConnectionManager:
    """Holds every WebSocket connection open to THIS process. A React
    dashboard client connects to whichever FastAPI replica its load
    balancer routes it to; each replica's manager only ever broadcasts to
    its own locally-held sockets, fed by BreachEventBroadcastSubscriber
    below so every replica converges on the same event stream."""

    def __init__(self) -> None:
        self._connections: set[WebSocket] = set()
        self._lock = asyncio.Lock()

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        async with self._lock:
            self._connections.add(websocket)
        logger.info("Dashboard WebSocket connected (%d active on this process).", len(self._connections))

    async def disconnect(self, websocket: WebSocket) -> None:
        async with self._lock:
            self._connections.discard(websocket)
        logger.info("Dashboard WebSocket disconnected (%d active on this process).", len(self._connections))

    async def broadcast(self, event: BreachEvent) -> None:
        payload = event.model_dump_json()
        async with self._lock:
            connections = list(self._connections)
        dead: list[WebSocket] = []
        for ws in connections:
            try:
                await ws.send_text(payload)
            except Exception:  # noqa: BLE001 - a dead/closing socket must not stop the fanout to the rest
                dead.append(ws)
        if dead:
            async with self._lock:
                for ws in dead:
                    self._connections.discard(ws)


class BreachEventBroadcastSubscriber:
    def __init__(self, redis_client: redis.Redis, channel: str, manager: BreachDashboardConnectionManager) -> None:
        self._redis = redis_client
        self._channel = channel
        self._manager = manager
        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        attempt = 0
        while not self._stop.is_set():
            try:
                await self._subscribe_and_listen()
                attempt = 0
            except (redis.ConnectionError, redis.TimeoutError) as exc:
                delay = _RECONNECT_BACKOFF_SECONDS[min(attempt, len(_RECONNECT_BACKOFF_SECONDS) - 1)]
                logger.warning("Breach-event subscriber lost its Redis connection (%s); reconnecting in %ds.", exc, delay)
                attempt += 1
                await self._sleep_or_stop(delay)
            except Exception:  # noqa: BLE001 - this loop must never crash-exit
                logger.exception("Unexpected error in breach-event subscriber; restarting listen loop.")
                await self._sleep_or_stop(_RECONNECT_BACKOFF_SECONDS[0])

    async def _sleep_or_stop(self, delay: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=delay)
        except asyncio.TimeoutError:
            pass

    async def _subscribe_and_listen(self) -> None:
        pubsub = self._redis.pubsub()
        await pubsub.subscribe(self._channel)
        logger.info("Breach-event subscriber connected; listening on '%s'.", self._channel)
        try:
            while not self._stop.is_set():
                message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                if message is None:
                    continue
                try:
                    event = BreachEvent.model_validate_json(message["data"])
                except Exception:  # noqa: BLE001 - a malformed event must not kill the subscriber
                    logger.error("Discarding malformed breach event message.")
                    continue
                await self._manager.broadcast(event)
        finally:
            await pubsub.unsubscribe(self._channel)
            await pubsub.aclose()
