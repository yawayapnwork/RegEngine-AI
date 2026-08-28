"""Exponential-backoff-with-jitter retry policy shared by every Celery task
in this package, plus the classifier that decides whether a given
exception is even worth retrying.

Celery's own `retry_backoff=True` / `retry_backoff_max` / `retry_jitter`
task options (configured on each task below) already implement the
textbook "full jitter" algorithm --
`random.uniform(0, min(retry_backoff_max, retry_backoff * 2**retries))` --
so tasks use those native options rather than reimplementing the same
math by hand; `compute_backoff_delay` here exists for the few places
retry timing is computed OUTSIDE Celery's own retry loop (this module's
own tests, and any future non-Celery background loop that wants the exact
same policy app.execution.policy_hot_reload.py's subscriber reconnect
loop uses a simpler fixed table for, deliberately -- see that module for
why a hot-reload *connection* retry and a *task* retry have different
shapes).
"""
from __future__ import annotations

import random

import httpx

# Exception types a retry can plausibly fix: the failure is about
# REACHING a dependency (network, DNS, connection reset, timeout), not
# about the CONTENT of what was sent to it. Deliberately a broad net --
# a false positive here just means one extra retry before the same
# NonRetryableError surfaces anyway; a false negative sends something
# transient straight to the DLQ with no chance to self-heal.
TRANSIENT_EXCEPTION_TYPES: tuple[type[BaseException], ...] = (
    httpx.TransportError,  # connection refused/reset, DNS failure, etc. (covers httpx.ConnectError, ReadTimeout, ...)
    httpx.TimeoutException,
    ConnectionError,  # stdlib -- also the base of redis.exceptions.ConnectionError
    TimeoutError,  # stdlib -- also the base of redis.exceptions.TimeoutError and asyncio.TimeoutError
    OSError,  # broad socket-level failures (e.g. "Network is unreachable")
)


def is_transient(exc: BaseException) -> bool:
    """True if `exc` (or anything in its __cause__ chain, since this
    codebase frequently wraps a lower-level failure -- e.g.
    `ExtractionBackendError(f"...: {primary_exc!r}") from primary_exc` --
    in a typed exception before it propagates) is a network/connectivity
    failure worth retrying."""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        if isinstance(current, TRANSIENT_EXCEPTION_TYPES):
            return True
        seen.add(id(current))
        current = current.__cause__
    return False


def compute_backoff_delay(attempt: int, *, base_seconds: float = 2.0, max_delay_seconds: float = 300.0) -> float:
    """Full-jitter exponential backoff: `random.uniform(0, min(max_delay, base * 2**attempt))`.
    `attempt` is 0-indexed (the delay before the FIRST retry uses attempt=0).
    Matches the algorithm Celery's own `retry_backoff`/`retry_jitter`
    options implement -- provided standalone for call sites (and tests)
    that need the same policy without going through a Celery task's
    retry machinery."""
    if attempt < 0:
        raise ValueError("attempt must be >= 0")
    ceiling = min(max_delay_seconds, base_seconds * (2**attempt))
    return random.uniform(0, ceiling)
