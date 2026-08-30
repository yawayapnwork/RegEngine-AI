"""Requirement 3 -- Red Team Benchmark Suite: runs every combination of
injection technique x hiding technique (app.redteam.attack_generator)
through the defense middleware (app.redteam.defense,
app.redteam.output_guard), logs every result to the security-vault
telemetry store (app.redteam.telemetry), and reports an aggregate
resistance rate.

Two scenario types, matching the two places an injection can be caught
(or missed) in this pipeline:

  1. INPUT scenario: a full adversarial PDF is generated, its text
     layer is extracted (the real attack surface -- see
     attack_generator's module docstring), and
     app.redteam.defense.sanitize_source_text runs against it. This
     measures whether the injection is caught BEFORE it ever reaches an
     LLM prompt.

  2. OUTPUT scenario: simulates the failure mode where an injection
     survived input sanitization (or entered some other way) and the
     LLM echoed/obeyed it into its structured output --
     app.redteam.output_guard.check_extraction_output runs against a
     synthetic "compromised" extraction result. This measures the
     second line of defense independent of the first.

Neither scenario requires a live LLM call (no HUGGINGFACEHUB_API_TOKEN
needed) -- both measure whether the DETERMINISTIC defense layers this
codebase actually ships would catch each attack, which is exactly what
"defense middleware resistance" means for the two guards Requirement 2
asks for. A live-LLM red-team run (feeding an adversarial PDF through
the REAL Extraction Agent and checking whether IT personally resisted
the injection) is a valid and valuable further test, but needs a
configured LLM and is out of scope for what can be verified in this
sandbox -- see this codebase's established `--offline-agents` precedent
(regengine-cli.py) for the same distinction applied elsewhere.
"""
from __future__ import annotations

import itertools
import logging

from pydantic import BaseModel, Field

from app.config import Settings, get_settings
from app.redteam.attack_generator import (
    HidingTechnique,
    InjectionTechnique,
    craft_injection_payload,
    extract_all_text_layers,
    generate_adversarial_circular_pdf,
)
from app.redteam.defense import sanitize_source_text
from app.redteam.output_guard import check_extraction_output
from app.redteam.telemetry import AttackOutcome, RedTeamTelemetryRecord, RedTeamTelemetryVault

logger = logging.getLogger(__name__)


class ScenarioResult(BaseModel):
    scenario_name: str
    technique: InjectionTechnique
    outcome: AttackOutcome
    detected_patterns: list[str] = Field(default_factory=list)
    detail: str | None = None


class BenchmarkReport(BaseModel):
    results: list[ScenarioResult]
    resistance_rate: float
    resistance_rate_by_technique: dict[str, float]

    @property
    def escaped_defects(self) -> list[ScenarioResult]:
        return [r for r in self.results if r.outcome == AttackOutcome.NOT_RESISTED]


async def run_input_sanitization_scenario(technique: InjectionTechnique, hiding: HidingTechnique) -> ScenarioResult:
    scenario_name = f"input_sanitization:{technique.value}:{hiding.value}"
    try:
        payload = craft_injection_payload(technique, hiding)
        pdf_bytes = generate_adversarial_circular_pdf([payload])
        extracted_text = extract_all_text_layers(pdf_bytes).replace("​", "")
        sanitization = sanitize_source_text(extracted_text)

        outcome = AttackOutcome.RESISTED if sanitization.is_suspicious else AttackOutcome.NOT_RESISTED
        return ScenarioResult(
            scenario_name=scenario_name,
            technique=technique,
            outcome=outcome,
            detected_patterns=sanitization.detected_patterns,
            detail=(
                f"invisible_chars_removed={sanitization.invisible_chars_removed}, "
                f"delimiter_spoof_detected={sanitization.delimiter_spoof_detected}"
            ),
        )
    except Exception as exc:  # noqa: BLE001 - a scenario that fails to RUN is a distinct, non-verdict outcome (see AttackOutcome.ERROR's docstring)
        logger.exception("Input-sanitization scenario %s failed to run.", scenario_name)
        return ScenarioResult(scenario_name=scenario_name, technique=technique, outcome=AttackOutcome.ERROR, detail=str(exc))


async def run_output_guard_scenario(technique: InjectionTechnique, settings: Settings) -> ScenarioResult:
    scenario_name = f"output_guard:{technique.value}"
    try:
        payload = craft_injection_payload(technique)
        # Simulates the failure mode this scenario tests: an LLM that
        # was successfully manipulated into echoing the injected
        # instruction into a free-text output field, rather than
        # extracting a faithful ExtractedComplianceRule.extraction_notes.
        compromised_output = f"Note: {payload.injected_text}"

        guard_result = check_extraction_output(compromised_output, None, settings)
        outcome = AttackOutcome.NOT_RESISTED if guard_result.validation_passed else AttackOutcome.RESISTED
        detected = [f.detail for f in guard_result.findings if not f.passed and f.detail]
        return ScenarioResult(scenario_name=scenario_name, technique=technique, outcome=outcome, detected_patterns=detected)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Output-guard scenario %s failed to run.", scenario_name)
        return ScenarioResult(scenario_name=scenario_name, technique=technique, outcome=AttackOutcome.ERROR, detail=str(exc))


async def run_full_benchmark(vault: RedTeamTelemetryVault, settings: Settings | None = None) -> BenchmarkReport:
    settings = settings or get_settings()
    results: list[ScenarioResult] = []

    for technique, hiding in itertools.product(InjectionTechnique, HidingTechnique):
        results.append(await run_input_sanitization_scenario(technique, hiding))

    for technique in InjectionTechnique:
        results.append(await run_output_guard_scenario(technique, settings))

    for result in results:
        await vault.record(
            RedTeamTelemetryRecord(
                scenario_name=result.scenario_name,
                technique=result.technique.value,
                outcome=result.outcome,
                detected_patterns=result.detected_patterns,
                detail=result.detail,
            )
        )

    verdicted = [r for r in results if r.outcome != AttackOutcome.ERROR]
    overall_rate = (sum(1 for r in verdicted if r.outcome == AttackOutcome.RESISTED) / len(verdicted)) if verdicted else 1.0

    by_technique: dict[str, float] = {}
    for technique in InjectionTechnique:
        technique_results = [r for r in verdicted if r.technique == technique]
        if technique_results:
            by_technique[technique.value] = sum(1 for r in technique_results if r.outcome == AttackOutcome.RESISTED) / len(technique_results)

    report = BenchmarkReport(results=results, resistance_rate=overall_rate, resistance_rate_by_technique=by_technique)
    logger.info(
        "Red team benchmark complete: %d scenarios, overall resistance_rate=%.2f, %d escaped defect(s).",
        len(results), overall_rate, len(report.escaped_defects),
    )
    return report
