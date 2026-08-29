"""Maps one `app.execution.evaluator.Evaluator` result onto ledger entries.

One ledger row per matched policy, not one per transaction: the required
"SEBI Circular Source Mapping" (circular ID, clause hash, exact section) is
per-*rule*, so a transaction checked against three compiled policies must
produce three independently auditable rows, each traceable to the exact
clause it was evaluated against.
"""
from __future__ import annotations

import logging

from app.execution.models import EvaluationResult, PolicyOutcome, TransactionPayload
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
                details={"violations": outcome.violations, "package": outcome.package, "transaction_decision": result.decision.value},
            )
        )
    return events


async def log_evaluation(ledger: LedgerService, transaction: TransactionPayload, result: EvaluationResult) -> None:
    """Best-effort: a ledger outage must not block a live compliance
    decision from being returned to the broker (the decision itself is
    already final by the time this runs). Failures are logged loudly so
    an ops alert can catch a ledger that is silently falling behind —
    a production deployment with a hard "no evaluation without an audit
    row" requirement should instead write through a durable outbox that
    retries independently of the request/response cycle."""
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
