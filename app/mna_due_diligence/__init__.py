"""M&A Compliance Due-Diligence Agent (PRD Addendum v2 Section 8.5).

Autonomous compliance comparison agent for regulated market intermediaries.
"""
from app.mna_due_diligence.models import (
    EntityClauseSnapshot,
    EntityComplianceSnapshot,
    EntityHITLReviewSnapshot,
    EntityHistoricalViolationsSummary,
    EntityRuleSnapshot,
    MNAComparisonRequest,
    MNADifferenceType,
    MNADueDiligenceReport,
    MNAFinding,
    MNAFindingProvenance,
    MNAFindingSeverity,
    MNAJobProgress,
    MNAJobStatus,
)
from app.mna_due_diligence.auth import verify_dual_entity_authorization
from app.mna_due_diligence.comparison_engine import run_due_diligence_comparison
from app.mna_due_diligence.snapshot import build_entity_snapshot
from app.mna_due_diligence.tasks import MNAJobManager, run_mna_job_pipeline

__all__ = [
    "EntityClauseSnapshot",
    "EntityComplianceSnapshot",
    "EntityHITLReviewSnapshot",
    "EntityHistoricalViolationsSummary",
    "EntityRuleSnapshot",
    "MNAComparisonRequest",
    "MNADifferenceType",
    "MNADueDiligenceReport",
    "MNAFinding",
    "MNAFindingProvenance",
    "MNAFindingSeverity",
    "MNAJobManager",
    "MNAJobProgress",
    "MNAJobStatus",
    "build_entity_snapshot",
    "run_due_diligence_comparison",
    "run_mna_job_pipeline",
    "verify_dual_entity_authorization",
]
