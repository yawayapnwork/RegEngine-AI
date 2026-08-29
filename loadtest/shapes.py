"""Custom Locust load shapes for staged ramp-up and breakpoint (find-the-
ceiling) testing against the OPA evaluation endpoint.

Important: a `LoadTestShape` controls SIMULATED USER COUNT over time, not
requests-per-second directly -- Locust has no built-in way to dictate "hit
exactly N rps" (that requires either very precise wait_time tuning per
stage, or a third-party rps-shaping plugin this project deliberately
avoids depending on). The relationship between users and achieved RPS,
by Little's Law, is approximately:

    achieved_rps ~= user_count / (think_time + mean_response_time)

With `wait_time = constant(0)` (locustfile.py's default, simulating a
broker OMS firing orders back-to-back) and a healthy mean response time
in the tens of milliseconds, each simulated user can drive tens to
low-hundreds of requests/sec on its own -- so reaching 10,000 rps needs on
the order of a few hundred to a few thousand concurrent users, NOT
10,000 users at a 1:1 ratio. The exact ratio depends on the system's
actual response time at each stage, which is precisely what a breakpoint
test is trying to discover -- so treat the `--users` figures below as a
starting point to tune from your first run's observed rps, not a fixed
prescription.

Locust only supports ONE active `LoadTestShape` subclass per run --
loading two (breakpoint ramp + soak) would make it refuse to start with
"Multiple LoadTestShape classes found". `LOADTEST_SHAPE` (env var,
default "breakpoint") picks which one actually inherits from
`LoadTestShape` at import time; the other stays a plain mixin with
identical logic, available for the other mode without needing a second
file or a second locust invocation flag.
"""
from __future__ import annotations

import os

from locust import LoadTestShape

_ACTIVE_SHAPE = os.environ.get("LOADTEST_SHAPE", "breakpoint")


class _BreakpointRampLogic:
    """Stepped ramp: (duration_seconds, target_user_count, spawn_rate).

    Each step holds long enough (2-3 min) for the system to reach steady
    state and for Prometheus rate()/histogram_quantile() windows to
    stabilize before breakpoint_analysis.py samples them, then steps up.
    The final stage is a deliberate overshoot past the 10,000 rps target
    -- finding exactly where the SLA breaks requires going past it, not
    stopping exactly at it.
    """

    STAGES: list[tuple[int, int, int]] = [
        (120, 500, 50),      # warm-up / baseline
        (120, 2000, 100),    # normal peak-hours load
        (120, 5000, 150),    # elevated / partial market-open surge
        (120, 8000, 200),    # approaching target
        (180, 10000, 250),   # sustained target load (the SLA's actual claim)
        (180, 14000, 300),   # deliberate overshoot -- find the real breakpoint
    ]

    def tick(self):
        run_time = self.get_run_time()
        elapsed = 0
        for duration, users, spawn_rate in self.STAGES:
            elapsed += duration
            if run_time < elapsed:
                return (users, spawn_rate)
        return None  # stop the test after the last stage completes


class _SoakLogic:
    """Flat, extended hold at the target load -- for confirming the system
    doesn't degrade over time (a slow memory leak, a connection pool that
    never releases, ledger write latency creeping up as the table grows
    without vacuum/index maintenance keeping pace) rather than testing the
    ceiling itself. Run this AFTER the breakpoint ramp has established
    what the safe target user count actually is (set LOADTEST_SHAPE=soak)."""

    TARGET_USERS = 10000
    SPAWN_RATE = 250
    DURATION_SECONDS = 3600  # 1 hour soak

    def tick(self):
        run_time = self.get_run_time()
        if run_time < self.DURATION_SECONDS:
            return (self.TARGET_USERS, self.SPAWN_RATE)
        return None


if _ACTIVE_SHAPE == "soak":
    class ActiveLoadShape(_SoakLogic, LoadTestShape):
        pass
else:
    class ActiveLoadShape(_BreakpointRampLogic, LoadTestShape):
        pass
