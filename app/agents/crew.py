"""CrewAI configuration for the dual-agent extraction/audit pipeline.

Two agents, sequential process, bounded revision loop:

    ClauseChunk --> [Extraction Agent] --> ExtractedComplianceRule
                          ^                        |
                          |                        v
                 (revise, max N times)   [Logic Auditor Agent] --> ComplianceRuleAudit
                          |                        |
                          +---- needs_revision -----+
                                       |
                              approved / rejected
                                       v
                            AuditedComplianceRule (persisted)

The Auditor never edits the extraction directly — on `needs_revision` its
findings are fed back into a fresh Extraction task so the same agent that
produced the claim is the one accountable for correcting it. This keeps the
audit trail clean (findings <-> revision <-> re-audit) and avoids the
auditor silently overwriting facts with its own unverified guesses.

crewai and its Anthropic LLM binding are imported lazily inside the builder
functions so `app.agents.schemas` / `app.agents.tools` (and their tests)
remain usable without crewai installed.
"""
from __future__ import annotations

import json
import logging

from app.agents.schemas import (
    AuditedComplianceRule,
    ComplianceRuleAudit,
    ExtractedComplianceRule,
)
from app.config import Settings, get_settings
from app.models import ClauseChunk
from app.regulatory.taxonomy import REGULATOR_PROFILES, Regulator, resolve_domain

logger = logging.getLogger(__name__)

MAX_REVISION_ROUNDS = 2

# --------------------------------------------------------------------------
# LLM factory
# --------------------------------------------------------------------------


def _build_llm(settings: Settings, *, temperature: float, model_override: str | None = None):
    """Claude 3.5 Sonnet via CrewAI's LiteLLM-backed LLM wrapper, by
    default. `model_override` is how app.agents.graph's fallback node
    escalates to a secondary model (settings.agent_fallback_model) when
    the primary extraction's confidence falls below
    settings.agent_confidence_threshold -- a distinct model family/
    checkpoint, not just a retried call to the same one, on the theory
    that a low-confidence result from one model is more likely to be
    resolved by a genuinely different model's reasoning than by asking
    the same model again with nothing new to go on."""
    from crewai import LLM  # deferred heavy import

    return LLM(
        model=model_override or "anthropic/claude-3-5-sonnet-20241022",
        api_key=settings.anthropic_api_key,
        temperature=temperature,
        max_tokens=4096,
    )


# --------------------------------------------------------------------------
# Agents
# --------------------------------------------------------------------------


def build_extraction_agent(settings: Settings, regulator: Regulator = Regulator.SEBI, model_override: str | None = None):
    from crewai import Agent  # deferred heavy import

    from app.agents.prompts import EXTRACTION_AGENT_SYSTEM_PROMPT
    from app.agents.tools import build_crewai_tools

    profile = REGULATOR_PROFILES[regulator]
    tools = build_crewai_tools()
    return Agent(
        role=f"{profile.display_name} Compliance Clause Extraction Specialist",
        goal=(
            f"Convert a single {profile.display_name} legal clause into a structured, "
            "schema-conformant ExtractedComplianceRule JSON object, with every claim "
            "traceable to an exact verbatim quote from the source text."
        ),
        # Regulator context is appended, never substituted -- the extraction
        # discipline (verbatim_evidence for every field, no inferred
        # numbers) in EXTRACTION_AGENT_SYSTEM_PROMPT applies identically
        # across every regulator; only the entity/obligation vocabulary
        # hint changes.
        backstory=f"{EXTRACTION_AGENT_SYSTEM_PROMPT}\n\n{profile.agent_persona_hint}",
        tools=[t for t in tools if t.name in ("verify_quotes", "scan_numeric_tokens", "lookup_entity")],
        llm=_build_llm(settings, temperature=0.0, model_override=model_override),
        allow_delegation=False,
        verbose=settings.agent_verbose,
        max_iter=8,
        respect_context_window=True,
    )


def build_quantitative_parsing_agent(settings: Settings, regulator: Regulator = Regulator.SEBI, model_override: str | None = None):
    """Specialized extraction agent for clauses containing mathematical
    formulas (VaR/CRAR-style computations, weighted averages, standard
    deviation/variance formulas) -- app.agents.graph.complexity_router
    routes to this agent instead of the general Extraction Agent when
    app.agents.graph.complexity_router.detect_complexity flags
    `has_math_formulas`. Produces the SAME ExtractedComplianceRule schema
    (build_extraction_task is reused unchanged) -- only the agent's own
    reasoning approach to formula variables differs."""
    from crewai import Agent  # deferred heavy import

    from app.agents.prompts import QUANTITATIVE_PARSING_AGENT_SYSTEM_PROMPT
    from app.agents.tools import build_crewai_tools

    profile = REGULATOR_PROFILES[regulator]
    tools = build_crewai_tools()
    return Agent(
        role=f"{profile.display_name} Quantitative Formula Parsing Specialist",
        goal=(
            "Decompose a mathematical/computational compliance formula into its constituent "
            "variables, each captured as its own NumericalThreshold where the formula fixes a "
            "numeric constant or bound, with the overall computation preserved in extraction_notes "
            "so a human reviewer can see how the pieces combine."
        ),
        backstory=f"{QUANTITATIVE_PARSING_AGENT_SYSTEM_PROMPT}\n\n{profile.agent_persona_hint}",
        tools=[t for t in tools if t.name in ("verify_quotes", "scan_numeric_tokens", "lookup_entity")],
        llm=_build_llm(settings, temperature=0.0, model_override=model_override),
        allow_delegation=False,
        verbose=settings.agent_verbose,
        max_iter=10,  # formula decomposition typically needs more tool-call rounds than a plain threshold
        respect_context_window=True,
    )


def build_reference_resolution_agent(settings: Settings, regulator: Regulator = Regulator.SEBI, model_override: str | None = None):
    """Specialized extraction agent for clauses with nested cross-references
    to other clauses/circulars/annexures -- routed to when
    complexity_router flags `has_cross_references`. Given the full
    sibling-chunk set up front (via build_clause_context, same tool the
    Logic Auditor already uses) so it can actually resolve a reference
    like "as specified in clause 3.2.1" against real sibling text instead
    of extracting the clause in isolation and leaving the reference
    unresolved."""
    from crewai import Agent  # deferred heavy import

    from app.agents.prompts import REFERENCE_RESOLUTION_AGENT_SYSTEM_PROMPT
    from app.agents.tools import build_crewai_tools

    profile = REGULATOR_PROFILES[regulator]
    tools = build_crewai_tools()
    return Agent(
        role=f"{profile.display_name} Cross-Reference Resolution Specialist",
        goal=(
            "Resolve every cross-reference to another clause, annexure, or circular in the source "
            "text against the provided sibling chunks BEFORE extracting the compliance rule, so the "
            "resulting ExtractedComplianceRule reflects the referenced content's actual effect, not "
            "just the referring clause's literal (and incomplete) wording."
        ),
        backstory=f"{REFERENCE_RESOLUTION_AGENT_SYSTEM_PROMPT}\n\n{profile.agent_persona_hint}",
        tools=[t for t in tools if t.name in ("verify_quotes", "scan_numeric_tokens", "lookup_entity", "build_clause_context")],
        llm=_build_llm(settings, temperature=0.0, model_override=model_override),
        allow_delegation=False,
        verbose=settings.agent_verbose,
        max_iter=10,
        respect_context_window=True,
    )


def build_audit_agent(settings: Settings, regulator: Regulator = Regulator.SEBI):
    from crewai import Agent  # deferred heavy import

    from app.agents.prompts import LOGIC_AUDITOR_SYSTEM_PROMPT
    from app.agents.tools import build_crewai_tools

    profile = REGULATOR_PROFILES[regulator]
    tools = build_crewai_tools()
    return Agent(
        role=f"{profile.display_name} Compliance Logic Auditor",
        goal=(
            "Adversarially cross-examine an ExtractedComplianceRule against its source "
            "clause text, mechanically verifying every quote, number, and entity, and "
            "returning a ComplianceRuleAudit with a definitive verdict."
        ),
        backstory=f"{LOGIC_AUDITOR_SYSTEM_PROMPT}\n\n{profile.agent_persona_hint}",
        tools=tools,
        llm=_build_llm(settings, temperature=0.0),
        allow_delegation=False,
        verbose=settings.agent_verbose,
        max_iter=8,
        respect_context_window=True,
    )


# --------------------------------------------------------------------------
# Tasks
# --------------------------------------------------------------------------


def build_extraction_task(agent, chunk: ClauseChunk, prior_findings: list[dict] | None = None):
    from crewai import Task  # deferred heavy import

    revision_note = ""
    if prior_findings:
        revision_note = (
            "\n\nThis is a REVISION. The Logic Auditor Agent rejected or flagged your "
            "previous extraction with these findings — you must resolve every one of "
            "them, either by correcting the field or moving the disputed content to "
            "`ambiguous_spans` if it truly cannot be grounded in the source text:\n"
            + json.dumps(prior_findings, indent=2)
        )

    description = f"""\
Extract a structured compliance rule from the following SEBI clause.

circular_number: {chunk.circular_number!r}
clause_number: {chunk.clause_number!r}
section_path: {chunk.section_path!r}
source_chunk_id: {chunk.chunk_id!r}
source_sha256: {chunk.sha256!r}

SOURCE CLAUSE TEXT:
\"\"\"
{chunk.text}
\"\"\"{revision_note}

Populate `rule_id` as "{chunk.sha256}:{chunk.clause_number or 'unscoped'}".
Follow every rule in your system prompt, in particular: no verbatim_evidence,
no field. Call verify_quotes on your own output before finalizing.
"""
    return Task(
        description=description,
        expected_output="A single JSON object conforming exactly to the ExtractedComplianceRule schema.",
        agent=agent,
        output_pydantic=ExtractedComplianceRule,
    )


def build_audit_task(agent, chunk: ClauseChunk, extraction_task, sibling_chunks: list[dict]):
    from crewai import Task  # deferred heavy import

    description = f"""\
Audit the extraction produced by the previous task against the raw source
clause text below. Use verify_quotes, scan_numeric_tokens, lookup_entity,
and build_clause_context as needed — do not assert a finding you have not
mechanically checked with a tool where a tool applies.

SOURCE CLAUSE TEXT:
\"\"\"
{chunk.text}
\"\"\"

circular_number: {chunk.circular_number!r}
clause_number: {chunk.clause_number!r}
section_path: {chunk.section_path!r}

SIBLING/PARENT CHUNKS (for build_clause_context, pass as `all_chunks`):
{json.dumps(sibling_chunks, indent=2)[:6000]}

Set `rule_id` to match the extraction's `rule_id` exactly. Populate
`verified_quote_count` / `unverified_quote_count` from your verify_quotes
tool call results across ALL verbatim_evidence fields in the extraction.
"""
    return Task(
        description=description,
        expected_output="A single JSON object conforming exactly to the ComplianceRuleAudit schema.",
        agent=agent,
        context=[extraction_task],
        output_pydantic=ComplianceRuleAudit,
    )


# --------------------------------------------------------------------------
# Crew assembly + revision-loop orchestration
# --------------------------------------------------------------------------


def _run_crew_once(
    chunk: ClauseChunk,
    settings: Settings,
    sibling_chunks: list[dict],
    prior_findings: list[dict] | None,
) -> tuple[ExtractedComplianceRule, ComplianceRuleAudit]:
    from crewai import Crew, Process  # deferred heavy import

    extraction_agent = build_extraction_agent(settings, chunk.regulator)
    audit_agent = build_audit_agent(settings, chunk.regulator)

    extraction_task = build_extraction_task(extraction_agent, chunk, prior_findings)
    audit_task = build_audit_task(audit_agent, chunk, extraction_task, sibling_chunks)

    crew = Crew(
        agents=[extraction_agent, audit_agent],
        tasks=[extraction_task, audit_task],
        process=Process.sequential,
        memory=False,        # each clause is independent; no cross-clause bleed
        cache=False,          # never reuse a cached tool/LLM result across revisions
        verbose=settings.agent_verbose,
        max_rpm=settings.agent_max_rpm,
    )
    crew.kickoff()

    extracted = extraction_task.output.pydantic
    audit = audit_task.output.pydantic
    if not isinstance(extracted, ExtractedComplianceRule) or not isinstance(audit, ComplianceRuleAudit):
        raise ValueError("Crew did not return schema-conformant output; refusing to persist.")

    # Regulator/domain are deterministic ingestion-time facts (chunk.regulator
    # came from app.regulatory.taxonomy.detect_regulator_and_document, not
    # from the LLM) -- stamped onto the extraction here rather than trusted
    # from whatever the model's JSON happened to contain. See
    # ExtractedComplianceRule.regulator's docstring for why this must never
    # be the LLM's own guess.
    extracted.regulator = chunk.regulator
    primary_entity = extracted.target_entities[0].normalized_entity if extracted.target_entities else None
    extracted.regulatory_domain = resolve_domain(chunk.regulator, primary_entity)

    return extracted, audit


def run_dual_validation(
    chunk: ClauseChunk,
    sibling_chunks: list[dict] | None = None,
    settings: Settings | None = None,
) -> AuditedComplianceRule:
    """Synchronous entrypoint: runs the extraction -> audit crew, resubmitting
    to the Extraction Agent up to MAX_REVISION_ROUNDS times on
    `needs_revision`, and stopping immediately on `rejected` (a rejected
    rule is escalated to human review rather than auto-retried, since a
    hallucinated threshold indicates the source clause may need a different
    extraction strategy, not just another attempt)."""
    settings = settings or get_settings()
    sibling_chunks = sibling_chunks or []

    prior_findings: list[dict] | None = None
    last_extracted: ExtractedComplianceRule | None = None
    last_audit: ComplianceRuleAudit | None = None

    for round_idx in range(MAX_REVISION_ROUNDS + 1):
        extracted, audit = _run_crew_once(chunk, settings, sibling_chunks, prior_findings)
        last_extracted, last_audit = extracted, audit

        if audit.verdict.value == "approved":
            break
        if audit.verdict.value == "rejected":
            logger.warning("Rule %s REJECTED by auditor on round %d: %d blocker(s)", extracted.rule_id, round_idx, len(audit.findings))
            break
        # needs_revision: loop again with findings fed back to the extractor
        logger.info("Rule %s needs revision (round %d): %d finding(s)", extracted.rule_id, round_idx, len(audit.findings))
        prior_findings = [f.model_dump() for f in audit.findings]
    else:
        round_idx = MAX_REVISION_ROUNDS  # exhausted retries without approval

    assert last_extracted is not None and last_audit is not None
    return AuditedComplianceRule(rule=last_extracted, audit=last_audit, revision_round=round_idx)
