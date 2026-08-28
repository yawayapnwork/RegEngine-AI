"""Telemetry aggregation pipeline for compliance analytics.

Reads from two sources:
  1. ``compliance_audit_ledger`` (the append-only, hash-chained ledger) via
     the existing ``get_ledger_engine()`` async engine.
  2. ``hitl_reviews`` and ``tenants`` ORM tables via the admin DB session
     (bypasses RLS so cross-tenant reports work without changing the GUC
     per-query).

All heavy-lifting is done in Pandas after a minimal SQL projection, keeping
the queries simple and predictable (no complex server-side aggregations that
would make the query plan opaque to an auditor reviewing the code).

The ``ComplianceAggregator`` class is stateless and short-lived — instantiate
it per-request or per-Celery task; it holds no cache.
"""
from __future__ import annotations

import datetime as dt
import logging
from typing import Any

import pandas as pd
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.analytics.models import (
    AggregatedReport,
    AuditTrailEntry,
    BrokerStats,
    ChainProofSummary,
    Granularity,
    PeriodBucket,
    ReportPeriod,
    RuleViolationSummary,
)
from app.db.models import HITLReview, Tenant
from app.ledger.models import compliance_audit_ledger, EvaluationOutcome
from app.ledger.verifier import verify_chain

logger = logging.getLogger(__name__)

_PASS = EvaluationOutcome.PASS.value
_FAIL = EvaluationOutcome.FAIL.value
_HITL = EvaluationOutcome.HITL_REVIEW.value


# ---------------------------------------------------------------------------
# SQL helpers
# ---------------------------------------------------------------------------

_LEDGER_COLS = [
    compliance_audit_ledger.c.sequence_num,
    compliance_audit_ledger.c.broker_id,
    compliance_audit_ledger.c.transaction_id,
    compliance_audit_ledger.c.evaluated_at,
    compliance_audit_ledger.c.circular_id,
    compliance_audit_ledger.c.section_reference,
    compliance_audit_ledger.c.rule_id,
    compliance_audit_ledger.c.evaluation_result,
    compliance_audit_ledger.c.hitl_review_id,
    compliance_audit_ledger.c.payload_digest,
    compliance_audit_ledger.c.current_hash,
]


async def _fetch_ledger_rows(
    engine: AsyncEngine,
    start: dt.datetime,
    end: dt.datetime,
    tenant_id: str | None,
) -> pd.DataFrame:
    """Pull raw ledger rows for the window into a DataFrame.

    ``tenant_id`` is applied as a WHERE filter when provided (single-tenant
    report).  For cross-tenant reports it is ``None`` and all rows are
    returned — the ledger engine connects as ``regengine_ledger_writer``
    which has SELECT on the whole table (no RLS on the ledger; its
    protection model is the hash chain, not row-level visibility controls).
    """
    stmt = (
        select(*_LEDGER_COLS)
        .where(
            compliance_audit_ledger.c.evaluated_at >= start,
            compliance_audit_ledger.c.evaluated_at <= end,
        )
        .order_by(compliance_audit_ledger.c.sequence_num.asc())
    )

    if tenant_id:
        # The ledger's broker_id == tenant_id for machine-originated evaluations.
        # We filter by broker_id here; the tenant_id FK column (added in
        # migration 0003) is also available but nullable for legacy rows.
        stmt = stmt.where(compliance_audit_ledger.c.broker_id == tenant_id)

    async with engine.connect() as conn:
        result = await conn.execute(stmt)
        rows = result.mappings().all()

    if not rows:
        return pd.DataFrame(
            columns=[
                "sequence_num", "broker_id", "transaction_id", "evaluated_at",
                "circular_id", "section_reference", "rule_id", "evaluation_result",
                "hitl_review_id", "payload_digest", "current_hash",
            ]
        )

    df = pd.DataFrame([dict(r) for r in rows])
    df["evaluated_at"] = pd.to_datetime(df["evaluated_at"], utc=True)
    return df


async def _fetch_hitl_stats(
    db: AsyncSession,
    start: dt.datetime,
    end: dt.datetime,
    tenant_id: str | None,
) -> pd.DataFrame:
    """Pull HITL review status aggregates from the ORM DB."""
    stmt = select(
        HITLReview.tenant_id,
        HITLReview.status,
        HITLReview.flagged_at,
    ).where(
        HITLReview.flagged_at >= start,
        HITLReview.flagged_at <= end,
    )
    if tenant_id:
        stmt = stmt.where(HITLReview.tenant_id == tenant_id)

    result = await db.execute(stmt)
    rows = result.all()
    if not rows:
        return pd.DataFrame(columns=["tenant_id", "status", "flagged_at"])
    return pd.DataFrame(rows, columns=["tenant_id", "status", "flagged_at"])


async def _fetch_tenants(db: AsyncSession) -> dict[str, dict[str, str]]:
    """Return {tenant_id: {display_name, tenant_type}} lookup."""
    result = await db.execute(
        select(Tenant.tenant_id, Tenant.display_name, Tenant.tenant_type)
    )
    return {
        row.tenant_id: {"display_name": row.display_name, "tenant_type": row.tenant_type}
        for row in result.all()
    }


# ---------------------------------------------------------------------------
# Period labelling helpers
# ---------------------------------------------------------------------------

def _monthly_label(ts: pd.Timestamp) -> str:
    return ts.strftime("%Y-%m")


def _quarterly_label(ts: pd.Timestamp) -> str:
    q = (ts.month - 1) // 3 + 1
    return f"{ts.year}-Q{q}"


def _add_period_col(df: pd.DataFrame, granularity: Granularity) -> pd.DataFrame:
    if granularity == Granularity.MONTHLY:
        df["_period"] = df["evaluated_at"].dt.to_period("M").astype(str)
    else:
        df["_period"] = df["evaluated_at"].apply(
            lambda ts: _quarterly_label(ts) if not pd.isna(ts) else None
        )
    return df


# ---------------------------------------------------------------------------
# Core aggregator
# ---------------------------------------------------------------------------

class ComplianceAggregator:
    """Builds ``AggregatedReport`` and ``AuditTrailReport`` objects from the
    compliance ledger and HITL reviews.

    Parameters
    ----------
    ledger_engine:
        The ``AsyncEngine`` returned by ``app.ledger.db.get_ledger_engine()``.
    db_session:
        An ``AsyncSession`` from ``get_admin_db_session()`` (bypasses RLS so
        cross-tenant joins work).  The caller owns the session lifecycle.
    """

    def __init__(self, ledger_engine: AsyncEngine, db_session: AsyncSession) -> None:
        self._ledger = ledger_engine
        self._db = db_session

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    async def build_aggregated_report(
        self,
        period: ReportPeriod,
        report_id: str,
        generated_by: str,
        tenant_id: str | None = None,
        verify_chain_integrity: bool = True,
    ) -> AggregatedReport:
        """Build the full ``AggregatedReport`` for the given period.

        Parameters
        ----------
        period:
            Date window and granularity.
        report_id:
            UUID for this report run (caller supplies it for idempotency).
        generated_by:
            Principal subject who triggered the report.
        tenant_id:
            ``None`` for a cross-tenant executive summary; a specific
            tenant_id for a single-broker view.
        verify_chain_integrity:
            Whether to run the hash-chain verifier.  Set ``False`` for
            quick previews where latency matters more than proof.
        """
        start = period.start_datetime
        end = period.end_datetime

        # 1. Pull raw data
        df = await _fetch_ledger_rows(self._ledger, start, end, tenant_id)
        hitl_df = await _fetch_hitl_stats(self._db, start, end, tenant_id)
        tenant_lookup = await _fetch_tenants(self._db)

        # 2. Headline KPIs
        kpis = self._compute_kpis(df)

        # 3. Time series
        time_series = self._build_time_series(df, period.granularity)

        # 4. Broker breakdown
        broker_stats = self._build_broker_stats(df, hitl_df, tenant_lookup)

        # 5. Top rule violations
        top_violations = self._build_violation_summary(df)

        # 6. Chain proof
        chain_proof: ChainProofSummary | None = None
        if verify_chain_integrity:
            chain_proof = await self._build_chain_proof(start, end)

        report = AggregatedReport(
            report_id=report_id,
            generated_by=generated_by,
            period=period,
            tenant_scope=tenant_id or "all",
            **kpis,
            time_series=time_series,
            broker_stats=broker_stats,
            top_violations=top_violations,
            chain_proof=chain_proof,
        )
        logger.info(
            "AggregatedReport built: report_id=%s period=%s total_txn=%d",
            report_id, period.label(), kpis["total_transactions"],
        )
        return report

    async def build_audit_trail(
        self,
        period: ReportPeriod,
        report_id: str,
        generated_by: str,
        tenant_id: str | None = None,
        page: int = 1,
        page_size: int = 500,
    ) -> tuple[list[AuditTrailEntry], ChainProofSummary, int]:
        """Return a paginated ledger extract and chain proof.

        Returns
        -------
        (entries, chain_proof, total_count)
        """
        start = period.start_datetime
        end = period.end_datetime

        df = await _fetch_ledger_rows(self._ledger, start, end, tenant_id)
        total = len(df)
        page_df = df.iloc[(page - 1) * page_size : page * page_size]

        entries = [
            AuditTrailEntry(
                sequence_num=int(row["sequence_num"]),
                broker_id=str(row["broker_id"]),
                transaction_id=str(row["transaction_id"]),
                evaluated_at=row["evaluated_at"].to_pydatetime(),
                circular_id=str(row["circular_id"]),
                section_reference=str(row["section_reference"]),
                rule_id=str(row["rule_id"]),
                evaluation_result=str(row["evaluation_result"]),
                hitl_review_id=row.get("hitl_review_id"),
                payload_digest=str(row["payload_digest"]),
                current_hash=str(row["current_hash"]),
            )
            for _, row in page_df.iterrows()
        ]

        chain_proof = await self._build_chain_proof(start, end)
        return entries, chain_proof, total

    # ------------------------------------------------------------------
    # Private aggregation helpers
    # ------------------------------------------------------------------

    def _compute_kpis(self, df: pd.DataFrame) -> dict[str, Any]:
        """Compute headline integer and rate KPIs from the raw DataFrame."""
        total = len(df)
        if total == 0:
            return {
                "total_transactions": 0,
                "total_passed": 0,
                "total_failed": 0,
                "total_hitl_review": 0,
                "overall_pass_rate_pct": 0.0,
                "overall_fail_rate_pct": 0.0,
                "overall_hitl_rate_pct": 0.0,
                "unique_brokers_evaluated": 0,
                "unique_rules_evaluated": 0,
                "unique_circulars_referenced": 0,
            }

        vc = df["evaluation_result"].value_counts()
        passed = int(vc.get(_PASS, 0))
        failed = int(vc.get(_FAIL, 0))
        hitl = int(vc.get(_HITL, 0))

        return {
            "total_transactions": total,
            "total_passed": passed,
            "total_failed": failed,
            "total_hitl_review": hitl,
            "overall_pass_rate_pct": round(passed / total * 100, 2),
            "overall_fail_rate_pct": round(failed / total * 100, 2),
            "overall_hitl_rate_pct": round(hitl / total * 100, 2),
            "unique_brokers_evaluated": int(df["broker_id"].nunique()),
            "unique_rules_evaluated": int(df["rule_id"].nunique()),
            "unique_circulars_referenced": int(df["circular_id"].nunique()),
        }

    def _build_time_series(
        self, df: pd.DataFrame, granularity: Granularity
    ) -> list[PeriodBucket]:
        """Group evaluations by period bucket and compute per-bucket rates."""
        if df.empty:
            return []

        df = _add_period_col(df.copy(), granularity)
        groups = df.groupby("_period")

        buckets: list[PeriodBucket] = []
        for period_label, grp in groups:
            total = len(grp)
            vc = grp["evaluation_result"].value_counts()
            passed = int(vc.get(_PASS, 0))
            failed = int(vc.get(_FAIL, 0))
            hitl = int(vc.get(_HITL, 0))

            # Derive period_start / period_end from the actual data in this bucket
            period_start = grp["evaluated_at"].min().date()
            period_end = grp["evaluated_at"].max().date()

            buckets.append(
                PeriodBucket(
                    period_label=str(period_label),
                    period_start=period_start,
                    period_end=period_end,
                    total_transactions=total,
                    passed=passed,
                    failed=failed,
                    hitl_review=hitl,
                    pass_rate_pct=round(passed / total * 100, 2) if total else 0.0,
                    fail_rate_pct=round(failed / total * 100, 2) if total else 0.0,
                    hitl_rate_pct=round(hitl / total * 100, 2) if total else 0.0,
                    unique_rules_triggered=int(grp["rule_id"].nunique()),
                    unique_circulars_referenced=int(grp["circular_id"].nunique()),
                    unique_brokers=int(grp["broker_id"].nunique()),
                )
            )
        return sorted(buckets, key=lambda b: b.period_label)

    def _build_broker_stats(
        self,
        df: pd.DataFrame,
        hitl_df: pd.DataFrame,
        tenant_lookup: dict[str, dict[str, str]],
    ) -> list[BrokerStats]:
        """Per-broker compliance metrics with HITL disposition breakdown."""
        if df.empty:
            return []

        # HITL disposition per broker (from the ORM hitl_reviews table)
        hitl_by_broker: dict[str, dict[str, int]] = {}
        if not hitl_df.empty:
            for _, row in hitl_df.iterrows():
                tid = str(row.get("tenant_id", ""))
                status = str(row.get("status", ""))
                if tid not in hitl_by_broker:
                    hitl_by_broker[tid] = {"PENDING": 0, "RESOLVED": 0, "REJECTED": 0}
                if status in hitl_by_broker[tid]:
                    hitl_by_broker[tid][status] += 1

        stats: list[BrokerStats] = []
        for broker_id, grp in df.groupby("broker_id"):
            broker_id = str(broker_id)
            total = len(grp)
            vc = grp["evaluation_result"].value_counts()
            passed = int(vc.get(_PASS, 0))
            failed = int(vc.get(_FAIL, 0))
            hitl = int(vc.get(_HITL, 0))

            # Top 5 violated rules for this broker
            fail_grp = grp[grp["evaluation_result"] == _FAIL]
            top_rules: list[str] = (
                fail_grp["rule_id"].value_counts().head(5).index.tolist()
                if not fail_grp.empty
                else []
            )
            top_circs: list[str] = (
                fail_grp["circular_id"].value_counts().head(5).index.tolist()
                if not fail_grp.empty
                else []
            )

            meta = tenant_lookup.get(broker_id, {})
            disp = hitl_by_broker.get(broker_id, {})

            stats.append(
                BrokerStats(
                    broker_id=broker_id,
                    display_name=meta.get("display_name"),
                    tenant_type=meta.get("tenant_type"),
                    total_transactions=total,
                    passed=passed,
                    failed=failed,
                    hitl_review=hitl,
                    pass_rate_pct=round(passed / total * 100, 2) if total else 0.0,
                    fail_rate_pct=round(failed / total * 100, 2) if total else 0.0,
                    hitl_rate_pct=round(hitl / total * 100, 2) if total else 0.0,
                    top_violated_rules=top_rules,
                    top_violation_circulars=top_circs,
                    hitl_pending=disp.get("PENDING", 0),
                    hitl_resolved=disp.get("RESOLVED", 0),
                    hitl_rejected=disp.get("REJECTED", 0),
                )
            )

        return sorted(stats, key=lambda s: s.total_transactions, reverse=True)

    def _build_violation_summary(
        self, df: pd.DataFrame, top_n: int = 10
    ) -> list[RuleViolationSummary]:
        """Aggregate the top-N most-violated rules across all brokers."""
        if df.empty:
            return []

        fail_df = df[df["evaluation_result"] == _FAIL].copy()
        if fail_df.empty:
            return []

        grouped = (
            fail_df.groupby(["rule_id", "circular_id", "section_reference"])
            .agg(
                total_failures=("transaction_id", "count"),
                affected_brokers=("broker_id", "nunique"),
                first_seen=("evaluated_at", "min"),
                last_seen=("evaluated_at", "max"),
            )
            .reset_index()
            .sort_values("total_failures", ascending=False)
            .head(top_n)
        )

        return [
            RuleViolationSummary(
                rule_id=str(row["rule_id"]),
                circular_id=str(row["circular_id"]),
                section_reference=str(row["section_reference"]),
                total_failures=int(row["total_failures"]),
                affected_brokers=int(row["affected_brokers"]),
                first_seen=row["first_seen"].to_pydatetime() if not pd.isna(row["first_seen"]) else None,
                last_seen=row["last_seen"].to_pydatetime() if not pd.isna(row["last_seen"]) else None,
            )
            for _, row in grouped.iterrows()
        ]

    async def _build_chain_proof(
        self, start: dt.datetime, end: dt.datetime
    ) -> ChainProofSummary:
        """Run the ledger chain verifier and package results as ChainProofSummary."""
        result = await verify_chain(self._ledger, start_time=start, end_time=end)

        # Fetch the last current_hash in the window to use as the 'seal'
        window_seal: str | None = None
        if result.entries_checked > 0 and result.valid:
            stmt = (
                select(compliance_audit_ledger.c.current_hash)
                .where(
                    compliance_audit_ledger.c.evaluated_at >= start,
                    compliance_audit_ledger.c.evaluated_at <= end,
                )
                .order_by(compliance_audit_ledger.c.sequence_num.desc())
                .limit(1)
            )
            async with self._ledger.connect() as conn:
                row = (await conn.execute(stmt)).first()
                window_seal = row[0] if row else None

        return ChainProofSummary(
            verified_at=result.verified_at,
            entries_checked=result.entries_checked,
            chain_valid=result.valid,
            break_count=len(result.breaks),
            range_start_sequence=result.range_start_sequence,
            range_end_sequence=result.range_end_sequence,
            window_seal_hash=window_seal,
        )
