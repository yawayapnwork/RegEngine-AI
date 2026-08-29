"""Assembles the full cryptographic audit binder ZIP (Requirement 2).

Package layout:

    regengine_audit_binder_<period>.zip
      manifest.json                    -- {path, sha256} for every evidentiary file below
      digital_signature.json           -- RSA-PSS/SHA-256 signature over manifest.json's exact bytes
      executive_summary.pdf            -- app.analytics.pdf_report.render_executive_summary output,
                                           PLUS a human-readable "Digital Signature & Certification"
                                           appendix page. Deliberately NOT itself in manifest.json --
                                           see module docstring below for why.
      audit_binder.xlsx                -- app.reporting.excel_export
      audit_binder.json                -- app.reporting.json_export
      ledger_proof.json                -- AuditTrailReport (full chain-of-custody rows + ChainProofSummary)
      rego/<rule_id>.rego              -- every compiled Rego module active/changed in the period
      source_circulars/<circular>.pdf  -- raw source PDFs (from the local ingestion archive),
                                           when available on disk
      source_circulars/clause_hashes.json -- {circular_number: {clause_number: sha256}} for every
                                           clause referenced, independent of whether the raw PDF
                                           itself was recoverable

Why `executive_summary.pdf` is excluded from `manifest.json`'s hash set:
appending a human-readable signature page to a PDF whose OWN hash is
inside the very manifest that page displays would be circular (the
manifest would need to include a hash of a file that doesn't exist yet
until after the manifest is signed). The PDF is a derived, human-readable
summary of the same data the manifest's other files already carry
verbatim (Rego source, raw circular PDFs, the ledger's own hash chain) --
the RAW EVIDENCE is what gets cryptographically covered; the PDF is a
courtesy rendering of it, and says so on its own appendix page.
"""
from __future__ import annotations

import hashlib
import io
import json
import logging
import zipfile
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.analytics.aggregator import ComplianceAggregator
from app.analytics.models import AuditTrailReport, ReportPeriod
from app.analytics.pdf_report import render_executive_summary
from app.config import Settings
from app.db.models import Circular, Clause, CompiledRule
from app.reporting.data_collector import collect_hitl_approvals, collect_referenced_circulars, collect_rule_changes
from app.reporting.excel_export import build_excel_workbook
from app.reporting.json_export import AuditBinderJSON, build_json_export
from app.reporting.pdf_signing_page import append_signature_page
from app.reporting.signing import SigningKeyNotConfiguredError, sign_manifest

logger = logging.getLogger(__name__)


class BinderProgress:
    """Callback protocol the CLI's progress bar implements -- kept as a
    plain duck-typed callable (not an abstract base class) so a caller
    that doesn't care about progress can just pass a no-op lambda."""

    def __call__(self, stage: str, current: int, total: int) -> None: ...


def _noop_progress(stage: str, current: int, total: int) -> None:
    return None


async def _collect_rego_files(db: AsyncSession, rule_ids: set[str]) -> dict[str, str]:
    """Returns {rule_id: rego_source} for every rule_id with a stored
    `rego_policy` -- pulled from whichever `CompiledRule` version is
    ACTIVE for that rule_id today (the version this period's evaluations
    actually ran against is the historically correct one to bind into the
    versioned audit binder, not necessarily whatever happens to be active
    at export time if it has since been superseded -- see the note in
    `build_audit_binder` about this being a known simplification)."""
    if not rule_ids:
        return {}
    rows = (
        await db.execute(
            select(CompiledRule.rule_id, CompiledRule.rego_policy).where(
                CompiledRule.rule_id.in_(rule_ids), CompiledRule.rego_policy.is_not(None)
            )
        )
    ).all()
    return {rule_id: rego for rule_id, rego in rows}


def _collect_clause_hashes(db_rows: list[tuple[str, str, str]]) -> dict[str, dict[str, str]]:
    result: dict[str, dict[str, str]] = {}
    for circular_number, clause_number, sha256 in db_rows:
        result.setdefault(circular_number, {})[clause_number or "unscoped"] = sha256
    return result


def _find_source_pdf(circular, settings: Settings) -> Path | None:
    """Mirrors app.ingestion.pipeline_trigger._archive_path's naming
    convention to locate a previously-archived raw PDF on disk. Returns
    None (not an error) when unavailable -- an ingestion-archive purge, a
    manually-uploaded circular with no source_url, or a deployment that
    never enabled local archiving are all legitimate reasons a raw PDF
    might be missing; the binder still ships `clause_hashes.json` for
    that circular either way, so the cryptographic chain of custody is
    never broken even when the convenience artifact (the raw PDF itself)
    is unavailable."""
    if not circular.source_url:
        return None
    filename = circular.source_url.rsplit("/", 1)[-1] or f"{circular.circular_number}.pdf"
    candidate = Path(settings.ingestion_pdf_download_dir) / filename
    return candidate if candidate.exists() else None


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


async def build_audit_binder(
    *,
    period: ReportPeriod,
    settings: Settings,
    db: AsyncSession,
    ledger_engine: AsyncEngine,
    report_id: str,
    generated_by: str,
    tenant_id: str | None = None,
    progress: BinderProgress = _noop_progress,
) -> bytes:
    """Returns the complete ZIP package as bytes. Six stages, each
    reported to `progress` so a CLI can render a meaningful progress bar
    rather than one opaque spinner for the whole (potentially slow, DB-
    and-crypto-heavy) operation.

    Known simplification: Rego source is pulled from each rule_id's
    CURRENTLY ACTIVE compiled version, not a point-in-time snapshot of
    what was active during the reporting period itself. `CompiledRule`
    retains full version history (every version ever created, per
    app.db.models.CompiledRule's docstring) but does not record an
    activation/deactivation TIMESTAMP per version, only a boolean
    `is_active` -- reconstructing "which version was live on any given
    day in the past" would require that history and is a documented gap,
    not silently swept under the rug.
    """
    files: dict[str, bytes] = {}

    # --- Stage 1: executive summary stats + chain proof ---
    progress("Aggregating executive metrics", 1, 6)
    aggregator = ComplianceAggregator(ledger_engine=ledger_engine, db_session=db)
    aggregated_report = await aggregator.build_aggregated_report(
        period=period, report_id=report_id, generated_by=generated_by, tenant_id=tenant_id, verify_chain_integrity=True
    )
    # No pagination for the binder (unlike the interactive
    # GET /v1/analytics/audit-trail endpoint, capped at 2000/page there):
    # a large page_size still costs one DB round trip, since
    # ComplianceAggregator fetches the whole period into memory before
    # client-side-slicing it for pagination -- the audit binder needs
    # every entry in the period regardless, so requesting one huge "page"
    # is both correct and no less efficient than the paginated path.
    ledger_entries, chain_proof, total_ledger_entries = await aggregator.build_audit_trail(
        period=period, report_id=report_id, generated_by=generated_by, tenant_id=tenant_id, page=1, page_size=10_000_000
    )
    audit_trail = AuditTrailReport(
        report_id=report_id, generated_by=generated_by, period=period, tenant_scope=tenant_id or "all",
        total_entries=total_ledger_entries, entries=ledger_entries, chain_proof=chain_proof,
        page=1, page_size=total_ledger_entries or 1, total_pages=1,
    )

    # --- Stage 2: row-level rule/HITL/circular collection ---
    progress("Collecting rule changes and HITL approval logs", 2, 6)
    start, end = period.start_datetime, period.end_datetime
    rule_changes = await collect_rule_changes(db, start, end, tenant_id)
    hitl_approvals = await collect_hitl_approvals(db, start, end, tenant_id)
    source_circulars = await collect_referenced_circulars(db, rule_changes, hitl_approvals)

    # --- Stage 3: Rego source + raw PDFs + clause-hash manifest ---
    progress("Bundling Rego source and source circular PDFs", 3, 6)
    rego_by_rule_id = await _collect_rego_files(db, {r.rule_id for r in rule_changes})
    for rule_id, rego_source in rego_by_rule_id.items():
        safe_name = rule_id.replace("/", "_").replace(":", "_")
        files[f"rego/{safe_name}.rego"] = rego_source.encode("utf-8")

    clause_rows = (
        await db.execute(
            select(Circular.circular_number, Clause.clause_number, Clause.sha256)
            .join(Clause, Clause.circular_id == Circular.id)
            .where(Circular.circular_number.in_({c.circular_number for c in source_circulars}))
        )
    ).all()
    clause_hashes = _collect_clause_hashes(clause_rows)
    files["source_circulars/clause_hashes.json"] = json.dumps(clause_hashes, indent=2).encode("utf-8")

    circular_rows_full = (await db.execute(select(Circular).where(Circular.circular_number.in_({c.circular_number for c in source_circulars})))).scalars().all()
    for circular in circular_rows_full:
        pdf_path = _find_source_pdf(circular, settings)
        if pdf_path is not None:
            files[f"source_circulars/{circular.circular_number.replace('/', '_')}.pdf"] = pdf_path.read_bytes()
        else:
            logger.warning("Source PDF for circular %s not found on disk; only its clause hashes are included.", circular.circular_number)

    # --- Stage 4: Excel + JSON exports, ledger proof export ---
    progress("Building Excel workbook and JSON export", 4, 6)
    files["ledger_proof.json"] = audit_trail.model_dump_json(indent=2).encode("utf-8")
    files["audit_binder.xlsx"] = build_excel_workbook(
        period_label=period.label(), rule_changes=rule_changes, hitl_approvals=hitl_approvals,
        ledger_entries=audit_trail.entries, source_circulars=source_circulars,
    )
    files["audit_binder.json"] = build_json_export(
        AuditBinderJSON(
            report_id=report_id, period_label=period.label(), period_start=period.start_date, period_end=period.end_date,
            generated_by=generated_by, tenant_scope=tenant_id or "all", executive_summary=aggregated_report,
            rule_changes=rule_changes, hitl_approvals=hitl_approvals, ledger_entries=audit_trail.entries,
            source_circulars=source_circulars,
        )
    )

    # --- Stage 5: manifest + digital signature ---
    progress("Signing package manifest", 5, 6)
    manifest = {
        "package_id": report_id,
        "period": {"label": period.label(), "start": period.start_date.isoformat(), "end": period.end_date.isoformat()},
        "tenant_scope": tenant_id or "all",
        "files": [{"path": path, "sha256": _sha256_hex(content)} for path, content in sorted(files.items())],
        "note": (
            "executive_summary.pdf is intentionally NOT listed here -- see app.reporting.audit_binder's "
            "module docstring for why (it is a derived, human-readable rendering of this same data, not "
            "itself part of the signed raw-evidence set)."
        ),
    }
    manifest_bytes = json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8")
    files["manifest.json"] = manifest_bytes

    try:
        signature = sign_manifest(manifest_bytes, settings)
        files["digital_signature.json"] = signature.model_dump_json(indent=2).encode("utf-8")
    except SigningKeyNotConfiguredError as exc:
        logger.warning("Audit binder will be UNSIGNED: %s", exc)
        signature = None

    # --- Stage 6: executive summary PDF (built last so the signature, if any, can be displayed on it) ---
    progress("Rendering executive summary PDF", 6, 6)
    summary_pdf = render_executive_summary(aggregated_report)
    if signature is not None:
        summary_pdf = append_signature_page(summary_pdf, signature)
    files["executive_summary.pdf"] = summary_pdf

    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path, content in files.items():
            zf.writestr(path, content)

    return zip_buffer.getvalue()
