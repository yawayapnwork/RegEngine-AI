"""Specialized domain agents. Each agent is scoped to a regulatory
sub-domain and only ever votes on the `PolicyOutcome`s it recognizes as
belonging to it -- an agent that sees nothing in its domain abstains
(returns None from `evaluate`) rather than casting an uninformed vote.

Domain scoping is a deterministic KEYWORD match against each
`PolicyOutcome`'s `violations` text and `package` string -- the same
"cheap heuristic over structured/semi-structured text, not an NLP
model" posture as app.graph.penalty_detector and
app.graph.supersession_extractor. This is a real limitation worth
stating plainly: `app.compiler.naming.rego_package_name` encodes
`<regulator>.<business_domain>.circulars.<circular>.clause_<clause>`
(see app.regulatory.taxonomy's `domains` tuples -- "broking", "amc",
etc.), not a regulatory sub-topic like "margin" or "risk disclosure",
so there is no structured field to key off today. A violation message
authored by app.compiler.rego_compiler from the source
NumericalThreshold.metric / ObligationType almost always names its
topic in prose (e.g. "Upfront margin must be at least..."), which is
what these keyword lists match against. A production deployment with a
richer OPA decision schema (e.g. an explicit `sub_domain` field in the
compiled Rego decision object) should replace keyword matching with
that field entirely -- the seam is `DomainAgent._is_in_scope`.
"""
from __future__ import annotations

import logging

from app.execution.models import Decision, PolicyOutcome, TransactionPayload
from app.negotiation.models import AgentVerdict, NegotiationRound

# How much an agent's confidence softens when a prior round's tally
# included another agent voting DENY, with an equal-or-greater domain
# weight AND a specific cited SEBI clause, against this agent's own
# non-DENY vote. This is the mechanism that makes multiple negotiation
# rounds meaningfully different from round 1 -- these agents have no
# LLM reasoning to "change their mind" with, but a lower-priority
# agent's confidence genuinely SHOULD erode across rounds when a
# higher-priority, clause-grounded objection stands unrebutted, letting
# the weighted tally converge toward consensus (or toward a clean
# deadlock if it never does) rather than silently repeating the exact
# same round forever.
_NEGOTIATION_CONCESSION_DELTA = 0.35
_MIN_CONFIDENCE_AFTER_CONCESSION = 0.05

logger = logging.getLogger(__name__)


def _outcome_text(outcome: PolicyOutcome) -> str:
    # `package` is a dotted/underscored Rego identifier (see
    # app.compiler.naming.rego_package_name), e.g.
    # "...clause_risk_disclosure" -- normalized to spaces so a
    # keyword like "risk disclosure" matches it the same way it would
    # match prose in `violations`.
    text = f"{outcome.package} {' '.join(outcome.violations)}".lower()
    return text.replace("_", " ").replace("-", " ")


class DomainAgent:
    """Base class -- subclasses set `agent_key`/`domain`/`weight`/
    `domain_keywords` as class attributes (see MarginAgent etc. below)
    and inherit `evaluate`/`_reduce_scoped_outcomes` as-is unless a
    sub-domain needs bespoke reduction logic."""

    agent_key: str
    domain: str
    weight: float
    domain_keywords: tuple[str, ...] = ()

    def _is_in_scope(self, outcome: PolicyOutcome) -> bool:
        text = _outcome_text(outcome)
        return any(keyword.replace("-", " ") in text for keyword in self.domain_keywords)

    def scoped_outcomes(self, policy_outcomes: list[PolicyOutcome]) -> list[PolicyOutcome]:
        return [o for o in policy_outcomes if self._is_in_scope(o)]

    async def evaluate(
        self,
        transaction: TransactionPayload,
        policy_outcomes: list[PolicyOutcome],
        prior_round: NegotiationRound | None = None,
    ) -> AgentVerdict | None:
        """Returns None (abstains) when no in-scope policy outcome
        exists for this transaction -- the consensus engine
        (app.negotiation.consensus) excludes abstaining agents from the
        vote tally entirely, rather than counting an abstention as a
        vote for any particular decision.

        `prior_round`, when given (round 2+), lets this agent's
        confidence soften under negotiation pressure from a
        higher-priority, clause-grounded dissenting vote -- see
        `_apply_negotiation_pressure`. This agent's own DECISION never
        changes; only how strongly it's held does, which is what lets
        the weighted tally shift across rounds without any agent
        fabricating agreement it doesn't have."""
        scoped = self.scoped_outcomes(policy_outcomes)
        if not scoped:
            return None
        verdict = self._reduce_scoped_outcomes(transaction, scoped)
        return self._apply_negotiation_pressure(verdict, prior_round)

    def _apply_negotiation_pressure(self, verdict: AgentVerdict, prior_round: NegotiationRound | None) -> AgentVerdict:
        if prior_round is None:
            return verdict

        stronger_dissent = any(
            other.agent_key != self.agent_key
            and other.decision == Decision.DENY
            and other.decision != verdict.decision
            and other.weight >= self.weight
            and other.cited_circular_number is not None
            for other in prior_round.verdicts
        )
        if not stronger_dissent:
            return verdict

        softened_confidence = max(_MIN_CONFIDENCE_AFTER_CONCESSION, verdict.confidence - _NEGOTIATION_CONCESSION_DELTA)
        return verdict.model_copy(update={
            "confidence": softened_confidence,
            "rationale": verdict.rationale + " [Confidence reduced: a higher-or-equal-weight agent cited a specific SEBI clause for DENY in the prior round.]",
        })

    def _reduce_scoped_outcomes(self, transaction: TransactionPayload, scoped: list[PolicyOutcome]) -> AgentVerdict:
        """Same most-restrictive-wins shape as
        app.execution.evaluator.Evaluator._reduce, applied only to this
        agent's own in-scope outcomes -- each domain agent is, in
        effect, a miniature Evaluator for its own sub-domain."""
        violations = [msg for o in scoped for msg in o.violations]
        matched_rule_ids = [o.rule_id for o in scoped]
        cited = next((o for o in scoped if o.circular_number), scoped[0])

        if violations:
            return AgentVerdict(
                agent_key=self.agent_key, domain=self.domain, decision=Decision.DENY,
                confidence=0.9, weight=self.weight,
                cited_circular_number=cited.circular_number, cited_clause_number=cited.clause_number,
                rationale="; ".join(violations), matched_rule_ids=matched_rule_ids,
            )

        undefined = [o for o in scoped if o.allow is None]
        if undefined:
            return AgentVerdict(
                agent_key=self.agent_key, domain=self.domain, decision=Decision.FLAGGED,
                confidence=0.4, weight=self.weight,
                cited_circular_number=cited.circular_number, cited_clause_number=cited.clause_number,
                rationale=f"{len(undefined)} in-domain polic(ies) returned an undefined result for transaction {transaction.transaction_id}.",
                matched_rule_ids=matched_rule_ids,
            )

        return AgentVerdict(
            agent_key=self.agent_key, domain=self.domain, decision=Decision.ALLOW,
            confidence=0.85, weight=self.weight,
            cited_circular_number=cited.circular_number, cited_clause_number=cited.clause_number,
            rationale=f"All {len(scoped)} in-domain polic(ies) passed for transaction {transaction.transaction_id}.",
            matched_rule_ids=matched_rule_ids,
        )


class MarginAgent(DomainAgent):
    agent_key = "margin_agent"
    domain = "margin"
    weight = 1.2  # margin shortfalls are a direct, immediate settlement-risk control -- weighted above disclosure/segregation defaults
    domain_keywords = ("margin", "mtm", "mark-to-market", "collateral")


class RiskDisclosureAgent(DomainAgent):
    agent_key = "risk_disclosure_agent"
    domain = "risk_disclosure"
    weight = 1.0
    domain_keywords = ("risk disclosure", "risk factor", "disclosure statement", "disclosure document")


class FundSegregationAgent(DomainAgent):
    agent_key = "fund_segregation_agent"
    domain = "fund_segregation"
    weight = 1.1  # client-money commingling is a fit-and-proper/registration-level breach, weighted above disclosure but below margin
    domain_keywords = ("segregat", "client fund", "client bank account", "client money", "commingl")
