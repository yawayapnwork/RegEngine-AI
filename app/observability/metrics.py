"""Prometheus metrics for the RegEngine AI pipeline.

Four metrics are the literal requirement; each is recorded at the one
call site that is the natural, single choke point ALL traffic of that
kind flows through -- not scattered across every caller -- so instrumenting
once can never drift out of sync with a second, forgotten call site:

    sebi_circular_ingestion_latency_seconds  -- app.services.pipeline.parse_pdf_bytes
    llm_hallucination_detection_total        -- app.agents.pipeline.extract_and_audit_clause
    opa_policy_evaluation_duration_seconds   -- app.execution.opa_engine.OPAEngine.evaluate
    hitl_review_queue_depth                  -- periodic poll, see below

Two supplementary metrics are added to make the Grafana dashboard's "LLM
agent latency" and "transaction verification outcomes" panels possible at
all -- requirement 2's four metrics don't include a duration for the
agent step or a breakdown of allow/deny/flagged outcomes, and neither
panel can be built without one:

    llm_agent_task_duration_seconds  -- same call site as the hallucination counter
    transaction_evaluation_total     -- app.execution.evaluator.Evaluator.evaluate_transaction

hitl_review_queue_depth is a Gauge, and deliberately PULL-refreshed by
`poll_queue_depths` (a periodic background task, see app.main's lifespan)
rather than incremented/decremented at every enqueue/dequeue call site.
Queue depth is a property of current STATE ("how many items are in the
queue right now"), not a count of EVENTS -- maintaining it incrementally
would require finding and correctly updating it at every single place
across app.execution.hitl_queue and app.api.hitl_review_routes that adds
or removes a pending item, and a single missed decrement (e.g. an item
removed by a direct DB/Redis operation, a crashed process mid-update)
would silently and permanently desync the gauge from reality with no
self-correcting mechanism. A periodic poll of the actual source of truth
(Redis pending set size; a `COUNT(*) WHERE status = 'PENDING'` query) can
never drift for longer than one poll interval.
"""
from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from contextlib import contextmanager

import redis.asyncio as redis
from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, Counter, Gauge, Histogram, generate_latest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.schemas import AuditFinding, FindingType

logger = logging.getLogger(__name__)

REGISTRY = CollectorRegistry()

# --- Requirement 2's four named metrics -----------------------------------

SEBI_CIRCULAR_INGESTION_LATENCY = Histogram(
    "sebi_circular_ingestion_latency_seconds",
    "Wall-clock time to parse + chunk one SEBI circular PDF (app.services.pipeline.parse_pdf_bytes).",
    labelnames=("outcome",),  # "success" | "error"
    buckets=(0.5, 1, 2, 5, 10, 20, 30, 60, 120, 180),  # PDF parsing: sub-second to a few minutes for large/OCR docs
    registry=REGISTRY,
)

LLM_HALLUCINATION_DETECTION_TOTAL = Counter(
    "llm_hallucination_detection_total",
    "Count of Logic Auditor Agent findings indicating the Extraction Agent hallucinated a threshold or entity "
    "not actually present in the source clause.",
    labelnames=("finding_type", "severity"),
    registry=REGISTRY,
)

OPA_POLICY_EVALUATION_DURATION = Histogram(
    "opa_policy_evaluation_duration_seconds",
    "Wall-clock time for one OPA decision query (app.execution.opa_engine.OPAEngine.evaluate).",
    labelnames=("outcome",),  # "allow" | "deny" | "undefined" | "error"
    buckets=(0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1),  # sub-ms to 1s; HFT-relevant range
    registry=REGISTRY,
)

HITL_REVIEW_QUEUE_DEPTH = Gauge(
    "hitl_review_queue_depth",
    "Current number of pending HITL items awaiting human action.",
    labelnames=("queue_type",),  # "transaction" (app.execution.hitl_queue, Redis) | "policy" (app.db HITLReview, Postgres)
    registry=REGISTRY,
)

# --- Supplementary metrics (needed for the Grafana dashboard; see module docstring) ---

LLM_AGENT_TASK_DURATION = Histogram(
    "llm_agent_task_duration_seconds",
    "Wall-clock time for the full CrewAI extraction+audit pass on one clause "
    "(app.agents.pipeline.extract_and_audit_clause).",
    labelnames=("audit_verdict",),  # "approved" | "needs_revision" | "rejected"
    buckets=(0.5, 1, 2, 5, 10, 20, 30, 60, 120),  # LLM calls: seconds, not milliseconds
    registry=REGISTRY,
)

TRANSACTION_EVALUATION_TOTAL = Counter(
    "transaction_evaluation_total",
    "Count of transaction evaluations by final decision (app.execution.evaluator.Evaluator.evaluate_transaction).",
    labelnames=("decision",),  # "allow" | "deny" | "flagged"
    registry=REGISTRY,
)

# --- LLM cost-optimization metrics (app.llm_ops) ---
# tenant_id is deliberately NOT a label here (unbounded-ish cardinality
# across every SEBI-registered intermediary) -- per-tenant cost breakdown
# lives in the `llm_usage_events` Postgres table (app.llm_ops.models),
# which is exactly what it's indexed for. These Prometheus metrics answer
# the platform-wide "is the cache/router working" question for alerting.

LLM_CACHE_LOOKUP_TOTAL = Counter(
    "llm_cache_lookup_total",
    "Semantic prompt cache lookups by layer and outcome (app.llm_ops.semantic_cache.SemanticPromptCache.get).",
    labelnames=("layer", "hit"),  # layer: "exact" | "semantic" | "none"; hit: "true" | "false"
    registry=REGISTRY,
)

LLM_ROUTING_DECISION_TOTAL = Counter(
    "llm_routing_decision_total",
    "Model-tier routing decisions (app.llm_ops.router.ModelRouter).",
    labelnames=("tier", "complexity", "escalated"),  # escalated: "true" | "false"
    registry=REGISTRY,
)

LLM_COST_USD_TOTAL = Counter(
    "llm_cost_usd_total",
    "Cumulative estimated USD spend on LLM inference, by model tier (app.llm_ops.cost_tracker).",
    labelnames=("model_tier",),
    registry=REGISTRY,
)

LLM_TOKENS_TOTAL = Counter(
    "llm_tokens_total",
    "Cumulative LLM tokens consumed, by model tier and direction (app.llm_ops.cost_tracker).",
    labelnames=("model_tier", "direction"),  # direction: "input" | "output"
    registry=REGISTRY,
)

# --- Load-test / breakpoint-analysis support metrics ---
# (loadtest/breakpoint_analysis.py's pass/fail gates read these two
# directly.) Neither existed before load testing needed a concrete signal
# for "is the audit ledger silently dropping writes" and "is the L1 policy
# cache actually absorbing load" -- both were previously observable only
# via application logs / in-process counters with no external visibility.

AUDIT_LEDGER_WRITE_FAILURES_TOTAL = Counter(
    "audit_ledger_write_failures_total",
    "Count of ComplianceEvaluationEvent appends that raised instead of committing "
    "(app.ledger.integration.log_evaluation). Every evaluation response is already "
    "final by the time this fires -- see that function's docstring -- so this "
    "counter is the ONLY signal that a compliance decision's audit trail entry "
    "was lost. Must be zero under any load-test breakpoint.",
    registry=REGISTRY,
)

POLICY_CACHE_LOOKUP_TOTAL = Counter(
    "policy_cache_lookup_total",
    "L1 in-process policy cache lookups by outcome (app.execution.policy_cache.PolicyCache.policies_for).",
    labelnames=("outcome",),  # "hit" | "miss"
    registry=REGISTRY,
)

_HALLUCINATION_FINDING_TYPES = frozenset({FindingType.HALLUCINATED_THRESHOLD, FindingType.HALLUCINATED_ENTITY})


def record_hallucination_findings(findings: list[AuditFinding]) -> int:
    """Increments llm_hallucination_detection_total for every finding the
    Logic Auditor Agent classified as a hallucination (a threshold or
    entity the Extraction Agent invented, not present in source text) --
    as opposed to the other FindingTypes (missing context, misclassified
    obligation, ...), which are real extraction quality issues but not
    hallucinations specifically, and are intentionally NOT counted here.
    Returns the number of findings counted, purely for logging/testing."""
    count = 0
    for finding in findings:
        if finding.finding_type in _HALLUCINATION_FINDING_TYPES:
            LLM_HALLUCINATION_DETECTION_TOTAL.labels(
                finding_type=finding.finding_type.value, severity=finding.severity.value
            ).inc()
            count += 1
    return count


@contextmanager
def observe_ingestion_latency() -> Iterator[None]:
    started = time.perf_counter()
    outcome = "success"
    try:
        yield
    except Exception:
        outcome = "error"
        raise
    finally:
        SEBI_CIRCULAR_INGESTION_LATENCY.labels(outcome=outcome).observe(time.perf_counter() - started)


@contextmanager
def observe_opa_evaluation(outcome_holder: dict[str, str]) -> Iterator[None]:
    """`outcome_holder` is a plain dict the caller writes {"outcome": ...}
    into before the block exits -- Python has no clean way for a `with`
    block to hand a value back to its context manager after the fact, and
    this avoids restructuring OPAEngine.evaluate's control flow (early
    returns on undefined/error) just to fit a single return-value pattern."""
    started = time.perf_counter()
    try:
        yield
    finally:
        OPA_POLICY_EVALUATION_DURATION.labels(outcome=outcome_holder.get("outcome", "error")).observe(time.perf_counter() - started)


async def poll_queue_depths(
    redis_client: redis.Redis,
    hitl_pending_set_key: str,
    db_session_factory,
    *,
    interval_seconds: float,
    stop_event,
) -> None:
    """Background loop (started from app.main's lifespan): refreshes
    hitl_review_queue_depth every `interval_seconds` until `stop_event` is
    set. Runs forever regardless of individual poll failures -- a
    transient Redis/Postgres hiccup should degrade to "this gauge is one
    interval stale," never "this gauge stops updating for the rest of the
    process's life."""
    from app.db.models import HITLReview

    while not stop_event.is_set():
        try:
            transaction_depth = await redis_client.scard(hitl_pending_set_key)
            HITL_REVIEW_QUEUE_DEPTH.labels(queue_type="transaction").set(transaction_depth)
        except Exception:  # noqa: BLE001 - one failed poll must not kill the loop
            logger.exception("Failed to poll transaction-level HITL queue depth from Redis.")

        try:
            async with db_session_factory() as session:  # type: AsyncSession
                result = await session.execute(select(func.count()).select_from(HITLReview).where(HITLReview.status == "PENDING"))
                policy_depth = result.scalar_one()
            HITL_REVIEW_QUEUE_DEPTH.labels(queue_type="policy").set(policy_depth)
        except Exception:  # noqa: BLE001 - one failed poll must not kill the loop
            logger.exception("Failed to poll policy-level HITL queue depth from Postgres.")

        try:
            await _sleep_or_stop(stop_event, interval_seconds)
        except Exception:  # noqa: BLE001 - defensive; asyncio.wait_for/Event.wait shouldn't raise anything else
            logger.exception("Unexpected error in HITL queue-depth poll loop's sleep.")


async def _sleep_or_stop(stop_event, timeout: float) -> None:
    import asyncio

    try:
        await asyncio.wait_for(stop_event.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        pass


def render_latest() -> tuple[bytes, str]:
    """Returns (body, content_type) for the /metrics endpoint."""
    return generate_latest(REGISTRY), CONTENT_TYPE_LATEST
