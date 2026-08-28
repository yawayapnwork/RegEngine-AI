"""Core decision logic: evaluate one `TransactionPayload` against every
compiled policy registered for its `entity_type` and reduce the per-policy
OPA outcomes into a single `allow` / `deny` / `flagged` verdict.

Reduction rules (most-restrictive-wins, then safest-on-ambiguity):
1. Any policy explicitly reporting a violation -> DENY. A single confirmed
   breach is never overridden by other policies allowing.
2. No violations, but at least one policy came back UNDEFINED (OPA could
   not evaluate it — usually a missing `facts` key) -> FLAGGED. Treating
   missing data as an automatic allow would defeat the point of the
   control; treating it as an automatic deny would block brokers on
   payload gaps that a human may resolve in seconds. HITL is the correct
   fallback, per requirement 3.
3. No violations, nothing undefined, and at least one policy matched ->
   ALLOW.
4. No policy matched this entity_type at all -> ALLOW (no applicable
   compliance rule exists yet for this transaction shape) but this is
   logged distinctly from case 3 so policy-coverage gaps are visible.
"""
from __future__ import annotations

import logging
import time

from app.execution.hitl_queue import HITLQueue
from app.execution.models import Decision, EvaluationResult, PolicyOutcome, TransactionPayload
from app.execution.opa_engine import OPAEngine, OPAEngineError
from app.execution.policy_cache import PolicyLookup

logger = logging.getLogger(__name__)


class Evaluator:
    def __init__(self, opa_engine: OPAEngine, policy_registry: PolicyLookup, hitl_queue: HITLQueue) -> None:
        """`policy_registry` only needs to satisfy `PolicyLookup`
        (`async def policies_for(entity_type) -> list[dict]`) -- production
        wiring (app.execution.dependencies.get_evaluator) passes a
        `PolicyCache` for sub-millisecond lookups on the hot path; a bare
        `PolicyRegistry` (or a test double) works identically, just without
        the L1 cache in front of it."""
        self._opa = opa_engine
        self._registry = policy_registry
        self._hitl = hitl_queue

    async def evaluate_transaction(self, transaction: TransactionPayload) -> EvaluationResult:
        started = time.perf_counter()
        input_doc = {"entity_type": transaction.entity_type, "facts": transaction.facts}

        applicable = await self._registry.policies_for(transaction.entity_type)
        if not applicable:
            logger.info("No compiled policy applies to entity_type=%s (transaction=%s).", transaction.entity_type, transaction.transaction_id)
            return EvaluationResult(
                transaction_id=transaction.transaction_id,
                decision=Decision.ALLOW,
                reasons=["No compiled policy currently applies to this entity_type."],
                latency_ms=(time.perf_counter() - started) * 1000,
            )

        outcomes: list[PolicyOutcome] = []
        for entry in applicable:
            rule_id, package = entry["rule_id"], entry["package"]
            try:
                result = await self._opa.evaluate(package, input_doc)
            except OPAEngineError:
                logger.exception("OPA evaluation failed for rule_id=%s (transaction=%s).", rule_id, transaction.transaction_id)
                outcomes.append(PolicyOutcome(rule_id=rule_id, package=package, allow=None))
                continue

            if result is None:
                outcomes.append(PolicyOutcome(rule_id=rule_id, package=package, allow=None))
            else:
                outcomes.append(
                    PolicyOutcome(
                        rule_id=rule_id,
                        package=package,
                        allow=bool(result.get("allow", False)),
                        violations=list(result.get("violations", []) or []),
                        circular_number=result.get("circular_number"),
                        clause_number=result.get("clause_number"),
                    )
                )

        decision, reasons = self._reduce(outcomes)

        hitl_case_id = None
        if decision == Decision.FLAGGED:
            case = await self._hitl.enqueue(
                transaction=transaction,
                reason="; ".join(reasons) or "One or more policies returned an undefined decision.",
                matched_policies=outcomes,
            )
            hitl_case_id = case.case_id

        return EvaluationResult(
            transaction_id=transaction.transaction_id,
            decision=decision,
            matched_policies=outcomes,
            reasons=reasons,
            hitl_case_id=hitl_case_id,
            latency_ms=(time.perf_counter() - started) * 1000,
        )

    @staticmethod
    def _reduce(outcomes: list[PolicyOutcome]) -> tuple[Decision, list[str]]:
        violations = [msg for o in outcomes for msg in o.violations]
        if violations:
            return Decision.DENY, violations

        undefined = [o.rule_id for o in outcomes if o.allow is None]
        if undefined:
            return Decision.FLAGGED, [f"Policy(ies) {undefined} returned an undefined result — insufficient or malformed facts."]

        return Decision.ALLOW, []
