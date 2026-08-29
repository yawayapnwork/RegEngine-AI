"""Data contracts for the negotiation protocol. Pydantic (not
dataclasses) throughout, matching app.execution.models's convention,
since these are serialized to JSON for the transcript store
(app.negotiation.telemetry) exactly like HITLCase.model_dump_json().
"""
from __future__ import annotations

import datetime as dt
from enum import Enum

from pydantic import BaseModel, Field

from app.execution.models import Decision


class NegotiationStatus(str, Enum):
    CONSENSUS = "consensus"            # agents agreed within negotiation_consensus_threshold, no arbiter needed
    ARBITRATED = "arbitrated"          # consensus engine deadlocked; arbiter issued a definitive resolution
    ESCALATED_TO_HITL = "escalated_to_hitl"  # arbiter itself could not confidently resolve


class AgentVerdict(BaseModel):
    """One domain agent's independent evaluation of a transaction,
    scoped to the `PolicyOutcome`s that fall within its sub-domain."""

    agent_key: str = Field(..., description='Matches app.db.models.AgentInventory.agent_key\'s convention, e.g. "margin_agent".')
    domain: str
    decision: Decision
    confidence: float = Field(..., ge=0.0, le=1.0, description="This agent's own certainty in its decision, independent of its fixed voting weight.")
    weight: float = Field(..., gt=0.0, description="This agent's fixed voting weight in the consensus engine (app.config settings or agent registration, not learned).")
    cited_circular_number: str | None = None
    cited_clause_number: str | None = None
    rationale: str
    matched_rule_ids: list[str] = Field(default_factory=list, description="PolicyOutcome.rule_id values this verdict was derived from.")


class WeightedVoteTally(BaseModel):
    """The consensus engine's deterministic reduction of one round's
    `AgentVerdict`s into a weighted share per decision."""

    weighted_share: dict[str, float] = Field(default_factory=dict, description="Decision.value -> fraction of total weight*confidence voting power.")
    leading_decision: Decision
    leading_share: float
    unanimous: bool


class NegotiationRound(BaseModel):
    round_number: int
    verdicts: list[AgentVerdict]
    tally: WeightedVoteTally
    consensus_reached: bool
    dissenting_agents: list[str] = Field(default_factory=list)


class ConflictReasoningNode(BaseModel):
    """One node of the conflict reasoning tree the arbiter builds while
    cross-examining dissenting agents' cited SEBI clauses -- serialized
    verbatim into the audit transcript so a human reviewer (or a SEBI
    auditor examining a HITL escalation later) can see exactly which
    claim beat which, and why."""

    claim: str
    supporting_agent_key: str | None = None
    supporting_circular_number: str | None = None
    supporting_clause_number: str | None = None
    evidence_confidence: float | None = None
    children: list["ConflictReasoningNode"] = Field(default_factory=list)


ConflictReasoningNode.model_rebuild()


class ArbiterResolution(BaseModel):
    resolved_decision: Decision
    justification: str
    precedence_rule: str = Field(..., description='Which fixed rule in app.negotiation.arbiter.ConflictArbiterAgent.arbitrate resolved this, e.g. "EXPLICIT_DENY_PRECEDENCE".')
    cited_circular_number: str | None = None
    cited_clause_number: str | None = None
    overridden_agent_keys: list[str] = Field(default_factory=list)
    winning_confidence: float


class NegotiationResult(BaseModel):
    negotiation_id: str
    transaction_id: str
    status: NegotiationStatus
    final_decision: Decision
    rounds: list[NegotiationRound]
    arbiter_resolution: ArbiterResolution | None = None
    reasoning_tree: ConflictReasoningNode | None = None
    hitl_case_id: str | None = None
    started_at: dt.datetime = Field(default_factory=lambda: dt.datetime.now(dt.timezone.utc))
    completed_at: dt.datetime | None = None
