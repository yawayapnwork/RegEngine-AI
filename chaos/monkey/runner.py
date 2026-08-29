"""Orchestrates a full Compliance Chaos Monkey run: all three scenarios,
collected into one `ChaosRunReport`, with an automated post-mortem
written to disk on every run (Requirement 3) -- pass or fail, since a
FAILED chaos run (a defense that didn't catch its injected fault) is
exactly the finding a reliability program most needs on record.
"""
from __future__ import annotations

import datetime as dt
import logging
import uuid

from app.config import Settings, get_settings
from chaos.monkey.postmortem import render_postmortem, write_postmortem
from chaos.monkey.results import ChaosCheckResult, ChaosRunReport
from chaos.monkey.validators import (
    run_scenario_corrupted_policy_logic,
    run_scenario_ledger_network_dropout,
    run_scenario_malformed_pdf_ingestion,
)

logger = logging.getLogger(__name__)


class ChaosMonkeyDisabledError(RuntimeError):
    """Raised when a run is attempted with `settings.chaos_monkey_enabled=False`
    -- see app.config.Settings' docstring on that flag: this is a safety
    rail against accidentally running fault injection outside a
    deliberately-configured staging environment, not a feature gate."""


class ChaosMonkeyRunner:
    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()

    async def run_all(self) -> ChaosRunReport:
        if not self._settings.chaos_monkey_enabled:
            raise ChaosMonkeyDisabledError(
                "settings.chaos_monkey_enabled is False -- refusing to run fault injection. "
                "Set CHAOS_MONKEY_ENABLED=true only in an environment file for a staging "
                "deployment you intend to chaos-test."
            )

        run_id = str(uuid.uuid4())
        started_at = dt.datetime.now(dt.timezone.utc)
        logger.warning("Compliance Chaos Monkey run %s starting -- injecting faults into this environment's code paths.", run_id)

        results: list[ChaosCheckResult] = []
        results.append(run_scenario_corrupted_policy_logic())
        results.append(await run_scenario_ledger_network_dropout())
        results.append(await run_scenario_malformed_pdf_ingestion(self._settings))

        finished_at = dt.datetime.now(dt.timezone.utc)
        report = ChaosRunReport(run_id=run_id, started_at=started_at, finished_at=finished_at, results=results)

        for r in results:
            level = logging.INFO if r.passed else logging.ERROR
            logger.log(level, "[chaos:%s] %s -- %s", r.scenario_id, "PASS" if r.passed else "FAIL", r.summary)

        postmortem_path = write_postmortem(report, self._settings.chaos_monkey_postmortem_dir)
        logger.warning("Compliance Chaos Monkey run %s finished (all_passed=%s). Post-mortem: %s", run_id, report.all_passed, postmortem_path)

        return report

    def render_report(self, report: ChaosRunReport) -> str:
        return render_postmortem(report)
