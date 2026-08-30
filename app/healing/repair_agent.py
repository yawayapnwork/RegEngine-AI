"""Requirement 2 -- the Self-Healing Agent itself: a cheap, deterministic
fast path for mechanically-safe fixes, escalating to a CrewAI-based LLM
Policy Repair Agent for anything that needs actual judgment about the
source clause text. Mirrors app.llm_ops's local-tier-before-frontier-tier
cost-routing philosophy already established in this codebase: most of
Requirement 3's named defect classes ("syntax errors, missing field
checks, or invalid JSON-Logic types") have a small, well-defined,
mechanically-safe transformation and don't need an LLM call at all.

crewai and its Hugging Face LLM binding are imported lazily inside
`build_repair_agent`/`run_llm_repair`, exactly like app.agents.crew, so
this module (and its deterministic fast path) stays importable and
testable without crewai installed or an HUGGINGFACEHUB_API_TOKEN
configured -- see tests/test_healing.py, which exercises the full retry
loop through the deterministic path only, matching regengine-cli.py's own
`--offline-agents` precedent for testing this pipeline without a live
LLM.
"""
from __future__ import annotations

import logging
import re

from pydantic import BaseModel, Field

from app.config import Settings, get_settings
from app.healing.models import PolicyErrorType, PolicyFailure, RepairAttempt, RepairStrategy
from app.healing.repair_prompts import POLICY_REPAIR_AGENT_SYSTEM_PROMPT, build_repair_task_description

logger = logging.getLogger(__name__)

_NUMERIC_STRING_RE = re.compile(r"^-?\d+(\.\d+)?$")


class RepairSuggestion(BaseModel):
    """The Policy Repair Agent's structured output contract (both the
    deterministic fast path and the LLM agent produce this same shape,
    so `app.healing.orchestrator` never needs to know which one ran)."""

    can_repair: bool
    repaired_rego: str | None = None
    repaired_json_logic: dict | None = None
    repair_notes: str = Field(..., description="Always populated -- what changed and why, or why this could not be safely repaired.")


# --------------------------------------------------------------------------
# Deterministic fast path
# --------------------------------------------------------------------------


def _inject_default_allow(rego_code: str) -> str:
    """Inserts `default allow := false` immediately after the `package`
    line -- the exact position app.compiler.rego_compiler itself always
    emits it at (see that module's docstring's generated-structure
    example), so a repaired module matches this codebase's own
    established Rego layout, not an arbitrary insertion point."""
    lines = rego_code.splitlines()
    for i, line in enumerate(lines):
        if line.strip().startswith("package "):
            insert_at = i + 1
            # Skip a blank line and/or an `import` line right after
            # `package`, matching rego_compiler's own generated layout.
            while insert_at < len(lines) and (lines[insert_at].strip() == "" or lines[insert_at].strip().startswith("import ")):
                insert_at += 1
            lines.insert(insert_at, "\ndefault allow := false")
            return "\n".join(lines)
    # No package line at all -- not this fixer's job (that's a
    # SYNTAX_ERROR the deterministic fixer declines, per its docstring).
    return rego_code


def _coerce_numeric_json_logic_types(node):
    """Walks a JSON-Logic AST and coerces any numeric-looking STRING
    operand (e.g. "20" where the compiler should have emitted 20) into a
    real JSON number -- app.compiler.jsonlogic_compiler always emits
    threshold values as numbers (see its `_threshold_to_logic`), so a
    string here is unambiguously a serialization defect, never an
    intentional string comparison, and safe to mechanically fix."""
    if isinstance(node, dict):
        return {k: _coerce_numeric_json_logic_types(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_coerce_numeric_json_logic_types(item) for item in node]
    if isinstance(node, str) and _NUMERIC_STRING_RE.match(node):
        return float(node) if "." in node else int(node)
    return node


def apply_deterministic_fixes(failure: PolicyFailure) -> RepairSuggestion:
    """The mechanically-safe subset of Requirement 3's named defect
    classes. Returns `can_repair=False` (not an exception) when nothing
    here applies -- that is the signal for the orchestrator to escalate
    to the LLM agent, not a failure of this function."""
    notes: list[str] = []
    repaired_rego = failure.rego_code
    repaired_json_logic = failure.json_logic
    changed = False

    if failure.error_type == PolicyErrorType.SYNTAX_ERROR and repaired_rego and "default allow" not in repaired_rego and "package " in repaired_rego:
        repaired_rego = _inject_default_allow(repaired_rego)
        notes.append("Injected missing 'default allow := false' immediately after the package declaration.")
        changed = True

    if failure.error_type == PolicyErrorType.INVALID_JSON_LOGIC_TYPE and repaired_json_logic is not None:
        coerced = _coerce_numeric_json_logic_types(repaired_json_logic)
        if coerced != repaired_json_logic:
            repaired_json_logic = coerced
            notes.append("Coerced numeric-looking string operand(s) in the JSON-Logic AST to real JSON numbers.")
            changed = True

    if not changed:
        return RepairSuggestion(
            can_repair=False,
            repair_notes=(
                f"No deterministic fix applies to error_type={failure.error_type.value!r} "
                f"(message: {failure.error_message[:200]!r}). Escalating to the LLM Policy Repair Agent."
            ),
        )

    return RepairSuggestion(
        can_repair=True,
        repaired_rego=repaired_rego,
        repaired_json_logic=repaired_json_logic,
        repair_notes=" ".join(notes),
    )


# --------------------------------------------------------------------------
# LLM-based Policy Repair Agent (CrewAI)
# --------------------------------------------------------------------------


def build_repair_agent(settings: Settings):
    from crewai import Agent  # deferred heavy import

    from app.agents.crew import _build_llm  # reuse the same LLM factory (temperature=0.0 for a deterministic repair)

    return Agent(
        role="Compliance Policy Repair Specialist",
        goal=(
            "Fix a failing OPA Rego / JSON-Logic compliance policy's reported syntax, compile, or runtime "
            "defect while keeping every threshold and condition traceable to its original source clause text."
        ),
        backstory=POLICY_REPAIR_AGENT_SYSTEM_PROMPT,
        tools=[],
        llm=_build_llm(settings, temperature=0.0),
        allow_delegation=False,
        verbose=settings.agent_verbose,
        max_iter=6,
        respect_context_window=True,
    )


def build_repair_task(agent, failure: PolicyFailure, prior_attempts: list[RepairAttempt] | None = None):
    from crewai import Task  # deferred heavy import

    return Task(
        description=build_repair_task_description(failure, prior_attempts),
        expected_output="A single JSON object conforming exactly to the RepairSuggestion schema.",
        agent=agent,
        output_pydantic=RepairSuggestion,
    )


def run_llm_repair(failure: PolicyFailure, settings: Settings, prior_attempts: list[RepairAttempt] | None = None) -> RepairSuggestion:
    """Synchronous (CrewAI's `Crew.kickoff` is itself synchronous) --
    callers on an asyncio event loop must run this via
    `asyncio.to_thread`, matching how app.agents.pipeline calls
    `run_dual_validation`."""
    from crewai import Crew, Process  # deferred heavy import

    agent = build_repair_agent(settings)
    task = build_repair_task(agent, failure, prior_attempts)
    crew = Crew(agents=[agent], tasks=[task], process=Process.sequential, memory=False, cache=False, verbose=settings.agent_verbose, max_rpm=settings.agent_max_rpm)
    crew.kickoff()

    suggestion = task.output.pydantic
    if not isinstance(suggestion, RepairSuggestion):
        return RepairSuggestion(can_repair=False, repair_notes="Repair agent did not return schema-conformant output; refusing to trust an unparsed response.")
    return suggestion


# --------------------------------------------------------------------------
# Entry point the orchestrator calls
# --------------------------------------------------------------------------


def repair_policy(failure: PolicyFailure, settings: Settings | None = None, prior_attempts: list[RepairAttempt] | None = None) -> tuple[RepairSuggestion, RepairStrategy]:
    """Tries the deterministic fast path first; escalates to the LLM
    agent only if it declines AND an API key is configured. Returns the
    suggestion plus WHICH strategy actually produced it, so the caller's
    `RepairAttempt.strategy` is accurate rather than assumed."""
    settings = settings or get_settings()

    deterministic = apply_deterministic_fixes(failure)
    if deterministic.can_repair:
        return deterministic, RepairStrategy.DETERMINISTIC_FIX

    if not settings.hf_api_token:
        logger.info("No deterministic fix for rule_id=%s and no HUGGINGFACEHUB_API_TOKEN configured; cannot escalate to the LLM Policy Repair Agent.", failure.rule_id)
        return (
            RepairSuggestion(can_repair=False, repair_notes=deterministic.repair_notes + " No HUGGINGFACEHUB_API_TOKEN configured, so the LLM Policy Repair Agent could not be tried either."),
            RepairStrategy.UNFIXABLE,
        )

    llm_suggestion = run_llm_repair(failure, settings, prior_attempts)
    return llm_suggestion, (RepairStrategy.LLM_REPAIR_AGENT if llm_suggestion.can_repair else RepairStrategy.UNFIXABLE)
