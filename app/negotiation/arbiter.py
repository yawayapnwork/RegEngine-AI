"""Requirement 2 -- Conflict Arbiter Agent: a higher-tier agent invoked
only when `app.negotiation.consensus.ConsensusEngine` deadlocks (no
round reached `negotiation_consensus_threshold` within
`negotiation_max_rounds`).

The arbiter does NOT re-run the domain agents or introduce new
evidence -- it cross-examines the LAST negotiation round's verdicts
against two fixed, deterministic precedence rules, applied in order (an
explicit-clause DENY always outranks a confidence-weighted vote,
mirroring `app.execution.evaluator.Evaluator._reduce`'s own
"any violation -> DENY, unconditionally" rule):

    1. EXPLICIT_DENY_PRECEDENCE -- any dissenting agent's DENY verdict
       that cites a specific circular_number + clause_number is treated
       as a verified regulatory breach, not a heuristic score, and wins
       outright regardless of other agents' confidence or weight.
    2. HIGHEST_WEIGHT_CONFIDENCE -- otherwise, the decision with the
       highest total weight*confidence in the final round wins, but
       ONLY if it clears `negotiation_arbiter_min_confidence` (a share
       of 0-1, same units as the consensus engine's tally) -- below
       that bar the arbiter has no confident basis to override a
       deadlock and MUST escalate.

Anything the arbiter cannot resolve by rule 1 or 2 returns `None`,
which `app.negotiation.orchestrator.NegotiationOrchestrator` is
required to treat as "route to HITL with the full transcript" --
the arbiter is built to escalate by DEFAULT on doubt, not to force
a resolution.
"""
from __future__ import annotations

import logging

from app.execution.models import Decision
from app.negotiation.models import ArbiterResolution, ConflictReasoningNode, NegotiationRound
from app.negotiation.consensus import tally_votes

logger = logging.getLogger(__name__)


def build_reasoning_tree(rounds: list[NegotiationRound]) -> ConflictReasoningNode:
    """Renders every round's verdicts into the audit-facing conflict
    reasoning tree -- root claim is the transaction-level disagreement,
    one child per round, one grandchild per agent verdict in that round
    citing whatever SEBI clause it relied on. Pure and deterministic:
    the exact same `rounds` list always renders the exact same tree, so
    a stored transcript's tree can be independently regenerated and
    diffed against what's on record."""
    round_nodes = []
    for r in rounds:
        agent_nodes = [
            ConflictReasoningNode(
                claim=f"{v.agent_key} voted {v.decision.value} (confidence={v.confidence:.2f}, weight={v.weight:.2f}): {v.rationale}",
                supporting_agent_key=v.agent_key,
                supporting_circular_number=v.cited_circular_number,
                supporting_clause_number=v.cited_clause_number,
                evidence_confidence=v.confidence,
            )
            for v in r.verdicts
        ]
        round_nodes.append(
            ConflictReasoningNode(
                claim=f"Round {r.round_number}: leading decision {r.tally.leading_decision.value} at {r.tally.leading_share:.0%} weighted share (consensus_reached={r.consensus_reached})",
                children=agent_nodes,
            )
        )
    return ConflictReasoningNode(claim="Multi-agent negotiation deadlocked; arbiter cross-examination follows.", children=round_nodes)


class ConflictArbiterAgent:
    def __init__(self, min_confidence_to_resolve: float) -> None:
        self._min_confidence = min_confidence_to_resolve

    def arbitrate(self, rounds: list[NegotiationRound]) -> ArbiterResolution | None:
        if not rounds:
            return None
        final_round = rounds[-1]

        explicit_deny = self._explicit_deny_precedence(final_round)
        if explicit_deny is not None:
            return explicit_deny

        return self._highest_weight_confidence(final_round)

    def _explicit_deny_precedence(self, final_round: NegotiationRound) -> ArbiterResolution | None:
        deny_verdicts_with_clause = [
            v for v in final_round.verdicts
            if v.decision == Decision.DENY and v.cited_circular_number and v.cited_clause_number
        ]
        if not deny_verdicts_with_clause:
            return None

        # Deterministic tie-break among multiple clause-cited DENYs:
        # highest agent weight, then highest confidence, then agent_key
        # for total reproducibility.
        winner = max(deny_verdicts_with_clause, key=lambda v: (v.weight, v.confidence, v.agent_key))
        overridden = [v.agent_key for v in final_round.verdicts if v.agent_key != winner.agent_key]

        return ArbiterResolution(
            resolved_decision=Decision.DENY,
            justification=(
                f"{winner.agent_key} cited an explicit regulatory breach at "
                f"Circular {winner.cited_circular_number}, Clause {winner.cited_clause_number}: {winner.rationale}. "
                "An explicit, clause-cited DENY overrides confidence-weighted disagreement from other domain agents."
            ),
            precedence_rule="EXPLICIT_DENY_PRECEDENCE",
            cited_circular_number=winner.cited_circular_number,
            cited_clause_number=winner.cited_clause_number,
            overridden_agent_keys=overridden,
            winning_confidence=winner.confidence,
        )

    def _highest_weight_confidence(self, final_round: NegotiationRound) -> ArbiterResolution | None:
        tally = tally_votes(final_round.verdicts)
        if tally.leading_share < self._min_confidence:
            logger.info(
                "Arbiter cannot confidently resolve deadlock: leading share %.3f below min_confidence %.3f.",
                tally.leading_share, self._min_confidence,
            )
            return None

        winning_verdicts = [v for v in final_round.verdicts if v.decision == tally.leading_decision]
        best = max(winning_verdicts, key=lambda v: (v.weight * v.confidence, v.agent_key))
        overridden = [v.agent_key for v in final_round.verdicts if v.decision != tally.leading_decision]

        return ArbiterResolution(
            resolved_decision=tally.leading_decision,
            justification=(
                f"No single agent cited an explicit clause-grounded breach; {tally.leading_decision.value} carried "
                f"{tally.leading_share:.0%} of weighted voting power in the final round, above the "
                f"{self._min_confidence:.0%} confidence bar required for the arbiter to resolve without HITL. "
                f"Strongest supporting verdict: {best.rationale}"
            ),
            precedence_rule="HIGHEST_WEIGHT_CONFIDENCE",
            cited_circular_number=best.cited_circular_number,
            cited_clause_number=best.cited_clause_number,
            overridden_agent_keys=overridden,
            winning_confidence=tally.leading_share,
        )
