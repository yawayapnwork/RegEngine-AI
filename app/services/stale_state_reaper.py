"""Periodic sweeper that prevents circulars from lingering forever in an
intermediate processing state.

The E2E pipeline transitions a circular INGESTED -> EXTRACTING -> EXTRACTED
-> COMPILING -> AWAITING_HITL/APPROVED/FAILED with checkpointed commits.
If a worker crashes mid-run (the deployed backend is a single Render web
process; a worker-less deployment also queues async jobs that can be
orphaned on restart), a circular can be left in EXTRACTING/COMPILING with
no job still alive to move it forward. The frontend then shows an eternal
spinner with no terminal state -- the single most confusing user-visible
failure this system can have.

This reaper is the safety net: any circular that has NOT been touched
(updated_at) for `stale_circular_reclaim_seconds` while sitting in
EXTRACTING/COMPILING is swept to a terminal FAILED state with a clear,
actionable error message. A deliberately generous window means a
legitimately slow in-progress job (large circular, cold LLM provider) is
never reclaimed while it is still checkpointing.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging

from sqlalchemy import select, update

from app.db.models import Circular, CircularStateTransition
from app.services.orchestrator import ProcessingState

logger = logging.getLogger(__name__)

_STALE_STATES = (ProcessingState.EXTRACTING.value, ProcessingState.COMPILING.value)

_SWEEP_MESSAGE = (
    "Processing stalled in an intermediate state (" + "/".join(_STALE_STATES) +
    ") and was not updated for a long period; marked FAILED by the stale-state "
    "reaper. Re-upload the document to retry."
)


async def _sweep_once(session, stale_after: dt.timedelta) -> int:
    """Marks every circular that has sat untouched in a stale intermediate
    state as FAILED, recording an audit transition for each. Returns the
    number of circulars swept."""
    cutoff = dt.datetime.now(dt.timezone.utc) - stale_after

    victims = list(
        (
            await session.execute(
                select(Circular.id, Circular.processing_state).where(
                    Circular.processing_state.in_(_STALE_STATES),
                    Circular.updated_at < cutoff,
                )
            )
        ).all()
    )
    if not victims:
        return 0

    victim_ids = [row[0] for row in victims]
    await session.execute(
        update(Circular)
        .where(Circular.id.in_(victim_ids))
        .values(processing_state=ProcessingState.FAILED.value, error_message=_SWEEP_MESSAGE)
    )
    for circular_id, from_state in victims:
        session.add(
            CircularStateTransition(
                circular_id=circular_id,
                from_state=from_state,
                to_state=ProcessingState.FAILED.value,
                triggered_by="system:stale-state-reaper",
                error_message=_SWEEP_MESSAGE,
            )
        )
    await session.commit()
    return len(victims)


async def reap_stale_circulars(
    session_factory,
    interval_seconds: int,
    stale_after: dt.timedelta,
    stop_event: asyncio.Event,
) -> None:
    """Background loop wired into app.main's lifespan. Sweeps once per
    interval until the stop event is set; any per-sweep failure is logged
    and does not kill the loop."""
    logger.info("Stale-state reaper started (interval=%ss, reclaim-after=%s).", interval_seconds, stale_after)
    while not stop_event.is_set():
        try:
            async with session_factory() as session:
                swept = await _sweep_once(session, stale_after)
            if swept:
                logger.warning("Stale-state reaper marked %d circular(s) as FAILED.", swept)
        except Exception:  # noqa: BLE001 - a failed sweep must never kill the loop
            logger.exception("Stale-state reaper sweep failed; will retry on the next interval.")

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
        except asyncio.TimeoutError:
            pass  # normal case: interval elapsed without stop() being called
    logger.info("Stale-state reaper stopped.")