"""Top-level entrypoint: wires the consensus engine, the conflict
arbiter, the HITL escalation path, and the telemetry store into one
call. Sits downstream of `app.execution.evaluator.Evaluator` -- a
caller (e.g. a future negotiation-aware execution route, or a Celery
task processing a batch) is expected to run `Evaluator.evaluate_transaction`
first to get its `matched_policies: list[PolicyOutcome]`, and pass those
into `NegotiationOrchestrator.negotiate` ONLY when negotiation is worth
running at all (see `should_negotiate`) -- this package never calls OPA
itself.
"""
from __future__ import annotations

import datetime as dt
import logging
import time
import uuid

from app.execution.hitl_queue import HITLQueue
from app.execution.models import Decision, PolicyOutcome, TransactionPayload
from app.negotiation.agents import DomainAgent
from app.negotiation.arbiter import ConflictArbiterAgent, build_reasoning_tree
from app.negotiation.consensus import ConsensusEngine, ConsensusState
from app.negotiation.models import NegotiationResult, NegotiationStatus
from app.negotiation.telemetry import NegotiationTranscriptStore
from app.observability.metrics import NEGOTIATION_DURATION_SECONDS, NEGOTIATION_OUTCOME_TOTAL, NEGOTIATION_ROUNDS_TOTAL
from app.observability.tracing import traced_span

logger = logging.getLogger(__name__)


def should_negotiate(policy_outcomes: list[PolicyOutcome], agents: list[DomainAgent]) -> bool:
    """Negotiation is only worth running when at least two DIFFERENT
    domain agents would actually have something in-scope to vote on --
    a transaction whose policies fall entirely within one agent's
    domain (or none at all) has nothing to negotiate; a caller should
    keep `Evaluator.evaluate_transaction`'s own decision in that case,
    not pay for a negotiation round that can only ever be unanimous."""
    domains_in_scope = {agent.domain for agent in agents if agent.scoped_outcomes(policy_outcomes)}
    return len(domains_in_scope) >= 2


class NegotiationOrchestrator:
    def __init__(
        self,
        agents: list[DomainAgent],
        consensus_engine: ConsensusEngine,
        arbiter: ConflictArbiterAgent,
        hitl_queue: HITLQueue,
        transcript_store: NegotiationTranscriptStore,
    ) -> None:
        self._agents = agents
        self._consensus_engine = consensus_engine
        self._arbiter = arbiter
        self._hitl = hitl_queue
        self._transcript = transcript_store

    async def negotiate(self, transaction: TransactionPayload, policy_outcomes: list[PolicyOutcome]) -> NegotiationResult:
        negotiation_id = str(uuid.uuid4())
        started = time.perf_counter()

        with traced_span("negotiation.negotiate", transaction_id=transaction.transaction_id, negotiation_id=negotiation_id):
            rounds, state = await self._consensus_engine.run(transaction, policy_outcomes)

            for round_ in rounds:
                await self._transcript.record_round(negotiation_id, round_)
                NEGOTIATION_ROUNDS_TOTAL.labels(consensus_reached=str(round_.consensus_reached).lower()).inc()

            if state == ConsensusState.CONSENSUS_REACHED:
                result = self._consensus_result(negotiation_id, transaction, rounds)
            else:
                result = await self._resolve_deadlock(negotiation_id, transaction, policy_outcomes, rounds)

            result.completed_at = dt.datetime.now(dt.timezone.utc)
            await self._transcript.record_outcome(result)

            NEGOTIATION_OUTCOME_TOTAL.labels(status=result.status.value).inc()
            NEGOTIATION_DURATION_SECONDS.observe(time.perf_counter() - started)

            logger.info(
                "Negotiation %s for transaction %s resolved: status=%s decision=%s rounds=%d",
                negotiation_id, transaction.transaction_id, result.status.value, result.final_decision.value, len(rounds),
            )
            return result

    def _consensus_result(self, negotiation_id: str, transaction: TransactionPayload, rounds) -> NegotiationResult:
        final_tally = rounds[-1].tally
        return NegotiationResult(
            negotiation_id=negotiation_id, transaction_id=transaction.transaction_id,
            status=NegotiationStatus.CONSENSUS, final_decision=final_tally.leading_decision, rounds=rounds,
        )

    async def _resolve_deadlock(self, negotiation_id: str, transaction: TransactionPayload, policy_outcomes, rounds) -> NegotiationResult:
        # Consensus never even started a round (no agent had anything
        # in-scope) -- this is a caller error (should_negotiate should
        # have prevented it), handled defensively as a safe ALLOW with no
        # arbitration/HITL noise rather than escalating something with
        # zero actual disagreement.
        if not rounds:
            return NegotiationResult(
                negotiation_id=negotiation_id, transaction_id=transaction.transaction_id,
                status=NegotiationStatus.CONSENSUS, final_decision=Decision.ALLOW, rounds=[],
            )

        resolution = self._arbiter.arbitrate(rounds)
        reasoning_tree = build_reasoning_tree(rounds)

        if resolution is not None:
            return NegotiationResult(
                negotiation_id=negotiation_id, transaction_id=transaction.transaction_id,
                status=NegotiationStatus.ARBITRATED, final_decision=resolution.resolved_decision,
                rounds=rounds, arbiter_resolution=resolution, reasoning_tree=reasoning_tree,
            )

        reason = (
            f"Multi-agent negotiation deadlocked after {len(rounds)} round(s) and the Conflict Arbiter Agent "
            "could not confidently resolve it (no explicit clause-cited breach, and the leading decision's "
            "weighted share fell below the configured confidence bar). Routed to human review with the full "
            "negotiation transcript."
        )
        case = await self._hitl.enqueue(transaction=transaction, reason=reason, matched_policies=policy_outcomes)
        logger.warning("Negotiation %s escalated to HITL: case_id=%s", negotiation_id, case.case_id)

        return NegotiationResult(
            negotiation_id=negotiation_id, transaction_id=transaction.transaction_id,
            status=NegotiationStatus.ESCALATED_TO_HITL, final_decision=Decision.FLAGGED,
            rounds=rounds, reasoning_tree=reasoning_tree, hitl_case_id=case.case_id,
        )
