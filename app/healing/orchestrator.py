"""Requirement 3 -- the Auto-Correction Loop: at most
`settings.policy_self_healing_max_retries` (default 3) repair rounds,
each running Requirement 3's isolated test cases before a repair is
trusted, tracked via `app.healing.tracking.HealingAttemptTracker`.

Relationship to the existing DLQ/HITL paths (read this before wiring a
new call site): a compile-time `MalformedASTError`
(app.resilience.exceptions) is documented as non-retryable "by design" --
its own docstring says "more inference calls will not fix a source
clause the model cannot structure." That is still true and this module
does not contradict it: `MalformedASTError` covers the compiler itself
producing an invalid AST shape from a *correctly* extracted/audited
rule, which is a compiler bug, not something a repair pass should paper
over. This loop targets a DIFFERENT failure surface -- a policy that
compiled fine but fails at OPA-publish time (a real compile/type error)
or at test-replay time (a runtime crash / type mismatch) -- both of
which ARE plausibly fixable by a bounded repair pass, which is why this
is a new, separate, opt-in pathway (`settings.policy_self_healing_enabled`,
default False) rather than a change to `compile_audited_rule_task`'s
existing behavior.

On exhaustion, this still falls back to the SAME
`app.resilience.dead_letter_queue.DeadLetterQueue` every other
unrecoverable pipeline failure in this codebase uses (category
`POLICY_SELF_HEAL_EXHAUSTED`) -- the healing loop is a new first
attempt, not a replacement for the existing human-review safety net.
"""
from __future__ import annotations

import logging

from app.compiler.models import CompilationResult, CompiledRego, HITLFlag, HITLReasonCode, HITLSeverity, JsonLogicRule
from app.config import Settings, get_settings
from app.healing.detectors import check_rego_structure, run_isolated_test_cases
from app.healing.models import HealingOutcome, PolicyErrorType, PolicyFailure, RepairAttempt, RepairStrategy, SelfHealingResult, TestCaseResult
from app.healing.repair_agent import repair_policy
from app.healing.tracking import HealingAttemptTracker

logger = logging.getLogger(__name__)


class SelfHealingLoop:
    def __init__(self, tracker: HealingAttemptTracker, settings: Settings | None = None) -> None:
        self._tracker = tracker
        self._settings = settings or get_settings()

    async def heal(self, failure: PolicyFailure, test_fixtures: list[dict]) -> SelfHealingResult:
        """`test_fixtures` are Requirement 3's isolated test cases (see
        `app.healing.detectors.run_isolated_test_cases` for the exact
        shape) -- normally a small, fixed set derived from the rule's
        own thresholds (a value just inside the compliant boundary, one
        just outside it), independent of whatever facts happened to be
        in the transaction that originally triggered the failure, so a
        repair is validated against the RULE's intended behavior, not
        just against making one specific bad input stop crashing."""
        max_retries = self._settings.policy_self_healing_max_retries
        attempts: list[RepairAttempt] = []
        current_failure = failure
        outcome = HealingOutcome.ESCALATED_MAX_RETRIES  # overwritten below unless the loop runs 0 iterations, which never happens for max_retries >= 1

        for round_idx in range(max_retries):
            attempt_number = round_idx + 1
            suggestion, strategy = repair_policy(current_failure, self._settings, attempts)

            if not suggestion.can_repair:
                attempt = RepairAttempt(attempt_number=attempt_number, strategy=RepairStrategy.UNFIXABLE, repair_notes=suggestion.repair_notes, passed_tests=False)
                attempts.append(attempt)
                await self._tracker.record_attempt(failure.rule_id, attempt)
                logger.warning("Self-healing: rule_id=%s attempt #%d declined to repair: %s", failure.rule_id, attempt_number, suggestion.repair_notes)
                outcome = HealingOutcome.ESCALATED_UNFIXABLE
                break

            test_results, structural_issues = self._validate_repair(suggestion.repaired_rego, suggestion.repaired_json_logic, test_fixtures)
            passed = not structural_issues and (not test_fixtures or all(t.passed for t in test_results))

            attempt = RepairAttempt(
                attempt_number=attempt_number,
                strategy=strategy,
                repair_notes=suggestion.repair_notes,
                repaired_rego=suggestion.repaired_rego,
                repaired_json_logic=suggestion.repaired_json_logic,
                test_results=test_results,
                passed_tests=passed,
            )

            if passed:
                attempts.append(attempt)
                await self._tracker.record_attempt(failure.rule_id, attempt)
                logger.info("Self-healing: rule_id=%s HEALED on attempt #%d (strategy=%s).", failure.rule_id, attempt_number, strategy.value)
                outcome = HealingOutcome.HEALED
                break

            # Failed its own tests -- becomes the next attempt's input.
            next_failure = current_failure.model_copy(
                update={
                    "rego_code": suggestion.repaired_rego or current_failure.rego_code,
                    "json_logic": suggestion.repaired_json_logic or current_failure.json_logic,
                    "error_message": "; ".join(structural_issues) or "; ".join(t.detail or "" for t in test_results if not t.passed) or "Repair did not pass isolated test cases.",
                    "error_type": PolicyErrorType.SYNTAX_ERROR if structural_issues else current_failure.error_type,
                    "stack_trace": None,
                    "attempt_number": attempt_number,
                }
            )
            attempt.resulting_failure = next_failure
            attempts.append(attempt)
            await self._tracker.record_attempt(failure.rule_id, attempt)
            logger.info("Self-healing: rule_id=%s attempt #%d failed its test cases; retrying.", failure.rule_id, attempt_number)
            current_failure = next_failure
        else:
            outcome = HealingOutcome.ESCALATED_MAX_RETRIES

        final_compilation = None
        hitl_flag = None
        if outcome == HealingOutcome.HEALED:
            final_compilation, hitl_flag = self._build_healed_compilation(failure, attempts[-1])

        if outcome != HealingOutcome.HEALED:
            await self._tracker.reset(failure.rule_id)  # loop concluded (unsuccessfully) -- see reset()'s docstring

        return SelfHealingResult(rule_id=failure.rule_id, outcome=outcome, attempts=attempts, final_compilation=final_compilation, hitl_flag=hitl_flag)

    @staticmethod
    def _validate_repair(repaired_rego: str | None, repaired_json_logic: dict | None, test_fixtures: list[dict]) -> tuple[list[TestCaseResult], list[str]]:
        structural_issues = check_rego_structure(repaired_rego) if repaired_rego else []
        test_results = run_isolated_test_cases(repaired_json_logic, test_fixtures) if (repaired_json_logic and test_fixtures) else []
        return test_results, structural_issues

    @staticmethod
    def _build_healed_compilation(original_failure: PolicyFailure, healed_attempt: RepairAttempt) -> tuple[CompilationResult, HITLFlag]:
        rego = None
        if healed_attempt.repaired_rego and original_failure.package:
            rego = CompiledRego(
                rule_id=original_failure.rule_id,
                package=original_failure.package,
                rego_code=healed_attempt.repaired_rego,
                thresholds_compiled=original_failure.thresholds_compiled,
            )
        json_logic = None
        if healed_attempt.repaired_json_logic and original_failure.data_schema is not None:
            json_logic = JsonLogicRule(
                rule_id=original_failure.rule_id,
                logic=healed_attempt.repaired_json_logic,
                data_schema=original_failure.data_schema,
                violation_message_template=original_failure.violation_message_template or "",
                thresholds_compiled=original_failure.thresholds_compiled,
            )

        hitl_flag = HITLFlag(
            flag_id=f"self-heal:{original_failure.rule_id}:{healed_attempt.attempt_number}",
            rule_id=original_failure.rule_id,
            reason_code=HITLReasonCode.SELF_HEALED_REQUIRES_REVIEW,
            severity=HITLSeverity.ADVISORY,
            description=(
                f"Automatically repaired by the self-healing policy loop after a {original_failure.error_type.value} "
                f"failure (attempt {healed_attempt.attempt_number}, strategy={healed_attempt.strategy.value}). "
                f"Repair notes: {healed_attempt.repair_notes} A human must confirm this repair before it is trusted."
            ),
        )

        compilation = CompilationResult(rule_id=original_failure.rule_id, compiled=True, rego=rego, json_logic=json_logic, hitl_flags=[hitl_flag])
        return compilation, hitl_flag
