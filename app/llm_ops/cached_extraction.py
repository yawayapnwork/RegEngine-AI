"""Cost-optimized extraction entrypoint: semantic cache -> dynamic model
router -> (cheap tier | frontier tier, with escalation) -> cache write.

This sits in FRONT of `app.agents.pipeline.extract_and_audit_clause`'s
extraction half, as a cheaper alternative to always invoking the full
CrewAI dual-agent crew for the extraction step:

    ClauseChunk.text
        -> SemanticPromptCache.get()          [Redis exact / Qdrant semantic]
        -> HIT?  return cached ExtractedComplianceRule, no model call at all
        -> MISS: ModelRouter.decide()          [pre-call complexity heuristic]
        -> CHEAP_LOCAL: call the QLoRA-fine-tuned model (llm_finetune/)
             via its vLLM OpenAI-compatible endpoint
                -> low confidence / bad schema / ambiguous_spans?
                     ModelRouter.should_escalate() -> retry on FRONTIER
        -> FRONTIER: call Claude 3.5 Sonnet directly via litellm (the same
             library CrewAI's `LLM` wrapper uses internally, so this reuses
             whatever ANTHROPIC_API_KEY / retry config is already set up
             for app.agents.crew, without pulling in a second SDK)
        -> cache the validated result for next time

The Logic Auditor Agent (app.agents.crew.build_audit_agent) is
UNCHANGED and NOT bypassed by any of this -- caching/routing only ever
short-circuits which model produces the *draft* extraction; every draft,
cached or freshly generated, cheap-tier or frontier, still goes through
the same adversarial, tool-verified audit before it can be approved. See
`extract_and_audit_with_cost_optimization` at the bottom of this module
for how the two compose.
"""
from __future__ import annotations

import hashlib
import json
import logging

import httpx
from sqlalchemy.ext.asyncio import AsyncEngine

from app.agents.schemas import AuditedComplianceRule, ExtractedComplianceRule
from app.config import Settings
from app.llm_ops.cost_tracker import CostTracker, track_llm_call
from app.llm_ops.models import CacheLayer, ModelTier
from app.llm_ops.router import ModelRouter
from app.llm_ops.semantic_cache import SemanticPromptCache
from app.models import ClauseChunk

logger = logging.getLogger(__name__)

EXTRACTION_SYSTEM_PROMPT = (
    "You are a SEBI securities-law compliance analyst. Given a verbatim clause "
    "from a SEBI circular, extract its regulatory obligations as a single JSON "
    "object matching the ExtractedComplianceRule schema: target_entities, "
    "trigger_conditions, deterministic_logic (numeric thresholds only), "
    "qualitative_directives, obligation_type (mandatory/prohibited/conditional/"
    "recommended), and ambiguous_spans. Every extracted field MUST include a "
    "verbatim_evidence quote copied exactly from the clause -- never infer or "
    "round a number that is not explicitly stated. Output ONLY the JSON object, "
    "no commentary."
)


def _user_prompt(chunk: ClauseChunk) -> str:
    context_lines = []
    if chunk.circular_number:
        context_lines.append(f"Circular: {chunk.circular_number}")
    if chunk.clause_number:
        context_lines.append(f"Clause: {chunk.clause_number}")
    context = ("\n".join(context_lines) + "\n\n") if context_lines else ""
    return f'{context}Clause text:\n"""\n{chunk.text}\n"""\n\nPopulate rule_id as "{chunk.sha256}:{chunk.clause_number or "unscoped"}".'


async def _call_cheap_tier(chunk: ClauseChunk, settings: Settings) -> tuple[dict | None, int, int, bool]:
    """Returns (parsed_json_or_None, input_tokens, output_tokens, schema_valid)."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            f"{settings.llm_router_cheap_model_base_url}/chat/completions",
            json={
                "model": settings.llm_router_cheap_model,
                "temperature": 0.1,
                "max_tokens": 2048,
                "messages": [
                    {"role": "system", "content": EXTRACTION_SYSTEM_PROMPT},
                    {"role": "user", "content": _user_prompt(chunk)},
                ],
            },
        )
        resp.raise_for_status()
        body = resp.json()

    usage = body.get("usage", {})
    input_tokens = usage.get("prompt_tokens", 0)
    output_tokens = usage.get("completion_tokens", 0)
    raw_content = body["choices"][0]["message"]["content"]

    try:
        parsed = json.loads(raw_content)
        ExtractedComplianceRule.model_validate(parsed)
        return parsed, input_tokens, output_tokens, True
    except Exception:
        logger.warning("Cheap-tier extraction failed schema validation; will escalate to frontier tier.")
        return None, input_tokens, output_tokens, False


async def _call_frontier_tier(chunk: ClauseChunk, settings: Settings) -> tuple[dict, int, int]:
    import litellm  # same library app.agents.crew's CrewAI `LLM` wrapper uses internally

    response = await litellm.acompletion(
        model=settings.llm_router_frontier_model,
        api_key=settings.anthropic_api_key,
        temperature=0.0,
        max_tokens=4096,
        messages=[
            {"role": "system", "content": EXTRACTION_SYSTEM_PROMPT},
            {"role": "user", "content": _user_prompt(chunk)},
        ],
    )
    raw_content = response.choices[0].message.content
    usage = response.usage
    parsed = json.loads(raw_content)
    ExtractedComplianceRule.model_validate(parsed)  # let a frontier-tier schema failure raise -- no further fallback tier exists
    return parsed, usage.prompt_tokens, usage.completion_tokens


async def extract_clause_with_routing(
    chunk: ClauseChunk,
    settings: Settings,
    engine: AsyncEngine,
    tenant_id: str | None,
) -> ExtractedComplianceRule:
    cache = SemanticPromptCache(settings)
    tracker = CostTracker(engine)
    router = ModelRouter(settings)

    try:
        cache_result = await cache.get(chunk.text, "extraction")
        if cache_result.hit:
            async with track_llm_call(tracker, tenant_id=tenant_id, task_type="extraction", clause_sha256=chunk.sha256) as call:
                call.cache_layer = cache_result.layer
                call.details = {"similarity": cache_result.similarity}
            return ExtractedComplianceRule.model_validate(cache_result.cached_response)

        decision = router.decide(chunk.text)
        parsed: dict | None = None
        escalated = False

        if decision.tier == ModelTier.CHEAP_LOCAL:
            async with track_llm_call(tracker, tenant_id=tenant_id, task_type="extraction", clause_sha256=chunk.sha256) as call:
                call.model_tier = ModelTier.CHEAP_LOCAL
                call.model_name = decision.model_name
                call.complexity = decision.complexity
                call.details = {"routing_reasons": decision.reasons}

                parsed, in_tok, out_tok, schema_valid = await _call_cheap_tier(chunk, settings)
                call.input_tokens, call.output_tokens = in_tok, out_tok

                confidence = parsed.get("extraction_confidence") if parsed else None
                ambiguous_spans = parsed.get("ambiguous_spans") if parsed else None
                should_escalate, escalate_reasons = router.should_escalate(
                    extraction_confidence=confidence, ambiguous_spans=ambiguous_spans, schema_valid=schema_valid
                )
                if should_escalate:
                    call.details["escalate_reasons"] = escalate_reasons
                    escalated = True
                    parsed = None  # fetched at frontier tier below, outside this cost-tracking block

        if parsed is None:
            async with track_llm_call(tracker, tenant_id=tenant_id, task_type="extraction", clause_sha256=chunk.sha256) as call:
                call.model_tier = ModelTier.FRONTIER
                call.model_name = settings.llm_router_frontier_model
                call.complexity = decision.complexity
                call.escalated_from_cheap = escalated
                call.details = {"routing_reasons": decision.reasons}

                parsed, in_tok, out_tok = await _call_frontier_tier(chunk, settings)
                call.input_tokens, call.output_tokens = in_tok, out_tok

        extracted = ExtractedComplianceRule.model_validate(parsed)
        await cache.put(chunk.text, "extraction", extracted.model_dump(mode="json"))
        return extracted
    finally:
        await cache.close()


async def extract_and_audit_with_cost_optimization(
    chunk: ClauseChunk,
    sibling_chunks: list[dict],
    settings: Settings,
    engine: AsyncEngine,
    tenant_id: str | None,
) -> AuditedComplianceRule:
    """Full pipeline: cost-optimized extraction (above) feeding into the
    UNCHANGED, always-frontier-tier Logic Auditor Agent -- the audit stage
    is the anti-hallucination gate (mechanical quote/number verification
    via tools, see app.agents.crew.build_audit_agent) and is deliberately
    never routed to the cheap tier or skipped on a cache hit."""
    from app.agents.crew import build_audit_agent, build_audit_task
    from app.agents.schemas import ComplianceRuleAudit

    extracted = await extract_clause_with_routing(chunk, settings, engine, tenant_id)

    from crewai import Crew, Process  # deferred heavy import, mirrors app.agents.crew

    audit_agent = build_audit_agent(settings)

    class _StubTask:
        """Minimal stand-in exposing `.output.pydantic` so build_audit_task's
        `context=[extraction_task]` wiring can reference this extraction
        without re-running it through a CrewAI Task (which would invoke an
        LLM again just to reproduce output we already have)."""

        class _Output:
            def __init__(self, pydantic_obj):
                self.pydantic = pydantic_obj

        def __init__(self, pydantic_obj):
            self.output = self._Output(pydantic_obj)

    stub_extraction_task = _StubTask(extracted)
    audit_task = build_audit_task(audit_agent, chunk, stub_extraction_task, sibling_chunks)

    crew = Crew(agents=[audit_agent], tasks=[audit_task], process=Process.sequential, memory=False, cache=False, verbose=settings.agent_verbose, max_rpm=settings.agent_max_rpm)
    crew.kickoff()

    audit = audit_task.output.pydantic
    if not isinstance(audit, ComplianceRuleAudit):
        raise ValueError("Audit agent did not return schema-conformant output; refusing to persist.")

    return AuditedComplianceRule(rule=extracted, audit=audit, revision_round=0)
