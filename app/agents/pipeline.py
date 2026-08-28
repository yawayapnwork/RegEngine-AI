"""Async entrypoint bridging the sync CrewAI crew into the FastAPI event loop."""
from __future__ import annotations

import asyncio
import time

from app.agents.schemas import AuditedComplianceRule
from app.config import Settings, get_settings
from app.models import ClauseChunk
from app.observability.metrics import LLM_AGENT_TASK_DURATION, record_hallucination_findings
from app.observability.tracing import traced_span


async def extract_and_audit_clause(
    chunk: ClauseChunk,
    sibling_chunks: list[dict] | None = None,
    settings: Settings | None = None,
) -> AuditedComplianceRule:
    """Run the dual-agent extraction/audit crew for one clause chunk without
    blocking the event loop. `crewai.Crew.kickoff` is synchronous (it makes
    blocking LLM calls internally), so it is offloaded to a worker thread.

    This is also the single choke point every extraction/audit pass flows
    through regardless of caller (a lone clause via this function directly,
    or a whole circular via extract_and_audit_circular below), so it's
    where LLM agent latency and hallucination-detection metrics are
    recorded -- see app.observability.metrics's module docstring."""
    from app.agents.crew import run_dual_validation  # deferred heavy import

    settings = settings or get_settings()
    started = time.perf_counter()

    with traced_span("agents.extract_and_audit_clause", chunk_id=chunk.chunk_id, clause_number=chunk.clause_number):
        audited = await asyncio.to_thread(run_dual_validation, chunk, sibling_chunks, settings)

    LLM_AGENT_TASK_DURATION.labels(audit_verdict=audited.audit.verdict.value).observe(time.perf_counter() - started)
    record_hallucination_findings(audited.audit.findings)
    return audited


async def extract_and_audit_circular(
    chunks: list[ClauseChunk],
    settings: Settings | None = None,
    max_concurrency: int = 3,
) -> list[AuditedComplianceRule]:
    """Run the dual-agent pipeline across every clause chunk of a circular,
    bounding concurrency to respect the Anthropic rate limit / agent_max_rpm."""
    settings = settings or get_settings()
    sibling_payload = [
        {"chunk_id": c.chunk_id, "clause_number": c.clause_number, "section_path": c.section_path, "text": c.text}
        for c in chunks
    ]
    gate = asyncio.Semaphore(max_concurrency)

    async def _run(chunk: ClauseChunk) -> AuditedComplianceRule:
        async with gate:
            return await extract_and_audit_clause(chunk, sibling_payload, settings)

    return await asyncio.gather(*(_run(c) for c in chunks))
