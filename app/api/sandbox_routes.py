"""Sandboxed rule-testing API for market intermediaries.

Purpose
-------
Intermediaries (stockbrokers, AMCs) need to validate custom execution logic
and risk-overlay changes against *real historical SEBI circulars* before
promoting anything to production OPA bundles.  This router provides that
capability through four tightly-scoped endpoints:

  POST /v1/sandbox/evaluate
      Dry-run one or more transactions against the tenant's *currently active*
      compiled rules.  The session is fully tenant-scoped via RLS (the tenant
      can only read their own compiled_rules + shared SEBI baseline data),
      and the entire DB interaction is automatically rolled back — no sandbox
      artefact is ever persisted.

  POST /v1/sandbox/evaluate/with-overlay
      Same dry-run, but the caller supplies a *candidate risk overlay* (a dict
      of threshold overrides) that temporarily supersedes the tenant's stored
      ``risk_overlay``.  OPA is queried with the overlay merged into the input
      document so the intermediary can see how a threshold change would alter
      decisions without touching the production OPA data document.

  GET  /v1/sandbox/circulars
      Browse the historical SEBI circulars the requesting tenant has access
      to (their own + all shared ``is_shared=True`` ones).  Supports
      filtering by keyword, date range, and department.  Read-only.

  GET  /v1/sandbox/rules
      Enumerate the active compiled rules available in the tenant's sandbox
      partition, including the OPA package name and rule metadata.

Security posture
----------------
* Every endpoint requires a valid ``Broker_API_Client`` or
  ``Compliance_Officer`` bearer token — the sandbox is not public.
* ``Broker_API_Client`` tokens are restricted to their own ``tenant_id``
  partition via RLS; they physically cannot read another tenant's rules
  even through the sandbox.
* ``Compliance_Officer`` tokens get the admin GUC sentinel so they can
  inspect any tenant's sandbox partition for audit purposes.
* All DB sessions are ``SandboxSessionContext`` instances — they call
  ``ROLLBACK`` unconditionally, so no row can be written through the
  sandbox regardless of what the route handler does.
* The sandbox can be disabled globally via
  ``settings.sandbox_enabled = false``, which returns 503 on all four
  endpoints without requiring a code deploy.

OPA interaction
---------------
Sandbox evaluations hit the *live* OPA server (same sidecar as production)
against the *already-published* tenant policy bundle.  They are read-only
from OPA's perspective — no ``PUT /v1/policies`` or ``PUT /v1/data`` calls
are made.  The ``with-overlay`` endpoint injects the candidate overlay into
the OPA ``input`` document instead of pushing it as a ``/v1/data`` document,
so production OPA state is never mutated.
"""
from __future__ import annotations

import logging
import time
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.db.models import Circular, Clause, CompiledRule
from app.db.tenant_session import SandboxSessionContext
from app.execution.models import Decision, EvaluationResult, PolicyOutcome, TransactionPayload
from app.execution.opa_engine import OPAEngine, OPAEngineError
from app.execution.tenant_policy_registry import TenantPolicyRegistry
from app.security.dependencies import get_current_principal, require_roles
from app.security.models import Principal, Role

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/v1/sandbox",
    tags=["Sandbox"],
)

# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------


class SandboxEvaluateRequest(BaseModel):
    """Dry-run evaluation request body.

    ``transactions`` is evaluated against the tenant's currently active
    compiled rules (the same set the production evaluator uses) but the
    DB session is always rolled back and no HITL case / ledger entry is
    created.
    """

    transactions: list[TransactionPayload] = Field(
        ...,
        min_length=1,
        description="One or more transactions to evaluate.  Capped at settings.sandbox_max_transactions.",
    )


class SandboxEvaluateWithOverlayRequest(BaseModel):
    """Dry-run evaluation with a candidate risk-overlay override.

    ``candidate_overlay`` is merged into the OPA ``input`` document under
    the key ``overlay``, making threshold values available to Rego rules as
    ``input.overlay.<field>``.  This does NOT push the overlay to OPA data —
    the production ``data.tenants.<tenant_id>.overlay`` document is unchanged.
    """

    transactions: list[TransactionPayload] = Field(..., min_length=1)
    candidate_overlay: dict[str, Any] = Field(
        ...,
        description=(
            "Candidate threshold overrides, e.g. "
            '{"upfront_margin_pct": 22.5, "exposure_limit_cr": 50}.  '
            "Fields present here override the tenant's stored risk_overlay "
            "for this evaluation only."
        ),
    )


class SandboxPolicyOutcome(BaseModel):
    """Per-policy result enriched with sandbox-specific metadata."""

    rule_id: str
    package: str
    allow: bool | None
    violations: list[str] = Field(default_factory=list)
    circular_number: str | None = None
    clause_number: str | None = None
    # Indicates whether this policy outcome was influenced by the
    # candidate_overlay (only set on /evaluate/with-overlay responses).
    overlay_applied: bool = False


class SandboxEvaluationResult(BaseModel):
    """Dry-run result for a single transaction."""

    sandbox_run_id: str = Field(description="Unique id for this sandbox execution batch (same for all transactions in one request).")
    transaction_id: str
    decision: Decision
    matched_policies: list[SandboxPolicyOutcome] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)
    # Production evaluation would create a HITL case on FLAGGED; sandbox
    # surfaces the reason but creates nothing.
    would_trigger_hitl: bool = False
    latency_ms: float | None = None
    note: str = "Sandbox dry-run: no ledger entry written, no HITL case created."


class SandboxBatchResult(BaseModel):
    sandbox_run_id: str
    tenant_id: str
    total: int
    allowed: int
    denied: int
    flagged: int
    results: list[SandboxEvaluationResult]
    evaluated_at: str  # ISO-8601


class CircularSummary(BaseModel):
    """Minimal read-only view of a historical circular, safe to return to
    the sandbox caller (no raw text or digest is included)."""

    id: int
    circular_number: str
    title: str | None
    issue_date: str | None  # ISO-8601 date string
    department: str | None
    source_url: str | None
    is_shared: bool
    tenant_id: str
    clause_count: int = 0


class CompiledRuleSummary(BaseModel):
    """Minimal view of an active compiled rule available in the sandbox."""

    id: int
    rule_id: str
    rule_version: int
    opa_package_name: str | None
    has_rego: bool
    has_jsonlogic: bool
    hitl_status: str
    circular_number: str | None = None  # resolved via clause -> circular join
    clause_number: str | None = None


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _sandbox_guard(settings: Settings) -> None:
    """Raise 503 if the sandbox is globally disabled."""
    if not settings.sandbox_enabled:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "The sandbox environment is currently disabled by the system administrator. "
                "Contact your compliance team for assistance."
            ),
        )


def _resolve_tenant_id(principal: Principal) -> str:
    """Return the effective tenant_id for sandbox access:
    - Broker_API_Client  -> their own tenant_id (enforced by JWT claim)
    - Compliance_Officer / System_Admin -> must pass tenant_id explicitly
      (handled by callers; this helper just reads from the principal).

    Raises 403 if a Broker_API_Client token somehow lacks tenant_id.
    """
    if principal.tenant_id:
        return principal.tenant_id
    if principal.is_admin() or Role.COMPLIANCE_OFFICER in principal.roles:
        # Human roles don't carry a tenant_id; they specify it as a query param.
        # This path is reached only when _admin_tenant_override is used.
        return "__admin__"
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Broker_API_Client token is missing tenant_id claim.",
    )


def _reduce_outcomes(outcomes: list[SandboxPolicyOutcome]) -> tuple[Decision, list[str]]:
    """Same most-restrictive-wins reduction as app.execution.evaluator.Evaluator._reduce."""
    violations = [msg for o in outcomes for msg in o.violations]
    if violations:
        return Decision.DENY, violations
    undefined = [o.rule_id for o in outcomes if o.allow is None]
    if undefined:
        return Decision.FLAGGED, [
            f"Policy(ies) {undefined} returned an undefined result — insufficient or malformed facts."
        ]
    return Decision.ALLOW, []


async def _fetch_active_rules(
    db: AsyncSession, tenant_id: str
) -> list[CompiledRule]:
    """Return all is_active=True compiled rules visible to the tenant.
    RLS on the session already filters to tenant_id + shared baseline;
    we add is_active=True so the sandbox only evaluates what production
    would evaluate.
    """
    stmt = select(CompiledRule).where(CompiledRule.is_active == True)  # noqa: E712
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def _evaluate_one(
    transaction: TransactionPayload,
    rules: list[CompiledRule],
    opa: OPAEngine,
    input_overlay: dict[str, Any] | None,
    sandbox_run_id: str,
) -> SandboxEvaluationResult:
    """Evaluate one transaction against the provided rule list via OPA."""
    started = time.perf_counter()

    input_doc: dict[str, Any] = {
        "entity_type": transaction.entity_type,
        "facts": transaction.facts,
    }
    if input_overlay:
        input_doc["overlay"] = input_overlay

    applicable = [
        r for r in rules
        if r.opa_package_name  # must have a Rego package to evaluate
    ]

    outcomes: list[SandboxPolicyOutcome] = []
    for rule in applicable:
        package = rule.opa_package_name
        try:
            result = await opa.evaluate(package, input_doc)
        except OPAEngineError as exc:
            logger.warning(
                "Sandbox OPA evaluation failed for rule_id=%s package=%s: %s",
                rule.rule_id, package, exc,
            )
            outcomes.append(
                SandboxPolicyOutcome(
                    rule_id=rule.rule_id,
                    package=package,
                    allow=None,
                    overlay_applied=input_overlay is not None,
                )
            )
            continue

        if result is None:
            outcomes.append(
                SandboxPolicyOutcome(
                    rule_id=rule.rule_id,
                    package=package,
                    allow=None,
                    overlay_applied=input_overlay is not None,
                )
            )
        else:
            outcomes.append(
                SandboxPolicyOutcome(
                    rule_id=rule.rule_id,
                    package=package,
                    allow=bool(result.get("allow", False)),
                    violations=list(result.get("violations", []) or []),
                    circular_number=result.get("circular_number"),
                    clause_number=result.get("clause_number"),
                    overlay_applied=input_overlay is not None,
                )
            )

    if not applicable:
        decision, reasons = Decision.ALLOW, ["No active compiled rules apply to this entity_type in the sandbox."]
    else:
        decision, reasons = _reduce_outcomes(outcomes)

    return SandboxEvaluationResult(
        sandbox_run_id=sandbox_run_id,
        transaction_id=transaction.transaction_id,
        decision=decision,
        matched_policies=outcomes,
        reasons=reasons,
        would_trigger_hitl=(decision == Decision.FLAGGED),
        latency_ms=(time.perf_counter() - started) * 1000,
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post(
    "/evaluate",
    response_model=SandboxBatchResult,
    summary="Dry-run transactions against active compiled rules",
    description=(
        "Evaluates one or more transactions against the tenant's currently active OPA policy bundle "
        "in a fully isolated, read-only session.  No HITL cases, ledger entries, or webhook events "
        "are created.  The session is automatically rolled back after evaluation."
    ),
)
async def sandbox_evaluate(
    body: SandboxEvaluateRequest,
    request: Request,
    settings: Settings = Depends(get_settings),
    principal: Principal = Depends(
        require_roles(Role.BROKER_API_CLIENT, Role.COMPLIANCE_OFFICER, Role.SYSTEM_ADMIN)
    ),
) -> SandboxBatchResult:
    _sandbox_guard(settings)

    tenant_id = _resolve_tenant_id(principal)
    if len(body.transactions) > settings.sandbox_max_transactions:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Exceeded sandbox_max_transactions limit ({settings.sandbox_max_transactions}).",
        )

    sandbox_run_id = str(uuid.uuid4())
    opa = OPAEngine(
        base_url=settings.opa_server_url,
        timeout_seconds=settings.sandbox_opa_timeout_seconds,
    )

    results: list[SandboxEvaluationResult] = []
    async with SandboxSessionContext(tenant_id) as db:
        rules = await _fetch_active_rules(db, tenant_id)
        for txn in body.transactions:
            result = await _evaluate_one(
                transaction=txn,
                rules=rules,
                opa=opa,
                input_overlay=None,
                sandbox_run_id=sandbox_run_id,
            )
            results.append(result)
        # SandboxSessionContext always ROLLBACKs on __aexit__

    import datetime as dt
    counters = {"allowed": 0, "denied": 0, "flagged": 0}
    for r in results:
        counters[r.decision.value + "d" if r.decision != Decision.FLAGGED else "flagged"] = (
            counters.get(r.decision.value + "d" if r.decision != Decision.FLAGGED else "flagged", 0) + 1
        )
    allowed = sum(1 for r in results if r.decision == Decision.ALLOW)
    denied = sum(1 for r in results if r.decision == Decision.DENY)
    flagged = sum(1 for r in results if r.decision == Decision.FLAGGED)

    logger.info(
        "Sandbox evaluate: tenant=%s run_id=%s total=%d allow=%d deny=%d flagged=%d",
        tenant_id, sandbox_run_id, len(results), allowed, denied, flagged,
    )

    return SandboxBatchResult(
        sandbox_run_id=sandbox_run_id,
        tenant_id=tenant_id,
        total=len(results),
        allowed=allowed,
        denied=denied,
        flagged=flagged,
        results=results,
        evaluated_at=dt.datetime.now(dt.timezone.utc).isoformat(),
    )


@router.post(
    "/evaluate/with-overlay",
    response_model=SandboxBatchResult,
    summary="Dry-run transactions with a candidate risk-overlay",
    description=(
        "Same as /evaluate but merges a caller-supplied ``candidate_overlay`` dict into the OPA "
        "input document under the key ``input.overlay``.  Production OPA state is never mutated. "
        "Use this to preview how threshold changes (e.g. raising the upfront margin floor) would "
        "alter compliance decisions before submitting the overlay for production approval."
    ),
)
async def sandbox_evaluate_with_overlay(
    body: SandboxEvaluateWithOverlayRequest,
    request: Request,
    settings: Settings = Depends(get_settings),
    principal: Principal = Depends(
        require_roles(Role.BROKER_API_CLIENT, Role.COMPLIANCE_OFFICER, Role.SYSTEM_ADMIN)
    ),
) -> SandboxBatchResult:
    _sandbox_guard(settings)

    tenant_id = _resolve_tenant_id(principal)
    if len(body.transactions) > settings.sandbox_max_transactions:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Exceeded sandbox_max_transactions limit ({settings.sandbox_max_transactions}).",
        )
    if not body.candidate_overlay:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="candidate_overlay must not be empty; use /evaluate for a plain dry-run.",
        )

    sandbox_run_id = str(uuid.uuid4())
    opa = OPAEngine(
        base_url=settings.opa_server_url,
        timeout_seconds=settings.sandbox_opa_timeout_seconds,
    )

    results: list[SandboxEvaluationResult] = []
    async with SandboxSessionContext(tenant_id) as db:
        rules = await _fetch_active_rules(db, tenant_id)

        # Fetch the tenant's stored risk_overlay from the DB so we can
        # merge the candidate on top of it (candidate wins on conflicts).
        from sqlalchemy import select as sa_select
        from app.db.models import Tenant as TenantModel
        tenant_row = await db.get(TenantModel, tenant_id)
        base_overlay: dict[str, Any] = (tenant_row.risk_overlay if tenant_row else {}) or {}
        merged_overlay = {**base_overlay, **body.candidate_overlay}

        for txn in body.transactions:
            result = await _evaluate_one(
                transaction=txn,
                rules=rules,
                opa=opa,
                input_overlay=merged_overlay,
                sandbox_run_id=sandbox_run_id,
            )
            results.append(result)

    allowed = sum(1 for r in results if r.decision == Decision.ALLOW)
    denied = sum(1 for r in results if r.decision == Decision.DENY)
    flagged = sum(1 for r in results if r.decision == Decision.FLAGGED)

    logger.info(
        "Sandbox evaluate-with-overlay: tenant=%s run_id=%s overlay_keys=%s "
        "total=%d allow=%d deny=%d flagged=%d",
        tenant_id, sandbox_run_id, list(body.candidate_overlay.keys()),
        len(results), allowed, denied, flagged,
    )

    import datetime as dt
    return SandboxBatchResult(
        sandbox_run_id=sandbox_run_id,
        tenant_id=tenant_id,
        total=len(results),
        allowed=allowed,
        denied=denied,
        flagged=flagged,
        results=results,
        evaluated_at=dt.datetime.now(dt.timezone.utc).isoformat(),
    )


@router.get(
    "/circulars",
    response_model=list[CircularSummary],
    summary="Browse historical SEBI circulars available in the sandbox",
    description=(
        "Returns the circulars the requesting tenant has access to: their own tenant-specific "
        "circulars plus all shared SEBI master circulars (``is_shared=True``).  "
        "RLS enforces this automatically — no application-level tenant filter is added.  "
        "Supports optional keyword, date-range, and department filtering."
    ),
)
async def sandbox_list_circulars(
    keyword: str | None = Query(
        None,
        description="Case-insensitive substring match on circular_number or title.",
        max_length=200,
    ),
    date_from: str | None = Query(
        None,
        description="ISO-8601 date (YYYY-MM-DD). Filter circulars issued on or after this date.",
        pattern=r"^\d{4}-\d{2}-\d{2}$",
    ),
    date_to: str | None = Query(
        None,
        description="ISO-8601 date (YYYY-MM-DD). Filter circulars issued on or before this date.",
        pattern=r"^\d{4}-\d{2}-\d{2}$",
    ),
    department: str | None = Query(
        None,
        description="Case-insensitive match on the circular's department field.",
        max_length=128,
    ),
    limit: int = Query(
        default=50,
        ge=1,
        le=200,
        description="Maximum number of circulars to return.",
    ),
    offset: int = Query(default=0, ge=0, description="Pagination offset."),
    settings: Settings = Depends(get_settings),
    principal: Principal = Depends(
        require_roles(Role.BROKER_API_CLIENT, Role.COMPLIANCE_OFFICER, Role.SYSTEM_ADMIN)
    ),
) -> list[CircularSummary]:
    _sandbox_guard(settings)

    import datetime as dt
    from sqlalchemy import func as sa_func, and_, or_

    tenant_id = _resolve_tenant_id(principal)
    effective_limit = min(limit, settings.sandbox_max_circulars)

    async with SandboxSessionContext(tenant_id) as db:
        stmt = select(Circular)

        conditions = []
        if keyword:
            kw = f"%{keyword}%"
            conditions.append(
                or_(
                    Circular.circular_number.ilike(kw),
                    Circular.title.ilike(kw),
                )
            )
        if date_from:
            conditions.append(Circular.issue_date >= dt.date.fromisoformat(date_from))
        if date_to:
            conditions.append(Circular.issue_date <= dt.date.fromisoformat(date_to))
        if department:
            conditions.append(Circular.department.ilike(f"%{department}%"))

        if conditions:
            stmt = stmt.where(and_(*conditions))

        stmt = stmt.order_by(Circular.issue_date.desc().nulls_last()).offset(offset).limit(effective_limit)

        rows = list((await db.execute(stmt)).scalars().all())

        # Clause counts: one query for all circular ids to avoid N+1.
        circular_ids = [c.id for c in rows]
        clause_counts: dict[int, int] = {}
        if circular_ids:
            count_stmt = (
                select(Clause.circular_id, sa_func.count(Clause.id).label("cnt"))
                .where(Clause.circular_id.in_(circular_ids))
                .group_by(Clause.circular_id)
            )
            for cid, cnt in (await db.execute(count_stmt)).all():
                clause_counts[cid] = cnt

    return [
        CircularSummary(
            id=c.id,
            circular_number=c.circular_number,
            title=c.title,
            issue_date=c.issue_date.isoformat() if c.issue_date else None,
            department=c.department,
            source_url=c.source_url,
            is_shared=c.is_shared,
            tenant_id=c.tenant_id,
            clause_count=clause_counts.get(c.id, 0),
        )
        for c in rows
    ]


@router.get(
    "/rules",
    response_model=list[CompiledRuleSummary],
    summary="List active compiled rules available in the sandbox",
    description=(
        "Returns all ``is_active=True`` compiled rules visible to the requesting tenant.  "
        "RLS scopes this to the tenant's own rules plus any shared SEBI baseline rules.  "
        "The returned ``opa_package_name`` is what you would use in a custom Rego evaluation "
        "query against the sandbox."
    ),
)
async def sandbox_list_rules(
    entity_type: str | None = Query(
        None,
        description="Filter by the entity type the rule targets (e.g. 'Stockbroker').",
        max_length=128,
    ),
    circular_number: str | None = Query(
        None,
        description="Filter by the originating SEBI circular number.",
        max_length=128,
    ),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    settings: Settings = Depends(get_settings),
    principal: Principal = Depends(
        require_roles(Role.BROKER_API_CLIENT, Role.COMPLIANCE_OFFICER, Role.SYSTEM_ADMIN)
    ),
) -> list[CompiledRuleSummary]:
    _sandbox_guard(settings)

    from sqlalchemy.orm import selectinload

    tenant_id = _resolve_tenant_id(principal)

    async with SandboxSessionContext(tenant_id) as db:
        stmt = (
            select(CompiledRule)
            .where(CompiledRule.is_active == True)  # noqa: E712
            .options(
                selectinload(CompiledRule.clause).selectinload(Clause.circular)
            )
            .order_by(CompiledRule.created_at.desc())
            .offset(offset)
            .limit(limit)
        )
        rules = list((await db.execute(stmt)).scalars().all())

        # Apply post-fetch filters that require joined data.
        filtered: list[CompiledRule] = []
        for rule in rules:
            circ = rule.clause.circular if rule.clause else None
            if circular_number and (circ is None or circ.circular_number != circular_number):
                continue
            filtered.append(rule)

    return [
        CompiledRuleSummary(
            id=r.id,
            rule_id=r.rule_id,
            rule_version=r.rule_version,
            opa_package_name=r.opa_package_name,
            has_rego=r.rego_policy is not None,
            has_jsonlogic=r.jsonlogic_ast is not None,
            hitl_status=r.hitl_status,
            circular_number=(r.clause.circular.circular_number if r.clause and r.clause.circular else None),
            clause_number=(r.clause.clause_number if r.clause else None),
        )
        for r in filtered
    ]


@router.get(
    "/rules/{rule_id}",
    response_model=CompiledRuleSummary,
    summary="Fetch a single compiled rule by rule_id",
    description=(
        "Returns the active compiled rule with the given ``rule_id``.  "
        "Returns 404 if the rule does not exist or is not visible to the tenant."
    ),
)
async def sandbox_get_rule(
    rule_id: str,
    settings: Settings = Depends(get_settings),
    principal: Principal = Depends(
        require_roles(Role.BROKER_API_CLIENT, Role.COMPLIANCE_OFFICER, Role.SYSTEM_ADMIN)
    ),
) -> CompiledRuleSummary:
    _sandbox_guard(settings)

    from sqlalchemy.orm import selectinload

    tenant_id = _resolve_tenant_id(principal)

    async with SandboxSessionContext(tenant_id) as db:
        stmt = (
            select(CompiledRule)
            .where(CompiledRule.rule_id == rule_id, CompiledRule.is_active == True)  # noqa: E712
            .options(
                selectinload(CompiledRule.clause).selectinload(Clause.circular)
            )
            .limit(1)
        )
        rule = (await db.execute(stmt)).scalars().first()

    if rule is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No active compiled rule with rule_id='{rule_id}' found for this tenant.",
        )

    circ = rule.clause.circular if rule.clause else None
    return CompiledRuleSummary(
        id=rule.id,
        rule_id=rule.rule_id,
        rule_version=rule.rule_version,
        opa_package_name=rule.opa_package_name,
        has_rego=rule.rego_policy is not None,
        has_jsonlogic=rule.jsonlogic_ast is not None,
        hitl_status=rule.hitl_status,
        circular_number=circ.circular_number if circ else None,
        clause_number=rule.clause.clause_number if rule.clause else None,
    )
