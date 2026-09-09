"""[FROZEN / NON-MVP EXPERIMENTAL SUBSYSTEM]
===============================================================================
Status: Frozen / Non-MVP Experimental
Core MVP Pipeline: Regulatory Document -> Extraction -> Clause Interpretation
                  -> Canonical Facts -> Policy Compilation -> HITL Review
                  -> Policy Activation -> OPA Evaluation -> Evidence Ledger

This subsystem is preserved for future architectural extensions but is outside
the active regulatory-compliance MVP. It is not imported or required by the
core execution, compilation, ingestion, or ledger pipeline.
===============================================================================

Multi-agent negotiation protocol for resolving conflicting
multi-clause compliance requirements during trade execution.

Where `app.execution.evaluator.Evaluator` reduces every compiled
policy's raw OPA outcome with one fixed rule (any violation -> DENY),
this package sits ONE LAYER ABOVE that reduction for transactions whose
matched policies span more than one regulatory sub-domain: specialized
`DomainAgent`s (Margin, Risk Disclosure, Fund Segregation) each cast an
independent, evidence-cited vote, a deterministic weighted-consensus
state machine (`app.negotiation.consensus`) tries to resolve
disagreement across bounded negotiation rounds, and a higher-tier
`ConflictArbiterAgent` (`app.negotiation.arbiter`) either issues a
definitive, clause-cited resolution or escalates to the SAME
`app.execution.hitl_queue.HITLQueue` an ambiguous OPA result already
uses -- one human review queue, not two.

Gated behind `settings.negotiation_enabled` (default False); a
deployment that never enables it is entirely unaffected -- this package
is additive, invoked by a caller choosing to run it on top of
`Evaluator.evaluate_transaction`'s output, not a replacement for it.
"""
from app.negotiation.agents import (
    DomainAgent,
    FundSegregationAgent,
    MarginAgent,
    RiskDisclosureAgent,
)
from app.negotiation.arbiter import ConflictArbiterAgent, build_reasoning_tree
from app.negotiation.arbitration_engine import ArbitrationEngine
from app.negotiation.arbitration_models import (
    AgentArgument,
    AgentRole,
    ArbiterVerdict,
    ArbitrationOutcome,
    ArbitrationRound,
    ArbitrationTranscript,
    CanonicalFactClaim,
    ModelAttribution,
)
from app.negotiation.arbitration_store import ArbitrationTranscriptStore
from app.negotiation.consensus import ConsensusEngine, ConsensusState, check_consensus, tally_votes
from app.negotiation.hitl_bridge import (
    format_arbitration_transcript_for_hitl,
    should_escalate_arbitration_to_hitl,
)
from app.negotiation.models import (
    AgentVerdict,
    ArbiterResolution,
    ConflictReasoningNode,
    NegotiationResult,
    NegotiationRound,
    NegotiationStatus,
    WeightedVoteTally,
)
from app.negotiation.orchestrator import NegotiationOrchestrator, should_negotiate
from app.negotiation.security import inspect_for_prompt_injection, isolate_untrusted_source_text
from app.negotiation.telemetry import NegotiationTranscriptStore as DomainNegotiationTranscriptStore

__all__ = [
    # Domain Negotiation
    "DomainAgent",
    "MarginAgent",
    "RiskDisclosureAgent",
    "FundSegregationAgent",
    "ConflictArbiterAgent",
    "build_reasoning_tree",
    "ConsensusEngine",
    "ConsensusState",
    "check_consensus",
    "tally_votes",
    "AgentVerdict",
    "ArbiterResolution",
    "ConflictReasoningNode",
    "NegotiationResult",
    "NegotiationRound",
    "NegotiationStatus",
    "WeightedVoteTally",
    "NegotiationOrchestrator",
    "should_negotiate",
    "DomainNegotiationTranscriptStore",
    # Clause Extraction/Auditing Arbitration
    "ArbitrationEngine",
    "ArbitrationTranscriptStore",
    "AgentRole",
    "ArbitrationOutcome",
    "AgentArgument",
    "ArbitrationRound",
    "ArbiterVerdict",
    "ArbitrationTranscript",
    "CanonicalFactClaim",
    "ModelAttribution",
    "format_arbitration_transcript_for_hitl",
    "should_escalate_arbitration_to_hitl",
    "inspect_for_prompt_injection",
    "isolate_untrusted_source_text",
]

