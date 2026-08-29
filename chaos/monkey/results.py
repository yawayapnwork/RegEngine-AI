"""Shared result types for chaos.monkey scenario validators
(chaos/monkey/validators.py) and the runner/post-mortem generator that
consume them."""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ChaosCheckResult:
    """The outcome of one injection-and-validate cycle. `passed` means
    "the system behaved the way this scenario expects a resilient
    system to behave" -- for a corrupted-policy scenario that means the
    corruption was CAUGHT; for a fail-safe scenario it means the system
    degraded to the documented safe state rather than doing the unsafe
    thing. `evidence` is always JSON-serializable so it can go straight
    into the post-mortem report and into a machine-readable run log."""

    scenario_id: str
    title: str
    passed: bool
    summary: str
    evidence: dict[str, Any] = field(default_factory=dict)
    ran_at: dt.datetime = field(default_factory=lambda: dt.datetime.now(dt.timezone.utc))


@dataclass
class ChaosRunReport:
    run_id: str
    started_at: dt.datetime
    finished_at: dt.datetime
    results: list[ChaosCheckResult]

    @property
    def all_passed(self) -> bool:
        return all(r.passed for r in self.results)

    @property
    def failed(self) -> list[ChaosCheckResult]:
        return [r for r in self.results if not r.passed]
