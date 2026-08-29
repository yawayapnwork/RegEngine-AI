"""On-demand decision-explanation API.

  POST /v1/explainability/explain
      Given a full EvaluationResult (as returned by
      POST /v1/execution/transactions/evaluate, or reconstructed from an
      audit-ledger row for a historical transaction), returns a
      DecisionExplanationBundle -- deterministic explanations upgraded to
      an LLM-generated one wherever the deterministic parser couldn't
      structurally match a violation string.

      This is the ONLY place in the platform an LLM is invoked for
      explanation purposes -- the hot evaluate path
      (app.ledger.integration.log_evaluation) uses the deterministic-only
      path exclusively (app.explainability.explainer.explain_evaluation_result),
      so this endpoint's latency is irrelevant to trade-evaluation SLAs.

Restricted to Compliance_Officer / System_Admin: a Broker_API_Client
already receives its own EvaluationResult.reasons synchronously from the
evaluate call and has no separate need to hit this endpoint; this is a
compliance-officer/auditor review tool.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends

from app.config import Settings, get_settings
from app.execution.models import EvaluationResult
from app.explainability.explainer import explain_evaluation_result_full
from app.explainability.models import DecisionExplanationBundle
from app.security.dependencies import require_roles
from app.security.models import Role

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/explainability", tags=["Decision Explainability"])

_ALLOWED = require_roles(Role.COMPLIANCE_OFFICER, Role.SYSTEM_ADMIN)


@router.post("/explain", response_model=DecisionExplanationBundle, dependencies=[Depends(_ALLOWED)])
async def explain_decision(
    result: EvaluationResult,
    regulator: str = "sebi",
    settings: Settings = Depends(get_settings),
) -> DecisionExplanationBundle:
    return await explain_evaluation_result_full(result, settings, regulator)
