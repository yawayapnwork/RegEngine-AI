"""Orchestrates the explanation of a full `EvaluationResult` (potentially
several matched policies, each with its own violations) into a
`DecisionExplanationBundle`.

Two entry points, deliberately kept separate:

  - `explain_evaluation_result` (sync, deterministic-only): used on the
    hot `POST /v1/execution/transactions/evaluate` path
    (app.ledger.integration.log_evaluation) to produce the text stored in
    the audit ledger alongside the block hash. Never calls an LLM, never
    awaits I/O -- pure, fast string processing, matching the same
    "must not add latency to a synchronous decision" constraint the OPA
    engine itself operates under.
  - `explain_evaluation_result_full` (async, deterministic + LLM
    fallback): used by the on-demand `POST /v1/explainability/explain`
    endpoint for a compliance officer/auditor who wants an explanation
    even for a violation the deterministic parser couldn't structurally
    match.
"""
from __future__ import annotations

from app.config import Settings
from app.execution.models import EvaluationResult, PolicyOutcome
from app.explainability.llm_explainer import explain_violation_with_llm
from app.explainability.models import DecisionExplanationBundle, ExplanationSource, LegalExplanation
from app.explainability.nlg_deterministic import build_legal_explanation
from app.explainability.trace_parser import parse_violation


def explain_policy_outcome_deterministic(outcome: PolicyOutcome, regulator: str) -> list[LegalExplanation]:
    explanations = []
    for raw_text in outcome.violations:
        structured = parse_violation(
            raw_text,
            rule_id=outcome.rule_id,
            circular_number=outcome.circular_number,
            clause_number=outcome.clause_number,
            regulator=regulator,
        )
        if structured is not None:
            explanations.append(build_legal_explanation(structured))
        else:
            # Deterministic path could not structurally parse this one --
            # pass the raw compiler-generated text through verbatim rather
            # than silently dropping it. The async full path
            # (explain_evaluation_result_full) is what upgrades this to an
            # LLM-generated sentence on request.
            explanations.append(
                LegalExplanation(
                    rule_id=outcome.rule_id,
                    circular_number=outcome.circular_number,
                    clause_number=outcome.clause_number,
                    headline=f"Trade rejected: {raw_text}",
                    citation=f"Clause {outcome.clause_number or 'unscoped'} ({outcome.circular_number or 'circular unknown'})",
                    structured_violation=None,
                    source=ExplanationSource.UNPARSEABLE,
                    confidence=0.5,
                )
            )
    return explanations


def _overall_summary(result: EvaluationResult, explanation_count: int) -> str:
    if result.decision.value == "allow":
        return "Trade allowed: all applicable compliance policies were satisfied."
    if result.decision.value == "flagged":
        return f"Trade flagged for human review: {explanation_count} polic{'y' if explanation_count == 1 else 'ies'} returned an undefined/ambiguous result."
    plural = "violation" if explanation_count == 1 else "violations"
    return f"Trade rejected: {explanation_count} compliance {plural} found."


def explain_evaluation_result(result: EvaluationResult, regulator: str = "sebi") -> DecisionExplanationBundle:
    """Deterministic-only. See module docstring -- this is what
    app.ledger.integration.log_evaluation calls."""
    explanations: list[LegalExplanation] = []
    for outcome in result.matched_policies:
        if outcome.violations:
            explanations.extend(explain_policy_outcome_deterministic(outcome, regulator))

    return DecisionExplanationBundle(
        transaction_id=result.transaction_id,
        decision=result.decision.value,
        evaluated_at=result.evaluated_at,
        overall_summary=_overall_summary(result, len(explanations)),
        explanations=explanations,
    )


async def explain_evaluation_result_full(
    result: EvaluationResult,
    settings: Settings,
    regulator: str = "sebi",
) -> DecisionExplanationBundle:
    """Deterministic first, LLM fallback for anything the deterministic
    parser flagged as UNPARSEABLE. Used by the on-demand explanation API,
    never the hot evaluate path."""
    bundle = explain_evaluation_result(result, regulator)

    outcomes_by_rule_id = {o.rule_id: o for o in result.matched_policies}
    upgraded: list[LegalExplanation] = []
    for explanation in bundle.explanations:
        if explanation.source == ExplanationSource.UNPARSEABLE:
            outcome = outcomes_by_rule_id.get(explanation.rule_id)
            raw_text = explanation.headline.removeprefix("Trade rejected: ")
            if outcome is not None:
                upgraded.append(await explain_violation_with_llm(settings, outcome, raw_text, regulator))
                continue
        upgraded.append(explanation)

    bundle.explanations = upgraded
    return bundle
