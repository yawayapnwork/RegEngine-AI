"""Requirement 1 -- Consensus Engine: a deterministic, weighted-voting
state machine over `DomainAgent` verdicts.

State machine (per negotiation):

    COLLECTING -> (gather every agent's verdict for this round)
    COLLECTING -> CONSENSUS_REACHED   if the round's weighted tally clears negotiation_consensus_threshold
    COLLECTING -> NEGOTIATING         otherwise, and rounds remain
    NEGOTIATING -> COLLECTING         (next round: dissenting agents re-evaluate with the prior round's
                                       verdicts as context -- see `_context_for_next_round`)
    NEGOTIATING -> DEADLOCKED         once negotiation_max_rounds is exhausted with no consensus

DEADLOCKED is not a failure state for the OVERALL protocol -- it is the
expected trigger for `app.negotiation.arbiter.ConflictArbiterAgent`,
exactly as an OPA `allow: None` is not a pipeline failure but the
expected trigger for HITL in app.execution.evaluator.

Voting is weighted by BOTH each agent's fixed domain weight (a
regulatory-severity prior, e.g. MarginAgent > RiskDisclosureAgent) and
its per-verdict confidence (how certain that specific evaluation was) --
`weight * confidence` is each verdict's voting power, and shares are
computed as a fraction of total voting power among agents that did not
abstain. This is deterministic and reproducible: the same set of
verdicts always yields the same tally, with no randomness or model
sampling anywhere in the loop -- required for an audit trail a SEBI
examiner can independently re-derive from the transcript alone.
"""
from __future__ import annotations

import asyncio
import logging
from enum import Enum

from app.execution.models import Decision, PolicyOutcome, TransactionPayload
from app.negotiation.agents import DomainAgent
from app.negotiation.models import AgentVerdict, NegotiationRound, WeightedVoteTally

logger = logging.getLogger(__name__)

# Most-restrictive-wins tie-break: when the leading decision's share is
# within this margin of DENY's share, DENY wins the round even if it
# wasn't nominally ahead -- the same safety-first bias
# app.execution.evaluator.Evaluator._reduce applies unconditionally
# (any violation -> DENY, full stop). Here it's a tie-break rather than
# an absolute rule because a genuinely small, confidently-voted DENY
# minority (e.g. one low-confidence flag against two confident ALLOWs)
# should still be able to lose a round and proceed to negotiation/
# arbitration instead of blocking every transaction outright.
_DENY_TIE_BREAK_MARGIN = 0.05


class ConsensusState(str, Enum):
    COLLECTING = "collecting"
    CONSENSUS_REACHED = "consensus_reached"
    NEGOTIATING = "negotiating"
    DEADLOCKED = "deadlocked"


def tally_votes(verdicts: list[AgentVerdict]) -> WeightedVoteTally:
    """Pure function: verdicts in, tally out. No I/O, no randomness --
    the core of why this consensus mechanism is deterministic and
    independently re-computable from a stored transcript."""
    voting_power: dict[str, float] = {d.value: 0.0 for d in Decision}
    total_power = 0.0
    for v in verdicts:
        power = v.weight * v.confidence
        voting_power[v.decision.value] += power
        total_power += power

    if total_power == 0.0:
        # Every agent abstained or voted with zero confidence -- cannot
        # happen with the current agents (confidence is always >0 in
        # app.negotiation.agents.DomainAgent._reduce_scoped_outcomes),
        # but handled explicitly rather than dividing by zero.
        return WeightedVoteTally(weighted_share={d.value: 0.0 for d in Decision}, leading_decision=Decision.FLAGGED, leading_share=0.0, unanimous=False)

    share = {decision: power / total_power for decision, power in voting_power.items()}

    deny_share = share[Decision.DENY.value]
    leading_decision_value = max(share, key=share.get)
    if leading_decision_value != Decision.DENY.value and (share[leading_decision_value] - deny_share) < _DENY_TIE_BREAK_MARGIN and deny_share > 0:
        leading_decision_value = Decision.DENY.value

    unanimous = len({v.decision for v in verdicts}) == 1

    return WeightedVoteTally(
        weighted_share=share,
        leading_decision=Decision(leading_decision_value),
        leading_share=share[leading_decision_value],
        unanimous=unanimous,
    )


def check_consensus(tally: WeightedVoteTally, threshold: float) -> bool:
    return tally.unanimous or tally.leading_share >= threshold


class ConsensusEngine:
    def __init__(self, agents: list[DomainAgent], consensus_threshold: float, max_rounds: int) -> None:
        self._agents = agents
        self._threshold = consensus_threshold
        self._max_rounds = max_rounds

    async def run(self, transaction: TransactionPayload, policy_outcomes: list[PolicyOutcome]) -> tuple[list[NegotiationRound], ConsensusState]:
        rounds: list[NegotiationRound] = []
        state = ConsensusState.COLLECTING
        prior_round: NegotiationRound | None = None

        for round_number in range(1, self._max_rounds + 1):
            verdicts = await self._collect_verdicts(transaction, policy_outcomes, prior_round)
            if not verdicts:
                # No agent had anything in-scope for this transaction --
                # not a negotiation at all; the caller
                # (app.negotiation.orchestrator) is expected to check for
                # this and fall back to a plain ALLOW rather than call
                # ConsensusEngine.run in the first place, but handled
                # defensively here too.
                logger.debug("No in-scope agent verdicts for transaction %s; nothing to negotiate.", transaction.transaction_id)
                return rounds, ConsensusState.DEADLOCKED

            tally = tally_votes(verdicts)
            consensus_reached = check_consensus(tally, self._threshold)
            dissenting = [v.agent_key for v in verdicts if v.decision != tally.leading_decision]

            this_round = NegotiationRound(round_number=round_number, verdicts=verdicts, tally=tally, consensus_reached=consensus_reached, dissenting_agents=dissenting)
            rounds.append(this_round)

            if consensus_reached:
                return rounds, ConsensusState.CONSENSUS_REACHED

            state = ConsensusState.NEGOTIATING
            prior_round = this_round

        return rounds, ConsensusState.DEADLOCKED

    async def _collect_verdicts(
        self, transaction: TransactionPayload, policy_outcomes: list[PolicyOutcome], prior_round: NegotiationRound | None
    ) -> list[AgentVerdict]:
        results = await asyncio.gather(*(agent.evaluate(transaction, policy_outcomes, prior_round) for agent in self._agents))
        return [v for v in results if v is not None]
