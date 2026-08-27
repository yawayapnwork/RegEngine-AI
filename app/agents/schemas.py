"""JSON schemas (as Pydantic models) exchanged between the Extraction Agent
and the Logic Auditor Agent.

Design principle: every extracted claim must carry a `verbatim_evidence`
quote lifted directly from the source clause. This is the primary
anti-hallucination guardrail — it gives the Logic Auditor Agent (and the
`QuoteVerificationTool`) something mechanically checkable instead of having
to trust the extraction agent's prose. A field with no supporting quote is,
by construction, treated as unverifiable and must be flagged.
"""
from __future__ import annotations

import datetime as dt
from enum import Enum

from pydantic import BaseModel, Field, field_validator


# --------------------------------------------------------------------------
# Extraction Agent output
# --------------------------------------------------------------------------


class ObligationType(str, Enum):
    MANDATORY = "mandatory"        # "shall", "must"
    PROHIBITED = "prohibited"      # "shall not", "no entity may"
    CONDITIONAL = "conditional"    # obligation only under a stated condition
    RECOMMENDED = "recommended"    # "may", "is advised to"


class ComparisonOperator(str, Enum):
    GTE = ">="
    GT = ">"
    LTE = "<="
    LT = "<"
    EQ = "=="
    RANGE = "range"


class TargetEntity(BaseModel):
    """A regulated entity type the clause applies to, normalized against the
    SEBI entity taxonomy but retaining the source wording for auditability.
    """

    raw_text: str = Field(..., description="Entity phrase exactly as it appears in the source clause.")
    normalized_entity: str | None = Field(
        None, description="Canonical entity name from the controlled taxonomy, if resolvable."
    )
    verbatim_evidence: str = Field(..., description="Exact quoted span from the source text naming this entity.")


class TriggerCondition(BaseModel):
    """An event, cadence, or precondition that activates the obligation."""

    description: str = Field(..., description="Normalized description of the trigger, e.g. 'Daily Collateral Reporting'.")
    frequency: str | None = Field(None, description="Cadence if stated, e.g. 'daily', 'T+1', 'quarterly'.")
    verbatim_evidence: str = Field(..., description="Exact quoted span from the source text describing this trigger.")


class NumericalThreshold(BaseModel):
    """A deterministic, machine-checkable numeric rule. Every field here must
    be traceable to an explicit number in the source text — never inferred,
    rounded, or defaulted by the model."""

    metric: str = Field(..., description="What is being measured, e.g. 'Upfront Margin'.")
    operator: ComparisonOperator
    value: float = Field(..., description="Primary numeric value, e.g. 20 for '>= 20%'.")
    value_upper: float | None = Field(None, description="Upper bound, only set when operator == RANGE.")
    unit: str = Field(..., description="Unit of the value, e.g. '%', 'INR crore', 'days'.")
    applies_to: str | None = Field(None, description="Entity or instrument the threshold applies to, if scoped.")
    verbatim_evidence: str = Field(..., description="Exact quoted span from the source text containing this number.")

    @field_validator("value_upper")
    @classmethod
    def _upper_requires_range(cls, v: float | None, info) -> float | None:
        if v is not None and info.data.get("operator") != ComparisonOperator.RANGE:
            raise ValueError("value_upper may only be set when operator is RANGE")
        return v


class QualitativeDirective(BaseModel):
    """A non-numeric, principle-based obligation (e.g. 'adequate internal
    controls') that cannot be reduced to a deterministic check. Kept
    separate from NumericalThreshold so downstream systems never coerce a
    qualitative standard into a false-precision number."""

    directive_text: str = Field(..., description="Normalized statement of the qualitative standard.")
    verbatim_evidence: str = Field(..., description="Exact quoted span from the source text.")


class ExtractedComplianceRule(BaseModel):
    """Full structured output of the Extraction Agent for a single clause chunk."""

    rule_id: str = Field(..., description="Stable ID, derived as f'{source_sha256}:{clause_number}'.")
    source_chunk_id: str
    source_sha256: str
    circular_number: str | None = None
    clause_number: str | None = None
    section_path: list[str] = Field(default_factory=list)

    target_entities: list[TargetEntity] = Field(default_factory=list)
    trigger_conditions: list[TriggerCondition] = Field(default_factory=list)
    deterministic_logic: list[NumericalThreshold] = Field(default_factory=list)
    qualitative_directives: list[QualitativeDirective] = Field(default_factory=list)

    obligation_type: ObligationType
    extraction_confidence: float = Field(..., ge=0.0, le=1.0)
    ambiguous_spans: list[str] = Field(
        default_factory=list,
        description="Verbatim spans the agent found ambiguous/uninterpretable and deliberately did NOT convert into a structured field.",
    )
    extraction_notes: str | None = Field(
        None, description="Free-text caveats, e.g. cross-references to other clauses needed for full context."
    )


# --------------------------------------------------------------------------
# Logic Auditor Agent output
# --------------------------------------------------------------------------


class FindingType(str, Enum):
    HALLUCINATED_THRESHOLD = "hallucinated_threshold"   # number not present in source
    HALLUCINATED_ENTITY = "hallucinated_entity"           # entity not present/implied in source
    INCORRECT_ENTITY_ASSIGNMENT = "incorrect_entity_assignment"  # entity present but wrongly scoped
    MISSING_CONTEXT = "missing_context"                    # source qualifier/exception dropped
    UNSUPPORTED_CLAIM = "unsupported_claim"               # verbatim_evidence doesn't actually support the field
    MISCLASSIFIED_OBLIGATION = "misclassified_obligation"  # e.g. "may" tagged as mandatory
    SCOPE_OVERREACH = "scope_overreach"                    # applied beyond the clause's stated scope
    UNIT_OR_VALUE_MISMATCH = "unit_or_value_mismatch"      # number matches but unit/operator wrong
    OK = "ok"                                              # field verified clean


class Severity(str, Enum):
    BLOCKER = "blocker"   # must not ship; invalidates the rule (e.g. fabricated threshold)
    MAJOR = "major"        # materially changes compliance meaning
    MINOR = "minor"        # imprecise but not misleading
    INFO = "info"          # stylistic / no compliance impact


class AuditFinding(BaseModel):
    finding_type: FindingType
    severity: Severity
    field_path: str = Field(..., description="JSON-pointer-style path into ExtractedComplianceRule, e.g. 'deterministic_logic[0].value'.")
    description: str = Field(..., description="Plain-language explanation of the discrepancy.")
    source_excerpt: str | None = Field(None, description="Quoted source text relevant to this finding, if any.")
    suggested_correction: str | None = Field(None, description="Auditor's proposed fix, if determinable without new inference.")


class AuditVerdict(str, Enum):
    APPROVED = "approved"                 # no blocker/major findings
    NEEDS_REVISION = "needs_revision"     # major findings; fixable by re-extraction
    REJECTED = "rejected"                 # blocker findings; extraction agent hallucinated


class ComplianceRuleAudit(BaseModel):
    """Full structured output of the Logic Auditor Agent for one ExtractedComplianceRule."""

    rule_id: str
    verdict: AuditVerdict
    fidelity_score: float = Field(..., ge=0.0, le=1.0, description="Auditor's own confidence that the extraction is fully faithful to source.")
    findings: list[AuditFinding] = Field(default_factory=list)
    verified_quote_count: int = Field(..., description="Number of verbatim_evidence quotes confirmed present in source via QuoteVerificationTool.")
    unverified_quote_count: int = Field(..., description="Number of verbatim_evidence quotes NOT found verbatim in source.")
    audited_at: dt.datetime = Field(default_factory=dt.datetime.utcnow)


class AuditedComplianceRule(BaseModel):
    """Final artifact persisted downstream: the extraction paired with its audit."""

    rule: ExtractedComplianceRule
    audit: ComplianceRuleAudit
    revision_round: int = Field(0, description="0 = first pass; increments each time extraction was sent back for revision.")
