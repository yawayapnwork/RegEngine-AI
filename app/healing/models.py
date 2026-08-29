"""Pydantic models for the self-healing policy repair loop
(app.healing) -- what a failure looks like, what one repair attempt
produced, and the loop's final outcome.
"""
from __future__ import annotations

import datetime as dt
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from app.compiler.models import CompilationResult, HITLFlag


class PolicyErrorType(str, Enum):
    """WHICH of Requirement 1's three failure classes this is -- drives
    both how `app.healing.detectors` classifies an incoming failure and
    which repair strategy `app.healing.repair_agent` tries first."""

    SYNTAX_ERROR = "syntax_error"            # unbalanced braces, missing package/default, malformed Rego text
    COMPILE_ERROR = "compile_error"          # OPA's compiler rejected otherwise-well-formed Rego (type/safety errors)
    RUNTIME_CRASH = "runtime_crash"          # a test case's evaluation raised instead of returning a decision
    INVALID_JSON_LOGIC_TYPE = "invalid_json_logic_type"  # a JSON-Logic operand has the wrong type (e.g. a numeric threshold compiled as a string)


class RepairStrategy(str, Enum):
    DETERMINISTIC_FIX = "deterministic_fix"   # a known, mechanically-safe transformation (app.healing.repair_agent.apply_deterministic_fixes)
    LLM_REPAIR_AGENT = "llm_repair_agent"     # escalated to the CrewAI Policy Repair Agent
    UNFIXABLE = "unfixable"                    # neither strategy produced a candidate this round


class TestCaseResult(BaseModel):
    description: str
    passed: bool
    detail: str | None = None


class PolicyFailure(BaseModel):
    """Requirement 2's three inputs to the repair agent, plus enough
    identity/history to resume a loop: the failed Rego, the error, and
    the original legal text -- captured together so nothing about *why*
    this failed has to be re-derived downstream."""

    rule_id: str
    circular_number: str | None = None
    clause_number: str | None = None
    source_clause_text: str = Field(..., description="The original SEBI legal text this policy was compiled from -- Requirement 2's 'legal text context'.")

    rego_code: str | None = None
    json_logic: dict[str, Any] | None = None

    # Carried through so a HEALED outcome can reconstruct a full
    # CompilationResult (CompiledRego + JsonLogicRule) without
    # re-deriving metadata the original compile step already computed --
    # see app.compiler.models.CompiledRego / JsonLogicRule for what each
    # of these backs.
    package: str | None = None
    data_schema: dict[str, str] | None = None
    violation_message_template: str | None = None
    thresholds_compiled: int = 0

    error_type: PolicyErrorType
    error_message: str
    stack_trace: str | None = Field(None, description="Requirement 2's 'error stack trace' -- full traceback text when available (e.g. a Python exception from the isolated test runner); None for a bare OPA API error string.")

    attempt_number: int = Field(0, description="0 = the original failure that entered the loop; incremented each retry.")
    failed_at: dt.datetime = Field(default_factory=lambda: dt.datetime.now(dt.timezone.utc))


class RepairAttempt(BaseModel):
    """One iteration of Requirement 3's loop: what was tried, what came
    out, and whether it survived the isolated test cases."""

    attempt_number: int
    strategy: RepairStrategy
    repair_notes: str = Field(..., description="Human-readable explanation of what was changed and why -- always populated, even for UNFIXABLE, so a HITL reviewer or the DLQ entry carries the reasoning, not just a diff.")
    repaired_rego: str | None = None
    repaired_json_logic: dict[str, Any] | None = None
    test_results: list[TestCaseResult] = Field(default_factory=list)
    passed_tests: bool = False
    resulting_failure: PolicyFailure | None = Field(None, description="Set when this attempt's own test run failed -- becomes the next attempt's input.")
    attempted_at: dt.datetime = Field(default_factory=lambda: dt.datetime.now(dt.timezone.utc))


class HealingOutcome(str, Enum):
    HEALED = "healed"                              # a repair passed every isolated test case within the retry budget
    ESCALATED_MAX_RETRIES = "escalated_max_retries"  # retries exhausted with no passing repair
    ESCALATED_UNFIXABLE = "escalated_unfixable"      # a strategy determined (before spending a retry) that it cannot safely proceed


class SelfHealingResult(BaseModel):
    rule_id: str
    outcome: HealingOutcome
    attempts: list[RepairAttempt]
    final_compilation: CompilationResult | None = Field(None, description="Set only when outcome == HEALED -- the repaired policy in the same shape app.compiler.pipeline.compile_audited_rule produces, ready for the existing HITL/publish pipeline.")
    hitl_flag: HITLFlag | None = Field(None, description="Attached to final_compilation.hitl_flags when HEALED -- an ADVISORY flag (never blocking; the repair already passed its tests) so a human still reviews an auto-repaired rule before it is trusted, exactly like every other HITL flag in this system.")
