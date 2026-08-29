"""Maps real platform occurrences onto Requirement 1's trigger matrix.

Three builder functions, one per severity, each called from the exact
call site in the existing pipeline where that occurrence is already
known -- mirroring the "one instrumentation point per signal" principle
app.observability.metrics's module docstring establishes for Prometheus
metrics. None of these classify from scratch; they just package an
already-made decision (a DENY verdict, a FLAGGED verdict / blocking HITL
flag, a successful compile) into a `BreachEvent`.
"""
from __future__ import annotations

from app.execution.models import EvaluationResult, PolicyOutcome, TransactionPayload
from app.incident.models import BreachEvent, BreachEventType, Severity


def clause_violation_event(transaction: TransactionPayload, result: EvaluationResult, outcome: PolicyOutcome) -> BreachEvent:
    """CRITICAL: called from app.ledger.integration for every PolicyOutcome
    whose evaluation result is FAIL (a DENY decision actually enforced
    against a live production transaction) -- see that module's wiring."""
    violation_text = "; ".join(outcome.violations) if outcome.violations else "Policy violated with no detailed message."
    return BreachEvent(
        severity=Severity.CRITICAL,
        event_type=BreachEventType.CLAUSE_VIOLATION,
        title=f"Clause violation: {outcome.circular_number or 'unknown circular'} clause {outcome.clause_number or 'unscoped'}",
        description=violation_text,
        tenant_id=transaction.broker_id,
        transaction_id=transaction.transaction_id,
        rule_id=outcome.rule_id,
        circular_number=outcome.circular_number,
        clause_number=outcome.clause_number,
        metadata={"package": outcome.package, "decision": result.decision.value},
    )


def ambiguous_hitl_event(
    transaction: TransactionPayload | None,
    reason: str,
    hitl_case_id: str | None = None,
    rule_id: str | None = None,
    circular_number: str | None = None,
    clause_number: str | None = None,
) -> BreachEvent:
    """WARNING: called from either:
      - app.execution.evaluator when a transaction resolves to FLAGGED
        (an undefined OPA result -- see Evaluator._reduce's docstring), or
      - app.compiler.hitl when a newly compiled rule carries a BLOCKING
        HITLFlag (an ambiguous/qualitative clause the auditor could not
        confidently resolve).
    Both are "a human needs to look at this soon" situations, distinct
    from a CRITICAL live violation."""
    return BreachEvent(
        severity=Severity.WARNING,
        event_type=BreachEventType.AMBIGUOUS_HITL,
        title=f"Ambiguous rule requires review: {circular_number or 'unknown circular'} clause {clause_number or 'unscoped'}",
        description=reason,
        tenant_id=transaction.broker_id if transaction else None,
        transaction_id=transaction.transaction_id if transaction else None,
        rule_id=rule_id,
        circular_number=circular_number,
        clause_number=clause_number,
        hitl_case_id=hitl_case_id,
    )


def policy_compiled_event(rule_id: str, circular_number: str | None, clause_number: str | None, package: str) -> BreachEvent:
    """INFO: called from app.compiler.tasks on a successful
    compile_audited_rule result with `compiled=True` and no blocking HITL
    flags -- a routine, expected event that still belongs on the
    dashboard feed (compliance officers watching the feed should see
    healthy activity, not only incidents) but needs no acknowledgment and
    no escalation."""
    return BreachEvent(
        severity=Severity.INFO,
        event_type=BreachEventType.POLICY_COMPILED,
        title=f"Policy auto-compiled: {circular_number or 'unknown circular'} clause {clause_number or 'unscoped'}",
        description=f"Rule {rule_id} compiled successfully to OPA package '{package}'.",
        rule_id=rule_id,
        circular_number=circular_number,
        clause_number=clause_number,
        metadata={"package": package},
    )
