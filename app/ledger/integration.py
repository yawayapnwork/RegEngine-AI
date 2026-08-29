"""Maps one `app.execution.evaluator.Evaluator` result onto ledger entries.

One ledger row per matched policy, not one per transaction: the required
"SEBI Circular Source Mapping" (circular ID, clause hash, exact section) is
per-*rule*, so a transaction checked against three compiled policies must
produce three independently auditable rows, each traceable to the exact
clause it was evaluated against.
"""
from __future__ import annotations

import logging

from app.config import get_settings
from app.execution.dependencies import get_redis_pool
from app.execution.models import EvaluationResult, PolicyOutcome, TransactionPayload
from app.explainability.explainer import explain_policy_outcome_deterministic
from app.incident.publisher import raise_breach_event
from app.incident.trigger_matrix import ambiguous_hitl_event, clause_violation_event
from app.ledger.models import ComplianceEvaluationEvent, EvaluationOutcome
from app.ledger.service import LedgerService
from app.observability.metrics import AUDIT_LEDGER_WRITE_FAILURES_TOTAL

logger = logging.getLogger(__name__)


def _clause_hash(rule_id: str) -> str:
    """ExtractedComplianceRule.rule_id is minted as f'{source_sha256}:{clause_number}'
    (app/agents/schemas.py) — the hash is always the prefix before the
    first colon."""
    return rule_id.split(":", 1)[0]


def _outcome_result(outcome: PolicyOutcome) -> EvaluationOutcome:
    if outcome.allow is None:
        return EvaluationOutcome.HITL_REVIEW
    return EvaluationOutcome.PASS if outcome.allow else EvaluationOutcome.FAIL


def _explanation_texts(outcome: PolicyOutcome) -> list[str]:
    """Deterministic-only (see app.explainability.explainer's module
    docstring) -- this runs inline on the synchronous evaluate path via
    log_evaluation below, so it must never await an LLM call. The
    resulting natural-language headline(s) are written into `details`,
    which `app.ledger.hash_chain.canonical_payload` includes in
    `payload_digest` -- the explanation is therefore not merely stored
    "alongside" the block hash but literally bound INTO it: altering the
    stored explanation after the fact breaks the same recomputable hash
    chain that protects every other field on this row."""
    if not outcome.violations:
        return []
    return [exp.headline for exp in explain_policy_outcome_deterministic(outcome, regulator="sebi")]


def build_ledger_events(transaction: TransactionPayload, result: EvaluationResult) -> list[ComplianceEvaluationEvent]:
    events = []
    for outcome in result.matched_policies:
        evaluation_result = _outcome_result(outcome)
        events.append(
            ComplianceEvaluationEvent(
                broker_id=transaction.broker_id or "unknown",
                transaction_id=transaction.transaction_id,
                evaluated_at=result.evaluated_at,
                circular_id=outcome.circular_number or "unknown",
                clause_hash=_clause_hash(outcome.rule_id),
                section_reference=outcome.clause_number or "unscoped",
                rule_id=outcome.rule_id,
                evaluation_result=evaluation_result,
                hitl_review_id=result.hitl_case_id if evaluation_result == EvaluationOutcome.HITL_REVIEW else None,
                details={
                    "violations": outcome.violations,
                    "package": outcome.package,
                    "transaction_decision": result.decision.value,
                    "explanation": _explanation_texts(outcome),
                },
            )
        )
    return events


async def _raise_breach_events(transaction: TransactionPayload, result: EvaluationResult) -> None:
    """Requirement 1's trigger matrix, fired from the one place both a
    DENY and a FLAGGED outcome are already fully known. Raised
    independently of whether the ledger write below succeeds or fails --
    a breach notification is about the COMPLIANCE OUTCOME, not about
    audit-trail durability, so a ledger outage must never also silence
    the compliance officer alert for a live violation."""
    settings = get_settings()
    redis_client = get_redis_pool()
    for outcome in result.matched_policies:
        try:
            if _outcome_result(outcome) == EvaluationOutcome.FAIL:
                await raise_breach_event(clause_violation_event(transaction, result, outcome), redis_client, settings)
            elif _outcome_result(outcome) == EvaluationOutcome.HITL_REVIEW:
                await raise_breach_event(
                    ambiguous_hitl_event(
                        transaction,
                        reason=f"OPA returned an undefined result for rule_id={outcome.rule_id}; routed to HITL case {result.hitl_case_id}.",
                        hitl_case_id=result.hitl_case_id,
                        rule_id=outcome.rule_id,
                        circular_number=outcome.circular_number,
                        clause_number=outcome.clause_number,
                    ),
                    redis_client,
                    settings,
                )
        except Exception:  # noqa: BLE001 - a breach-notification failure must never mask the underlying compliance decision or block the ledger write
            logger.exception("Failed to raise breach event for transaction_id=%s rule_id=%s", transaction.transaction_id, outcome.rule_id)


async def log_evaluation(ledger: LedgerService, transaction: TransactionPayload, result: EvaluationResult) -> None:
    """Best-effort: a ledger outage must not block a live compliance
    decision from being returned to the broker (the decision itself is
    already final by the time this runs). Failures are logged loudly so
    an ops alert can catch a ledger that is silently falling behind —
    a production deployment with a hard "no evaluation without an audit
    row" requirement should instead write through a durable outbox that
    retries independently of the request/response cycle."""
    await _raise_breach_events(transaction, result)

    for event in build_ledger_events(transaction, result):
        try:
            await ledger.append_entry(event)
        except Exception:  # noqa: BLE001
            AUDIT_LEDGER_WRITE_FAILURES_TOTAL.inc()
            logger.exception(
                "Failed to append audit ledger entry for transaction_id=%s rule_id=%s",
                transaction.transaction_id,
                event.rule_id,
            )
