"""Data models and schemas for the Multi-Agent Arbitration capability (PRD Addendum v2 Section 8.3).

Enforces strict schema validation for:
- AgentRole, ModelAttribution (identifying which checkpoint produced which argument)
- CanonicalFactClaim, AgentArgument (structured arguments with source citations)
- ArbitrationRound (round-level debate between Extractor and Auditor)
- ArbiterVerdict (Arbiter's synthesis, same-checkpoint risk flag, security flags)
- ArbitrationTranscript (tamper-evident audit transcript with SHA-256 provenance)
"""
from __future__ import annotations

import datetime as dt
from enum import Enum
import hashlib
import json
from typing import Any
import uuid

from pydantic import BaseModel, Field


class AgentRole(str, Enum):
    EXTRACTOR = "extractor"
    AUDITOR = "auditor"
    ARBITER = "arbiter"


class ArbitrationOutcome(str, Enum):
    CONSENSUS = "consensus"
    DISAGREEMENT = "disagreement"
    REVIEW_REQUIRED = "review_required"
    SECURITY_BLOCKED = "security_blocked"
    TIMEOUT = "timeout"


class ModelAttribution(BaseModel):
    """Identifies the model/checkpoint that generated an argument.
    Critical for ensuring same-checkpoint reasoning is not falsely presented
    as genuinely independent evidence.
    """

    agent_role: AgentRole
    provider_type: str = Field(..., description="Provider: offline, openai, anthropic, huggingface")
    model_name: str = Field(..., description="Model identifier or checkpoint path")
    checkpoint_id: str | None = Field(None, description="Specific checkpoint hash or weights version")
    temperature: float = Field(0.0, ge=0.0, le=2.0)


class CanonicalFactClaim(BaseModel):
    """A deterministic fact claim extracted by an agent with verbatim source citation."""

    metric: str = Field(..., description="Metric name or identifier")
    canonical_fact: str | None = Field(None, description="Resolved canonical fact from app.regulatory.facts")
    operator: str = Field(..., description="Comparison operator: >=, >, <=, <, ==, range")
    value: float = Field(..., description="Numeric threshold value")
    unit: str = Field(..., description="Unit of measurement: %, INR crore, days, etc.")
    verbatim_evidence: str = Field(..., description="Exact quoted substring from the source clause")


class AgentArgument(BaseModel):
    """Structured argument submitted by an agent in an arbitration round.
    Every claim must be grounded in source text quotes and canonical facts.
    """

    argument_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    agent_role: AgentRole
    round_number: int = Field(1, ge=1)
    interpretation: str = Field(..., description="Natural language interpretation of the clause obligation")
    canonical_facts: list[CanonicalFactClaim] = Field(
        default_factory=list, description="Machine-checkable numeric threshold claims"
    )
    relevant_source_text: list[str] = Field(
        default_factory=list, description="List of verbatim quoted text spans supporting this argument"
    )
    source_location: str = Field(..., description="Clause number or section reference (e.g. 'Clause 4.2.b')")
    confidence: float = Field(..., ge=0.0, le=1.0, description="Agent's certainty in this argument")
    reasoning_summary: str = Field(..., description="Summary of reasoning leading to this interpretation")
    attribution: ModelAttribution
    obligation_type: str | None = Field(None, description="mandatory, prohibited, conditional, recommended")
    target_entities: list[str] = Field(default_factory=list, description="Regulated entities covered")
    is_rebuttal: bool = Field(False, description="True if this argument counters an earlier round argument")
    created_at: dt.datetime = Field(default_factory=lambda: dt.datetime.now(dt.timezone.utc))


class ArbitrationRound(BaseModel):
    """One round of debate between the Extractor and Auditor agents."""

    round_number: int = Field(1, ge=1)
    extractor_argument: AgentArgument
    auditor_argument: AgentArgument
    discrepancies: list[str] = Field(
        default_factory=list, description="Material differences identified between arguments"
    )
    has_material_disagreement: bool = Field(
        False, description="True if agents disagree on threshold, operator, entity, or obligation"
    )


class ArbiterVerdict(BaseModel):
    """Resolution issued by the Conflict Arbiter Agent after cross-examining arguments."""

    verdict_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    outcome: ArbitrationOutcome
    chosen_interpretation: str | None = Field(
        None, description="Consensus or resolved interpretation if determinable without invention"
    )
    accepted_canonical_facts: list[CanonicalFactClaim] = Field(
        default_factory=list, description="Facts verified against source text and canonical taxonomy"
    )
    rejected_claims: list[dict[str, Any]] = Field(
        default_factory=list, description="Claims rejected due to lack of source evidence or taxonomy mismatch"
    )
    confidence: float = Field(..., ge=0.0, le=1.0)
    justification: str = Field(..., description="Detailed justification referencing source quotes and rules")
    attribution: ModelAttribution
    hitl_escalation_reason: str | None = Field(
        None, description="Reason code when escalating to human review (e.g. material_disagreement, ambiguity)"
    )
    same_model_risk: bool = Field(
        False, description="True if Extractor, Auditor, and Arbiter shared the same underlying model checkpoint"
    )
    security_flags: list[str] = Field(
        default_factory=list, description="Security findings such as prompt injection attempts"
    )
    decided_at: dt.datetime = Field(default_factory=lambda: dt.datetime.now(dt.timezone.utc))


class ArbitrationTranscript(BaseModel):
    """Complete, tamper-evident audit record of the multi-agent arbitration session.
    Retains full cryptographic provenance over source document, clause, arguments, and verdict.
    """

    session_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    tenant_id: str = Field(..., description="Tenant owning this arbitration session enforcing isolation")
    circular_id: str = Field(..., description="Source circular reference ID")
    clause_id: str = Field(..., description="Source clause reference ID")
    source_document_sha256: str = Field(..., description="SHA-256 of the physical circular PDF")
    clause_sha256: str = Field(..., description="SHA-256 of the clause chunk text")
    untrusted_source_text: str = Field(..., description="Verbatim raw clause text treated as untrusted data")
    rounds: list[ArbitrationRound] = Field(default_factory=list)
    arbiter_verdict: ArbiterVerdict | None = None
    final_outcome: ArbitrationOutcome = ArbitrationOutcome.REVIEW_REQUIRED
    transcript_sha256: str = Field(
        "", description="Tamper-evident SHA-256 digest computed over canonical JSON transcript"
    )
    hitl_review_id: str | None = Field(
        None, description="ID of created HITLReview record if escalated to human compliance officer"
    )
    started_at: dt.datetime = Field(default_factory=lambda: dt.datetime.now(dt.timezone.utc))
    completed_at: dt.datetime | None = None
    duration_seconds: float | None = None

    def compute_hash(self) -> str:
        """Computes deterministic SHA-256 digest over the canonical JSON serialization of this transcript."""
        data = {
            "session_id": self.session_id,
            "tenant_id": self.tenant_id,
            "circular_id": self.circular_id,
            "clause_id": self.clause_id,
            "source_document_sha256": self.source_document_sha256,
            "clause_sha256": self.clause_sha256,
            "final_outcome": self.final_outcome.value,
            "rounds_count": len(self.rounds),
            "verdict": self.arbiter_verdict.model_dump(mode="json") if self.arbiter_verdict else None,
        }
        canonical_str = json.dumps(data, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical_str.encode("utf-8")).hexdigest()

    def seal(self) -> str:
        """Seals the transcript with its cryptographic hash."""
        self.transcript_sha256 = self.compute_hash()
        return self.transcript_sha256
