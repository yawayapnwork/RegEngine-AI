"""Pre-compilation circular impact-diff API.

  POST /v1/diffing/analyze
      Accepts the already-extracted-and-audited clauses of a newly
      ingested circular (the output of app.agents.pipeline, BEFORE
      app.compiler.pipeline.compile_audited_rule runs) and returns a
      CircularImpactReport: what changed vs. the historical Master
      Circular index, classified, and mapped to internal services.

Restricted to Compliance_Officer / System_Admin -- this is a review
artifact for the humans deciding whether/how to approve compilation of
the new circular's rules, not something a Broker_API_Client ever calls.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.schemas import AuditedComplianceRule
from app.config import Settings, get_settings
from app.db.tenant_session import get_admin_db_session
from app.diffing.models import CircularImpactReport
from app.diffing.report_builder import analyze_circular_impact
from app.models import ClauseChunk
from app.security.dependencies import require_roles
from app.security.models import Role

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/diffing", tags=["Impact Diffing"])

_ALLOWED = require_roles(Role.COMPLIANCE_OFFICER, Role.SYSTEM_ADMIN)


class ClauseWithAuditedRule(BaseModel):
    chunk: ClauseChunk
    audited_rule: AuditedComplianceRule


class AnalyzeCircularImpactRequest(BaseModel):
    clauses: list[ClauseWithAuditedRule] = Field(..., description="Every audited clause from the newly ingested circular's extraction/audit pass.")
    supersedes_circular_number: str | None = Field(
        None,
        description="If a compliance officer has determined this circular explicitly supersedes an earlier one, "
        "pass its circular_number to enable the OBLIGATION_REMOVED coverage check.",
    )


@router.post("/analyze", response_model=CircularImpactReport, dependencies=[Depends(_ALLOWED)])
async def analyze_impact(
    request: AnalyzeCircularImpactRequest,
    settings: Settings = Depends(get_settings),
    db: AsyncSession = Depends(get_admin_db_session),
) -> CircularImpactReport:
    pairs = [(item.chunk, item.audited_rule) for item in request.clauses]
    return await analyze_circular_impact(
        pairs,
        settings=settings,
        db=db,
        supersedes_circular_number=request.supersedes_circular_number,
    )
