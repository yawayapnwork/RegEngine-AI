#!/usr/bin/env python
"""regengine-report -- SEBI-ready audit binder & compliance proof export CLI.

An operator/compliance-team tool, distinct from `regengine-cli.py` (which
drives the live ingest->extract->compile->evaluate pipeline for smoke
testing): this tool only ever READS already-committed data -- compiled
rules, HITL review history, and the hash-chained audit ledger -- and
renders it into auditor-facing artifacts. It never mutates production
state.

Commands:
    export         Export executive summary / rule-change / HITL / ledger
                    data for a period, in PDF, Excel, and/or JSON.
    audit-binder   Build the full cryptographic audit binder ZIP
                    (Requirement 2): Rego source, raw source PDFs + clause
                    hashes, the signed ledger proof chain, and a signed
                    executive summary PDF.
    verify-binder  Independently verify a previously-generated binder
                    ZIP's digital signature and per-file hashes -- the
                    tool a SEBI auditor (or this project's own CI) runs
                    against a delivered package, with NO access to this
                    service, its database, or any private key.
    verify-chain   Standalone SHA-256 ledger hash-chain verification for
                    a period (no binder/PDF generation).
    keygen         Generate a fresh RSA-4096 signing key pair for
                    `--sign` (operator setup utility, run once).

Usage:
    python regengine-report.py export --quarter Q2-2025 --format pdf,excel,json --output ./exports/
    python regengine-report.py audit-binder --fiscal-year FY2025-26 --output ./exports/
    python regengine-report.py verify-binder ./exports/regengine_audit_binder_FY2025-26.zip
    python regengine-report.py verify-chain --quarter Q2-2025
    python regengine-report.py keygen --out-dir ./keys/
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import sys
import uuid
import zipfile
from pathlib import Path
from typing import AsyncIterator, Optional

import typer
from rich.console import Console
from rich.panel import Panel
from rich.progress import BarColumn, MofNCompleteColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
from rich.table import Table
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

from app.analytics.aggregator import ComplianceAggregator
from app.analytics.models import ReportPeriod
from app.analytics.pdf_report import render_executive_summary
from app.config import Settings, get_settings
from app.reporting.audit_binder import build_audit_binder
from app.reporting.data_collector import collect_hitl_approvals, collect_referenced_circulars, collect_rule_changes
from app.reporting.excel_export import build_excel_workbook
from app.reporting.json_export import AuditBinderJSON, build_json_export
from app.reporting.period import resolve_period
from app.reporting.signing import DigitalSignature, generate_signing_keypair, verify_signature

app = typer.Typer(
    name="regengine-report",
    help="Export SEBI-ready audit binders and regulatory compliance proofs from RegEngine AI.",
    no_args_is_help=True,
    add_completion=True,
)
console = Console()

_PROGRESS_COLUMNS = (
    SpinnerColumn(),
    TextColumn("[progress.description]{task.description}"),
    BarColumn(),
    MofNCompleteColumn(),
    TimeElapsedColumn(),
)


# --------------------------------------------------------------------------
# DB/ledger plumbing -- a standalone script, not a FastAPI request, so this
# builds its own engines directly (mirrors regengine-cli.py's pattern)
# rather than going through FastAPI's Depends() machinery.
# --------------------------------------------------------------------------


@contextlib.asynccontextmanager
async def _admin_db_session(settings: Settings) -> AsyncIterator[AsyncSession]:
    """Drives app.db.tenant_session.get_admin_db_session (a FastAPI
    generator-dependency) manually -- reused rather than reimplemented so
    this CLI's cross-tenant reads go through EXACTLY the same RLS-bypass
    GUC-setting logic production code does, with no risk of the two
    silently drifting apart."""
    from app.db.tenant_session import get_admin_db_session

    agen = get_admin_db_session()
    session = await agen.__anext__()
    try:
        yield session
    finally:
        with contextlib.suppress(StopAsyncIteration):
            await agen.__anext__()
        await agen.aclose()


def _ledger_engine(settings: Settings) -> AsyncEngine:
    return create_async_engine(settings.ledger_database_url, pool_pre_ping=True)


def _period_from_options(quarter: Optional[str], fiscal_year: Optional[str], start: Optional[str], end: Optional[str]) -> ReportPeriod:
    try:
        return resolve_period(quarter=quarter, fiscal_year=fiscal_year, start=start, end=end)
    except ValueError as exc:
        console.print(f"[bold red]Error:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc


_QuarterOption = typer.Option(None, "--quarter", "-q", help="e.g. 'Q2-2025' (Jul-Sep of FY2025-26)")
_FiscalYearOption = typer.Option(None, "--fiscal-year", "-y", help="e.g. 'FY2025-26' (Apr 2025 - Mar 2026)")
_StartOption = typer.Option(None, "--start", help="ISO date, e.g. 2025-07-01 (use with --end)")
_EndOption = typer.Option(None, "--end", help="ISO date, e.g. 2025-09-30 (use with --start)")
_TenantOption = typer.Option(None, "--tenant", "-t", help="Restrict to one broker tenant_id; omit for cross-tenant.")
_OutputOption = typer.Option(Path("./regengine-report-output"), "--output", "-o", help="Output directory.")


# --------------------------------------------------------------------------
# export
# --------------------------------------------------------------------------


@app.command()
def export(
    quarter: Optional[str] = _QuarterOption,
    fiscal_year: Optional[str] = _FiscalYearOption,
    start: Optional[str] = _StartOption,
    end: Optional[str] = _EndOption,
    tenant: Optional[str] = _TenantOption,
    output: Path = _OutputOption,
    formats: str = typer.Option("pdf,excel,json", "--format", "-f", help="Comma-separated: pdf,excel,json"),
    generated_by: str = typer.Option("cli-operator", "--as", help="Principal name recorded as report generator."),
) -> None:
    """Export executive summary + rule-change/HITL/ledger data for a
    period, WITHOUT the full cryptographic ZIP binder (use `audit-binder`
    for that). Fast path for a compliance team that just wants the
    numbers, not the signed evidence package."""
    period = _period_from_options(quarter, fiscal_year, start, end)
    requested_formats = {f.strip().lower() for f in formats.split(",") if f.strip()}
    invalid = requested_formats - {"pdf", "excel", "json"}
    if invalid:
        console.print(f"[bold red]Error:[/bold red] unknown format(s): {invalid}. Choose from pdf, excel, json.")
        raise typer.Exit(code=1)

    output.mkdir(parents=True, exist_ok=True)
    console.print(Panel(f"[bold]RegEngine AI Compliance Export[/bold]\nPeriod: {period.label()}\nTenant: {tenant or 'ALL'}\nFormats: {', '.join(sorted(requested_formats))}"))

    asyncio.run(_run_export(period, tenant, output, requested_formats, generated_by))
    console.print(f"[bold green]Done.[/bold green] Files written to {output.resolve()}")


async def _run_export(period: ReportPeriod, tenant: Optional[str], output: Path, formats: set[str], generated_by: str) -> None:
    settings = get_settings()
    report_id = str(uuid.uuid4())
    ledger_engine = _ledger_engine(settings)

    try:
        async with _admin_db_session(settings) as db:
            with Progress(*_PROGRESS_COLUMNS, console=console) as progress_bar:
                task = progress_bar.add_task("Aggregating executive metrics...", total=4)

                aggregator = ComplianceAggregator(ledger_engine=ledger_engine, db_session=db)
                aggregated_report = await aggregator.build_aggregated_report(
                    period=period, report_id=report_id, generated_by=generated_by, tenant_id=tenant
                )
                progress_bar.advance(task)

                progress_bar.update(task, description="Collecting rule changes / HITL logs...")
                start_dt, end_dt = period.start_datetime, period.end_datetime
                rule_changes = await collect_rule_changes(db, start_dt, end_dt, tenant)
                hitl_approvals = await collect_hitl_approvals(db, start_dt, end_dt, tenant)
                source_circulars = await collect_referenced_circulars(db, rule_changes, hitl_approvals)
                ledger_entries, _chain_proof, _total = await aggregator.build_audit_trail(
                    period=period, report_id=report_id, generated_by=generated_by, tenant_id=tenant, page=1, page_size=10_000_000
                )
                progress_bar.advance(task)

                if "pdf" in formats:
                    progress_bar.update(task, description="Rendering PDF...")
                    pdf_bytes = render_executive_summary(aggregated_report)
                    (output / f"executive_summary_{_slug(period)}.pdf").write_bytes(pdf_bytes)
                progress_bar.advance(task)

                if "excel" in formats:
                    progress_bar.update(task, description="Building Excel workbook...")
                    xlsx_bytes = build_excel_workbook(
                        period_label=period.label(), rule_changes=rule_changes, hitl_approvals=hitl_approvals,
                        ledger_entries=ledger_entries, source_circulars=source_circulars,
                    )
                    (output / f"audit_data_{_slug(period)}.xlsx").write_bytes(xlsx_bytes)

                if "json" in formats:
                    progress_bar.update(task, description="Building JSON export...")
                    json_bytes = build_json_export(
                        AuditBinderJSON(
                            report_id=report_id, period_label=period.label(), period_start=period.start_date,
                            period_end=period.end_date, generated_by=generated_by, tenant_scope=tenant or "all",
                            executive_summary=aggregated_report, rule_changes=rule_changes, hitl_approvals=hitl_approvals,
                            ledger_entries=ledger_entries, source_circulars=source_circulars,
                        )
                    )
                    (output / f"audit_data_{_slug(period)}.json").write_bytes(json_bytes)
                progress_bar.advance(task)
    finally:
        await ledger_engine.dispose()


def _slug(period: ReportPeriod) -> str:
    return period.label().replace(" ", "_").replace("/", "-")


# --------------------------------------------------------------------------
# audit-binder
# --------------------------------------------------------------------------


@app.command(name="audit-binder")
def audit_binder_cmd(
    quarter: Optional[str] = _QuarterOption,
    fiscal_year: Optional[str] = _FiscalYearOption,
    start: Optional[str] = _StartOption,
    end: Optional[str] = _EndOption,
    tenant: Optional[str] = _TenantOption,
    output: Path = _OutputOption,
    generated_by: str = typer.Option("cli-operator", "--as", help="Principal name recorded as report generator."),
) -> None:
    """Build the full cryptographic audit binder ZIP (Requirement 2):
    Rego source, raw circular PDFs + clause hashes, the verifiable
    SHA-256 ledger proof chain, and a digitally signed executive summary
    PDF -- everything a SEBI audit request needs in one package.

    Requires `settings.audit_binder_signing_private_key_pem` to produce a
    SIGNED package; without it, the binder is still built but ships
    unsigned (a loud warning is printed) -- run `keygen` first."""
    period = _period_from_options(quarter, fiscal_year, start, end)
    output.mkdir(parents=True, exist_ok=True)
    console.print(Panel(f"[bold]RegEngine AI Audit Binder[/bold]\nPeriod: {period.label()}\nTenant: {tenant or 'ALL'}"))

    zip_path = output / f"regengine_audit_binder_{_slug(period)}.zip"
    asyncio.run(_run_audit_binder(period, tenant, zip_path, generated_by))

    digest = hashlib.sha256(zip_path.read_bytes()).hexdigest()
    console.print(f"[bold green]Audit binder written:[/bold green] {zip_path.resolve()}")
    console.print(f"  Package SHA-256: [cyan]{digest}[/cyan]")
    console.print(f"  Verify with: python regengine-report.py verify-binder {zip_path}")


async def _run_audit_binder(period: ReportPeriod, tenant: Optional[str], zip_path: Path, generated_by: str) -> None:
    settings = get_settings()
    report_id = str(uuid.uuid4())
    ledger_engine = _ledger_engine(settings)

    try:
        async with _admin_db_session(settings) as db:
            with Progress(*_PROGRESS_COLUMNS, console=console) as progress_bar:
                task = progress_bar.add_task("Building audit binder...", total=6)

                def on_progress(stage: str, current: int, total: int) -> None:
                    progress_bar.update(task, completed=current, total=total, description=stage)

                zip_bytes = await build_audit_binder(
                    period=period, settings=settings, db=db, ledger_engine=ledger_engine,
                    report_id=report_id, generated_by=generated_by, tenant_id=tenant, progress=on_progress,
                )
        zip_path.write_bytes(zip_bytes)
    finally:
        await ledger_engine.dispose()


# --------------------------------------------------------------------------
# verify-binder
# --------------------------------------------------------------------------


@app.command(name="verify-binder")
def verify_binder_cmd(zip_path: Path = typer.Argument(..., exists=True, readable=True, help="Path to a regengine_audit_binder_*.zip")) -> None:
    """Independently verifies a previously-generated audit binder: every
    file's SHA-256 against manifest.json, and manifest.json's RSA-PSS
    signature against the embedded public key in digital_signature.json.
    Runs with NO access to this service, its database, or any private
    key -- exactly what a SEBI auditor (or this project's CI) would run
    against a delivered package."""
    console.print(Panel(f"[bold]Verifying audit binder:[/bold] {zip_path}"))

    with zipfile.ZipFile(zip_path, "r") as zf:
        names = set(zf.namelist())
        if "manifest.json" not in names:
            console.print("[bold red]FAIL:[/bold red] manifest.json not found in package.")
            raise typer.Exit(code=1)

        manifest_bytes = zf.read("manifest.json")
        manifest = json.loads(manifest_bytes)

        table = Table(title="Per-File Integrity Check")
        table.add_column("File")
        table.add_column("Status")
        all_files_ok = True
        for entry in manifest["files"]:
            path, expected_sha256 = entry["path"], entry["sha256"]
            if path not in names:
                table.add_row(path, "[bold red]MISSING[/bold red]")
                all_files_ok = False
                continue
            actual = hashlib.sha256(zf.read(path)).hexdigest()
            ok = actual == expected_sha256
            all_files_ok &= ok
            table.add_row(path, "[green]OK[/green]" if ok else f"[bold red]HASH MISMATCH[/bold red] (expected {expected_sha256[:12]}..., got {actual[:12]}...)")
        console.print(table)

        signature_ok = None
        if "digital_signature.json" in names:
            signature = DigitalSignature.model_validate_json(zf.read("digital_signature.json"))
            signature_ok = verify_signature(manifest_bytes, signature)
            console.print(f"Digital signature ({signature.algorithm}, signed by {signature.signer_id} at {signature.signed_at}): "
                          + ("[bold green]VALID[/bold green]" if signature_ok else "[bold red]INVALID[/bold red]"))
        else:
            console.print("[yellow]No digital_signature.json found -- this package was not signed.[/yellow]")

    if all_files_ok and signature_ok is not False:
        console.print("\n[bold green]Overall result: PASS[/bold green] -- package integrity confirmed.")
    else:
        console.print("\n[bold red]Overall result: FAIL[/bold red] -- do not treat this package as authentic.")
        raise typer.Exit(code=1)


# --------------------------------------------------------------------------
# verify-chain
# --------------------------------------------------------------------------


@app.command(name="verify-chain")
def verify_chain_cmd(
    quarter: Optional[str] = _QuarterOption,
    fiscal_year: Optional[str] = _FiscalYearOption,
    start: Optional[str] = _StartOption,
    end: Optional[str] = _EndOption,
) -> None:
    """Standalone SHA-256 ledger hash-chain verification for a period --
    no PDF/Excel/ZIP generation, just the chain-integrity result."""
    period = _period_from_options(quarter, fiscal_year, start, end)
    settings = get_settings()

    async def _run() -> None:
        from app.ledger.verifier import verify_chain

        ledger_engine = _ledger_engine(settings)
        try:
            with console.status(f"Verifying ledger chain for {period.label()}..."):
                result = await verify_chain(ledger_engine, period.start_datetime, period.end_datetime)
        finally:
            await ledger_engine.dispose()

        if result.valid:
            console.print(f"[bold green]Chain VALID[/bold green] -- {result.entries_checked} entries checked, sequence [{result.range_start_sequence}, {result.range_end_sequence}].")
        else:
            console.print(f"[bold red]Chain INVALID[/bold red] -- {len(result.breaks)} break(s) found:")
            for b in result.breaks:
                console.print(f"  seq={b.sequence_num}: {b.reason} (expected={b.expected!r}, actual={b.actual!r})")
            raise typer.Exit(code=1)

    asyncio.run(_run())


# --------------------------------------------------------------------------
# keygen
# --------------------------------------------------------------------------


@app.command()
def keygen(
    out_dir: Path = typer.Option(Path("./keys"), "--out-dir", help="Directory to write the key pair into."),
) -> None:
    """Generates a fresh RSA-4096 key pair for signing audit binders.
    Run ONCE per signing identity; store the private key in
    settings.audit_binder_signing_private_key_pem (via your secrets
    backend, never committed to source control) and distribute the
    public key to auditors for independent verification."""
    out_dir.mkdir(parents=True, exist_ok=True)
    private_pem, public_pem = generate_signing_keypair()

    private_path = out_dir / "audit_binder_signing_key.pem"
    public_path = out_dir / "audit_binder_signing_key.pub.pem"
    private_path.write_text(private_pem, encoding="utf-8")
    public_path.write_text(public_pem, encoding="utf-8")

    console.print(f"[bold green]Key pair generated.[/bold green]")
    console.print(f"  Private key: {private_path.resolve()}  [bold red](keep secret -- set as AUDIT_BINDER_SIGNING_PRIVATE_KEY_PEM)[/bold red]")
    console.print(f"  Public key:  {public_path.resolve()}  (distribute to auditors for verification)")


if __name__ == "__main__":
    app()
