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

Historical transaction backtesting engine & Real-Data Rule-Impact Preview:
replays historical ledger transactions against candidate policy versions offline.
"""
from app.backtest.candidate_evaluator import (
    CandidateEvaluator,
    JsonLogicCandidateEvaluator,
    OpaCandidateEvaluator,
)
from app.backtest.jsonlogic_evaluator import (
    MissingFactError,
    UnsupportedJsonLogicNodeError,
    evaluate_jsonlogic,
)
from app.backtest.models import (
    ADVISORY_DISCLAIMER,
    AggregateFinancialImpact,
    BacktestOutcome,
    BacktestRun,
    BacktestRunRequest,
    BacktestStatus,
    BacktestSummary,
    BrokerImpactBreakdown,
    DeltaChangeType,
    HistoricalTransaction,
    PreviewDatasetScope,
    PreviewRunRequest,
    RepresentativeExample,
    RuleImpactPreviewReport,
)
from app.backtest.orchestrator import (
    compute_policy_hash,
    run_backtest,
    run_rule_impact_preview,
)
from app.backtest.replay_engine import (
    compute_dataset_snapshot_hash,
    extract_financial_value,
    fetch_historical_transactions,
    replay_all,
    replay_transaction,
)
from app.backtest.reporting import (
    build_aggregate_financial_impact,
    build_outcomes,
    build_representative_examples,
    build_rule_impact_preview_report,
    build_summary,
    compute_preview_result_digest,
    mask_transaction_id,
    outcomes_to_dataframe,
)
from app.backtest.tasks import (
    cancel_preview,
    get_outcomes_page,
    get_preview_report,
    get_run,
    is_preview_cancelled,
    run_backtest_task,
    run_rule_impact_preview_task,
    save_outcomes,
    save_preview_report,
    save_run,
)

__all__ = [
    "ADVISORY_DISCLAIMER",
    "CandidateEvaluator",
    "JsonLogicCandidateEvaluator",
    "OpaCandidateEvaluator",
    "evaluate_jsonlogic",
    "MissingFactError",
    "UnsupportedJsonLogicNodeError",
    "BacktestStatus",
    "DeltaChangeType",
    "HistoricalTransaction",
    "BacktestOutcome",
    "BrokerImpactBreakdown",
    "BacktestSummary",
    "BacktestRunRequest",
    "BacktestRun",
    "PreviewDatasetScope",
    "RepresentativeExample",
    "AggregateFinancialImpact",
    "RuleImpactPreviewReport",
    "PreviewRunRequest",
    "run_backtest",
    "run_rule_impact_preview",
    "compute_policy_hash",
    "fetch_historical_transactions",
    "replay_transaction",
    "replay_all",
    "compute_dataset_snapshot_hash",
    "extract_financial_value",
    "build_outcomes",
    "outcomes_to_dataframe",
    "build_summary",
    "build_aggregate_financial_impact",
    "build_representative_examples",
    "compute_preview_result_digest",
    "build_rule_impact_preview_report",
    "mask_transaction_id",
    "save_run",
    "get_run",
    "save_outcomes",
    "get_outcomes_page",
    "run_backtest_task",
    "save_preview_report",
    "get_preview_report",
    "cancel_preview",
    "is_preview_cancelled",
    "run_rule_impact_preview_task",
]

