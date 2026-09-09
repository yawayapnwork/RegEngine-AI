"""Pydantic schemas for the Unified HITL Review Evidence structure.

Enforces strict categorization, evidence typing, and anti-delegation invariants:
1. Every evidence artifact is tagged with one of the 6 canonical evidence types.
2. AI-generated analysis and simulations explicitly declare `is_authoritative = False`
   and carry mandatory non-delegation disclaimers.
3. Candidate rule version and cryptographic hashes (policy SHA-256, clause SHA-256)
   are immutably preserved.
"""
from __future__ import annotations

import datetime as dt
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class EvidenceType(str, Enum):
    SOURCE_EVIDENCE = "source evidence"
    DETERMINISTIC_EVIDENCE = "deterministic evidence"
    AI_GENERATED_ANALYSIS = "AI-generated analysis"
    HISTORICAL_PRECEDENT = "historical precedent"
    SIMULATION = "simulation"
    CRYPTOGRAPHIC_PROOF = "cryptographic proof"


class EvidenceStatus(str, Enum):
    AVAILABLE = "AVAILABLE"
    NOT_FOUND = "NOT_FOUND"
    DISABLED = "DISABLED"
    ERROR = "ERROR"


class BaseEvidenceSection(BaseModel):
    """Base schema for an individual evidence section."""
    evidence_type: EvidenceType
    status: EvidenceStatus = EvidenceStatus.AVAILABLE
    is_authoritative: bool = Field(
        ...,
        description="True for statutory source text and deterministic compiler rules; False for AI, simulations, and precedents.",
    )
    can_auto_approve: bool = Field(
        default=False,
        description="Invariant: must ALWAYS be False. No evidence artifact may ever auto-approve a policy.",
    )
    disclaimer: str | None = None
    notes: str | None = None


# ---------------------------------------------------------------------------
# 1. Source Evidence
# ---------------------------------------------------------------------------

class SourceEvidenceSection(BaseEvidenceSection):
    evidence_type: EvidenceType = EvidenceType.SOURCE_EVIDENCE
    is_authoritative: bool = True
    circular_id: int | None = None
    circular_number: str | None = None
    clause_id: int | None = None
    clause_number: str | None = None
    section_title: str | None = None
    section_path: list[str] = Field(default_factory=list)
    raw_text: str = ""
    source_sha256: str = ""
    page_start: int | None = None
    page_end: int | None = None


# ---------------------------------------------------------------------------
# 2. Deterministic Evidence (Compiler & Rule AST)
# ---------------------------------------------------------------------------

class DeterministicEvidenceSection(BaseEvidenceSection):
    evidence_type: EvidenceType = EvidenceType.DETERMINISTIC_EVIDENCE
    is_authoritative: bool = True
    rule_id: str = ""
    rule_version: int = 1
    policy_sha256: str = ""
    canonical_facts: dict[str, Any] = Field(default_factory=dict)
    jsonlogic_ast: dict[str, Any] | None = None
    rego_policy: str | None = None
    opa_package_name: str | None = None
    is_active: bool = False
    hitl_status: str = "BLOCKING"


# ---------------------------------------------------------------------------
# 3. AI-Generated Analysis (Arbitration Dialogue)
# ---------------------------------------------------------------------------

class ArbitrationEvidenceSection(BaseEvidenceSection):
    evidence_type: EvidenceType = EvidenceType.AI_GENERATED_ANALYSIS
    is_authoritative: bool = False
    disclaimer: str = (
        "ADVISORY ONLY: Multi-agent arbitration is AI-generated analysis. It does NOT "
        "constitute regulatory authority, legal approval, or statutory interpretation, "
        "and CANNOT override deterministic compiler rules or human compliance sign-off."
    )
    session_id: str | None = None
    final_outcome: str | None = None
    arbiter_confidence: float | None = None
    same_model_risk: bool = False
    security_flags: list[str] = Field(default_factory=list)
    transcript_sha256: str | None = None
    formatted_transcript: str | None = None


# ---------------------------------------------------------------------------
# 4. Historical Precedent (Case-Law Memory)
# ---------------------------------------------------------------------------

class PrecedentItem(BaseModel):
    precedent_id: str
    circular_id: str | None = None
    clause_id: str | None = None
    clause_number: str | None = None
    similarity_score: float
    resolution_status: str
    decision_rationales: list[str] = Field(default_factory=list)
    approved_notes: str | None = None
    source_clause_text: str | None = None


class PrecedentEvidenceSection(BaseEvidenceSection):
    evidence_type: EvidenceType = EvidenceType.HISTORICAL_PRECEDENT
    is_authoritative: bool = False
    disclaimer: str = (
        "ADVISORY ONLY: Historical precedents represent past human-in-the-loop decisions. "
        "They provide informational context only and CANNOT override current regulatory text, "
        "deterministic compiler logic, or human compliance judgment."
    )
    precedents_count: int = 0
    precedents: list[PrecedentItem] = Field(default_factory=list)
    reasoning_summary: str | None = None


# ---------------------------------------------------------------------------
# 5. Simulation (Digital Twin Rule-Impact Preview)
# ---------------------------------------------------------------------------

class DigitalTwinEvidenceSection(BaseEvidenceSection):
    evidence_type: EvidenceType = EvidenceType.SIMULATION
    is_authoritative: bool = False
    disclaimer: str = (
        "SIMULATION ONLY: Historical replay reflects past evaluated transactions. "
        "Zero historical impact does NOT infer regulatory correctness, legal validity, "
        "or future compliance under changing market conditions."
    )
    report_id: str | None = None
    lookback_days: int | None = None
    transactions_evaluated: int = 0
    baseline_pass_count: int = 0
    baseline_fail_count: int = 0
    candidate_pass_count: int = 0
    candidate_fail_count: int = 0
    newly_failing_count: int = 0
    newly_passing_count: int = 0
    failure_rate_delta: float = 0.0
    aggregate_financial_impact: dict[str, Any] | None = None
    dataset_snapshot_hash: str | None = None
    result_digest: str | None = None


# ---------------------------------------------------------------------------
# 6. Cryptographic Proof (ZKP Verification Evidence)
# ---------------------------------------------------------------------------

class ZKPProofItem(BaseModel):
    circuit_id: str
    proof_hash: str
    verified: bool
    ledger_sequence_num: int | None = None
    timestamp: str | None = None
    public_signals: list[str] = Field(default_factory=list)


class ZKPEvidenceSection(BaseEvidenceSection):
    evidence_type: EvidenceType = EvidenceType.CRYPTOGRAPHIC_PROOF
    is_authoritative: bool = False
    disclaimer: str = (
        "CRYPTOGRAPHIC EVIDENCE: Verifies arithmetic circuit satisfaction for declared "
        "public signals. Does NOT certify authenticity of off-chain transactions prior "
        "to witness generation and does NOT alter live policy state."
    )
    proof_count: int = 0
    proofs: list[ZKPProofItem] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# 7. M&A Due-Diligence Findings
# ---------------------------------------------------------------------------

class MNAFindingItem(BaseModel):
    job_id: str
    compared_entity_id: str
    difference_type: str
    severity: str
    title: str
    description: str
    provenance: dict[str, Any] = Field(default_factory=dict)


class MNAEvidenceSection(BaseEvidenceSection):
    evidence_type: EvidenceType = EvidenceType.AI_GENERATED_ANALYSIS
    is_authoritative: bool = False
    disclaimer: str = (
        "ADVISORY DUE-DILIGENCE FINDINGS: Findings from cross-entity compliance comparisons. "
        "Does NOT merge policies or alter entity configurations."
    )
    findings_count: int = 0
    findings: list[MNAFindingItem] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Unified Review Evidence Envelope
# ---------------------------------------------------------------------------

class UnifiedReviewEvidence(BaseModel):
    """Unified Human-in-the-Loop review evidence structure synthesizing all roadmap
    capabilities with explicit type labeling and anti-delegation guarantees.
    """
    review_id: str
    tenant_id: str
    candidate_rule_id: str
    candidate_rule_version: int | None = None
    candidate_policy_sha256: str | None = None
    source_clause_sha256: str | None = None
    assembled_at: dt.datetime = Field(default_factory=lambda: dt.datetime.now(dt.timezone.utc))
    approval_status: str = "PENDING"
    final_approval_authority: str = "Human Compliance Officer (Compliance_Officer role + MFA step-up required)"

    # Strongly typed and labeled evidence sections
    source_evidence: SourceEvidenceSection
    deterministic_evidence: DeterministicEvidenceSection
    arbitration_analysis: ArbitrationEvidenceSection
    precedent_evidence: PrecedentEvidenceSection
    digital_twin_simulation: DigitalTwinEvidenceSection
    zkp_evidence: ZKPEvidenceSection
    mna_findings: MNAEvidenceSection
