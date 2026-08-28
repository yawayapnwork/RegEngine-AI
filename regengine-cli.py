#!/usr/bin/env python
"""regengine-cli.py -- RegEngine AI end-to-end pipeline runner.

A Lead-Systems-Integrator smoke-test / demo tool: drives the ENTIRE
pipeline in one command, calling the same production code paths the
FastAPI service and Celery workers use (never a reimplementation of them),
so a green run here is a real signal the stack is wired together correctly:

    1. Ingest      a SEBI circular PDF (local file, http(s) URL, or a
                    built-in synthetic sample) -> layout-aware clause chunks
                    (app.services.pipeline.parse_pdf_bytes)
    2. Extract/Audit  the target clause through the CrewAI dual-agent
                    pipeline (app.agents.pipeline.extract_and_audit_clause),
                    or a canned deterministic rule with --offline-agents
                    when no ANTHROPIC_API_KEY is configured
    3. Compile      the audited rule to OPA Rego (+ JSON-Logic fallback)
                    (app.compiler.pipeline.compile_audited_rule)
    4. Deploy       the compiled Rego to a live OPA server's Policy API
                    (app.execution.opa_engine.OPAEngine.publish_policy)
    5. Simulate     a broker trade payload and evaluate it against that
                    policy (app.execution.opa_engine.OPAEngine.evaluate)
    6. Commit       the evaluation to the SHA-256 hash-chained audit ledger
                    and verify the whole chain's integrity
                    (app.ledger.service.LedgerService, app.ledger.verifier)

Requires a reachable OPA server and PostgreSQL ledger for steps 4-6 (e.g.
`docker compose up opa postgres` -- see docker-compose.yml); step 2 needs
ANTHROPIC_API_KEY unless run with --offline-agents. Steps 1-3 have no
external dependency beyond what's already in requirements.txt.

Usage:
    python regengine-cli.py                                    # full run, synthetic sample, real agents
    python regengine-cli.py --offline-agents                    # no ANTHROPIC_API_KEY required
    python regengine-cli.py --pdf ./my_circular.pdf              # ingest a real file
    python regengine-cli.py --pdf https://example.com/c.pdf      # ingest from a URL
    python regengine-cli.py --facts '{"upfront_margin_pct": 25}' # demonstrate an ALLOW instead of DENY
    python regengine-cli.py --dry-run                            # steps 1-3 only, no OPA/Postgres needed
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import sys
import uuid
from pathlib import Path
from typing import Any

import click
import httpx
from rich.console import Console
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table
from rich.traceback import install as install_rich_traceback
from sqlalchemy.ext.asyncio import create_async_engine

from app.agents.pipeline import extract_and_audit_clause
from app.agents.schemas import (
    AuditedComplianceRule,
    AuditVerdict,
    ComparisonOperator,
    ComplianceRuleAudit,
    ExtractedComplianceRule,
    NumericalThreshold,
    ObligationType,
    TargetEntity,
)
from app.compiler.models import CompilationResult
from app.compiler.naming import metric_field_name
from app.compiler.pipeline import compile_audited_rule
from app.config import Settings
from app.execution.models import Decision
from app.execution.opa_engine import OPAEngine, OPAEngineError
from app.ledger.models import ComplianceEvaluationEvent, EvaluationOutcome, LedgerEntry
from app.ledger.service import LedgerService
from app.ledger.verifier import ChainVerificationResult, verify_chain
from app.models import ClauseChunk, ParseResult
from app.parsing.exceptions import ParsingError
from app.services.pipeline import parse_pdf_bytes

install_rich_traceback(show_locals=False, suppress=[click])
console = Console()

STEP_TITLES = (
    "Ingest SEBI circular",
    "Extraction + Logic Audit",
    "Compile to OPA Rego",
    "Deploy to OPA",
    "Simulate broker transaction",
    "Commit to audit ledger + verify chain",
)


class PipelineError(click.ClickException):
    """A step failed in an expected, already-diagnosed way (bad input,
    unreachable OPA, HITL-blocked compilation, ...). Caught at the top
    level and rendered as a red panel instead of a stack trace -- an
    UNEXPECTED exception still gets rich's full traceback via
    install_rich_traceback, which is the signal something here itself
    is broken, not the pipeline's subject matter."""


# --------------------------------------------------------------------------
# Synthetic sample PDF (used when --pdf is omitted)
# --------------------------------------------------------------------------

_SAMPLE_CIRCULAR_LINES = [
    "SEBI/HO/MRD/DP/CIR/P/2026/45",
    "",
    "Master Circular for Stock Brokers",
    "",
    "1 January 2026",
    "",
    "1. Applicability",
    "This circular applies to all stock brokers registered with SEBI.",
    "",
    "2. Margin Requirements",
    "2.1 Every stock broker shall maintain an upfront margin of not",
    "less than 20% of the transaction value for all cash market trades.",
]


def _pdf_escape(text: str) -> str:
    return text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")


def build_sample_pdf_bytes(lines: list[str] = _SAMPLE_CIRCULAR_LINES) -> bytes:
    """Hand-assembles a minimal, valid, single-page PDF (uncompressed
    content stream, base-14 Helvetica, no external dependency such as
    reportlab) so `--pdf` can be omitted and the CLI still exercises the
    REAL PDF-parsing backend (app.parsing.extractor) end-to-end, not a
    mocked stand-in for it."""
    objects: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /Resources << /Font << /F1 4 0 R >> >> "
        b"/MediaBox [0 0 612 792] /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]

    content_parts = ["BT", "/F1 11 Tf", "50 760 Td", "14 TL"]
    for i, line in enumerate(lines):
        escaped = _pdf_escape(line)
        content_parts.append(f"({escaped}) Tj" if i == 0 else f"T* ({escaped}) Tj")
    content_parts.append("ET")
    content_stream = "\n".join(content_parts).encode("latin-1")
    objects.append(b"<< /Length %d >>\nstream\n" % len(content_stream) + content_stream + b"\nendstream")

    buf = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for i, obj in enumerate(objects, start=1):
        offsets.append(len(buf))
        buf += f"{i} 0 obj\n".encode("latin-1") + obj + b"\nendobj\n"

    xref_offset = len(buf)
    count = len(objects) + 1
    buf += f"xref\n0 {count}\n".encode("latin-1")
    buf += b"0000000000 65535 f \n"
    for off in offsets:
        buf += f"{off:010d} 00000 n \n".encode("latin-1")
    buf += f"trailer\n<< /Size {count} /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF".encode("latin-1")
    return bytes(buf)


async def _load_pdf_bytes(pdf_source: str | None) -> tuple[bytes, str]:
    if pdf_source is None:
        return build_sample_pdf_bytes(), "sample_circular.pdf"

    if pdf_source.startswith(("http://", "https://")):
        try:
            async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
                response = await client.get(pdf_source)
                response.raise_for_status()
        except httpx.HTTPError as exc:
            raise PipelineError(f"Failed to download PDF from '{pdf_source}': {exc}") from exc
        filename = pdf_source.rsplit("/", 1)[-1] or "circular.pdf"
        return response.content, filename

    path = Path(pdf_source)
    if not path.is_file():
        raise PipelineError(f"PDF file not found: {path}")
    return path.read_bytes(), path.name


# --------------------------------------------------------------------------
# Offline (--offline-agents) canned extraction/audit
# --------------------------------------------------------------------------


def _canned_audited_rule(chunk: ClauseChunk) -> AuditedComplianceRule:
    """A deterministic 'Upfront Margin >= 20%' rule bound to the ACTUAL
    ingested chunk's identity (sha256/chunk_id/circular_number/clause_number),
    used in place of a real CrewAI/Claude call when --offline-agents is
    set. Mirrors the fixture used throughout this repo's own test suite
    (tests/test_agent_pipeline.py) -- the same worked example, here
    standing in for a live LLM extraction rather than testing one."""
    rule = ExtractedComplianceRule(
        rule_id=f"{chunk.sha256}:{chunk.clause_number or 'unscoped'}",
        source_chunk_id=chunk.chunk_id,
        source_sha256=chunk.sha256,
        circular_number=chunk.circular_number,
        clause_number=chunk.clause_number,
        section_path=chunk.section_path,
        target_entities=[
            TargetEntity(raw_text="stock broker", normalized_entity="Stockbroker", verbatim_evidence="stock broker")
        ],
        deterministic_logic=[
            NumericalThreshold(
                metric="Upfront Margin",
                operator=ComparisonOperator.GTE,
                value=20,
                unit="%",
                applies_to="Stockbroker",
                verbatim_evidence="not less than 20% of the transaction value",
            )
        ],
        obligation_type=ObligationType.MANDATORY,
        extraction_confidence=0.95,
    )
    audit = ComplianceRuleAudit(
        rule_id=rule.rule_id,
        verdict=AuditVerdict.APPROVED,
        fidelity_score=0.98,
        findings=[],
        verified_quote_count=1,
        unverified_quote_count=0,
    )
    return AuditedComplianceRule(rule=rule, audit=audit)


def _select_target_chunk(parsed: ParseResult) -> ClauseChunk:
    """Picks the clause chunk to run through extraction: prefers one that
    looks like it carries a numeric obligation (a '%' sign is a strong,
    cheap signal for this sample-circular use case) over the first chunk,
    which is often a title/applicability clause with nothing to extract."""
    for chunk in parsed.chunks:
        if "%" in chunk.text:
            return chunk
    return parsed.chunks[0]


def _default_violating_facts(thresholds: list[NumericalThreshold]) -> dict[str, float]:
    """Builds a transaction fact map that INTENTIONALLY VIOLATES every
    extracted threshold, derived from whatever was actually extracted
    (offline canned rule or a real, unpredictable LLM extraction) rather
    than a hardcoded field name -- so the default --facts always produces
    a meaningful DENY regardless of --pdf/--offline-agents. Pass --facts
    explicitly to demonstrate an ALLOW instead."""
    facts: dict[str, float] = {}
    for t in thresholds:
        field = metric_field_name(t.metric, t.unit)
        if t.operator in (ComparisonOperator.GTE, ComparisonOperator.GT):
            facts[field] = t.value - 5
        elif t.operator in (ComparisonOperator.LTE, ComparisonOperator.LT):
            facts[field] = t.value + 5
        elif t.operator == ComparisonOperator.EQ:
            facts[field] = t.value + 1
        elif t.operator == ComparisonOperator.RANGE:
            facts[field] = (t.value_upper or t.value) + 5
    return facts


def _map_opa_result(opa_result: dict[str, Any] | None) -> tuple[Decision, EvaluationOutcome]:
    if opa_result is None:
        return Decision.FLAGGED, EvaluationOutcome.HITL_REVIEW
    if opa_result.get("violations"):
        return Decision.DENY, EvaluationOutcome.FAIL
    return Decision.ALLOW, EvaluationOutcome.PASS


# --------------------------------------------------------------------------
# Rich display helpers
# --------------------------------------------------------------------------

_DECISION_STYLE = {Decision.ALLOW: "bold green", Decision.DENY: "bold red", Decision.FLAGGED: "bold yellow"}


def _step_header(n: int) -> None:
    console.rule(f"[bold cyan]Step {n}/6[/bold cyan] -- {STEP_TITLES[n - 1]}", style="cyan")


def _ok(message: str) -> None:
    console.print(f"[bold green]OK[/bold green]  {message}")


# --------------------------------------------------------------------------
# Pipeline steps
# --------------------------------------------------------------------------


async def step_ingest(pdf_source: str | None, settings: Settings) -> tuple[ParseResult, ClauseChunk]:
    _step_header(1)
    pdf_bytes, filename = await _load_pdf_bytes(pdf_source)
    console.print(f"Source: [cyan]{pdf_source or '(built-in synthetic sample)'}[/cyan]  ({len(pdf_bytes)} bytes)")

    try:
        parsed = await parse_pdf_bytes(pdf_bytes, filename, settings)
    except ParsingError as exc:
        raise PipelineError(f"PDF ingestion failed: {exc}") from exc

    table = Table(show_header=True, header_style="bold cyan", box=None)
    table.add_column("Clause")
    table.add_column("Section path")
    table.add_column("Text", overflow="fold", max_width=70)
    for chunk in parsed.chunks:
        table.add_row(chunk.clause_number or "-", " > ".join(chunk.section_path) or "-", chunk.text[:120])
    console.print(table)

    target = _select_target_chunk(parsed)
    _ok(f"Parsed {len(parsed.chunks)} clause chunk(s) from '{parsed.metadata.circular_number or filename}'.")
    console.print(f"Target clause for extraction: [bold]{target.clause_number or target.chunk_id}[/bold]")
    return parsed, target


async def step_extract_and_audit(chunk: ClauseChunk, settings: Settings, offline: bool) -> AuditedComplianceRule:
    _step_header(2)
    if offline:
        console.print("[yellow]--offline-agents set: skipping CrewAI/Anthropic, using a canned rule.[/yellow]")
        audited = _canned_audited_rule(chunk)
    else:
        if not settings.anthropic_api_key:
            raise PipelineError(
                "ANTHROPIC_API_KEY is not set. Configure it in .env, or re-run with --offline-agents "
                "to use a canned rule instead of the live CrewAI extraction/audit agents."
            )
        try:
            audited = await extract_and_audit_clause(chunk, settings=settings)
        except Exception as exc:  # noqa: BLE001 - surface any agent/LLM failure as a diagnosed pipeline error
            raise PipelineError(f"Extraction/audit agent pipeline failed: {exc!r}") from exc

    table = Table(show_header=False, box=None)
    table.add_row("Rule ID", audited.rule.rule_id)
    table.add_row("Obligation", audited.rule.obligation_type.value)
    table.add_row("Extraction confidence", f"{audited.rule.extraction_confidence:.2f}")
    table.add_row("Thresholds extracted", str(len(audited.rule.deterministic_logic)))
    table.add_row("Audit verdict", audited.audit.verdict.value)
    table.add_row("Audit fidelity", f"{audited.audit.fidelity_score:.2f}")
    console.print(table)

    _ok("Extraction + Logic Audit complete.")
    return audited


def step_compile(audited: AuditedComplianceRule) -> CompilationResult:
    _step_header(3)
    result = compile_audited_rule(audited)

    if not result.compiled or result.rego is None:
        blocking = [f for f in result.hitl_flags if f.severity.value == "blocking"]
        lines = "\n".join(f"  - [{f.reason_code.value}] {f.description}" for f in blocking)
        raise PipelineError(
            f"Compilation blocked by {len(blocking)} blocking HITL flag(s); routed for human review, not compiled:\n{lines}"
        )

    console.print(Syntax(result.rego.rego_code, "text", theme="ansi_dark", line_numbers=True))
    if result.hitl_flags:
        console.print(f"[yellow]{len(result.hitl_flags)} advisory HITL flag(s) accompany this compile (non-blocking).[/yellow]")
    _ok(f"Compiled package '[bold]{result.rego.package}[/bold]' ({result.rego.thresholds_compiled} threshold(s)).")
    return result


async def step_deploy(compiled: CompilationResult, opa_url: str, timeout: float) -> OPAEngine:
    _step_header(4)
    assert compiled.rego is not None
    engine = OPAEngine(base_url=opa_url, timeout_seconds=timeout)
    console.print(f"OPA server: [cyan]{opa_url}[/cyan]")
    try:
        await engine.publish_policy(compiled.rego)
    except OPAEngineError as exc:
        raise PipelineError(f"Failed to publish policy to OPA: {exc}") from exc
    _ok(f"Policy '{compiled.rego.rule_id}' published to OPA's Policy API (hot-loaded, no restart).")
    return engine


async def step_simulate(
    engine: OPAEngine,
    compiled: CompilationResult,
    audited: AuditedComplianceRule,
    entity_type: str,
    facts_override: dict[str, Any] | None,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    _step_header(5)
    assert compiled.rego is not None
    facts = facts_override if facts_override is not None else _default_violating_facts(audited.rule.deterministic_logic)
    input_doc = {"entity_type": entity_type, "facts": facts}

    console.print(Syntax(json.dumps(input_doc, indent=2), "json", theme="ansi_dark"))
    try:
        result = await engine.evaluate(compiled.rego.package, input_doc)
    except OPAEngineError as exc:
        raise PipelineError(f"OPA evaluation failed: {exc}") from exc

    decision, _ = _map_opa_result(result)
    style = _DECISION_STYLE[decision]
    console.print(Panel(json.dumps(result, indent=2) if result else "null (undefined)", title=f"[{style}]Decision: {decision.value.upper()}[/{style}]", border_style=style.split()[-1]))
    return input_doc, result


async def step_commit_ledger(
    ledger_database_url: str,
    audited: AuditedComplianceRule,
    input_doc: dict[str, Any],
    opa_result: dict[str, Any] | None,
    broker_id: str,
) -> tuple[LedgerEntry, ChainVerificationResult]:
    _step_header(6)
    decision, outcome = _map_opa_result(opa_result)
    transaction_id = f"CLI-{uuid.uuid4().hex[:12]}"
    hitl_review_id = f"cli-manual-{uuid.uuid4().hex[:8]}" if outcome == EvaluationOutcome.HITL_REVIEW else None

    event = ComplianceEvaluationEvent(
        broker_id=broker_id,
        transaction_id=transaction_id,
        evaluated_at=dt.datetime.now(dt.timezone.utc),
        circular_id=audited.rule.circular_number or "unknown",
        clause_hash=audited.rule.source_sha256,
        section_reference=audited.rule.clause_number or "unscoped",
        rule_id=audited.rule.rule_id,
        evaluation_result=outcome,
        hitl_review_id=hitl_review_id,
        details={"input": input_doc, "opa_result": opa_result, "decision": decision.value},
    )

    console.print(f"Ledger: [cyan]{ledger_database_url}[/cyan]")
    engine = create_async_engine(ledger_database_url, pool_pre_ping=True)
    try:
        service = LedgerService(engine)
        try:
            entry = await service.append_entry(event)
        except Exception as exc:  # noqa: BLE001 - surface any DB/connectivity failure as a diagnosed pipeline error
            raise PipelineError(f"Failed to append audit ledger entry: {exc!r}") from exc

        table = Table(show_header=False, box=None)
        table.add_row("sequence_num", str(entry.sequence_num))
        table.add_row("transaction_id", entry.transaction_id)
        table.add_row("payload_digest", entry.payload_digest)
        table.add_row("previous_hash", entry.previous_hash)
        table.add_row("current_hash", entry.current_hash)
        console.print(table)
        _ok(f"Ledger entry #{entry.sequence_num} committed.")

        console.print("Verifying full chain integrity...")
        verification = await verify_chain(engine)
        if verification.valid:
            _ok(f"Chain VALID over {verification.entries_checked} entries (sequence 0..{entry.sequence_num}).")
        else:
            console.print(f"[bold red]Chain INVALID: {len(verification.breaks)} break(s) detected![/bold red]")
            for b in verification.breaks:
                console.print(f"  [red]seq {b.sequence_num}: {b.reason}[/red]")

        return entry, verification
    finally:
        await engine.dispose()


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------


async def run_pipeline(
    *,
    pdf_source: str | None,
    offline_agents: bool,
    opa_url: str,
    opa_timeout: float,
    ledger_database_url: str,
    entity_type: str,
    broker_id: str,
    facts_override: dict[str, Any] | None,
    dry_run: bool,
) -> int:
    settings = Settings(opa_server_url=opa_url, ledger_database_url=ledger_database_url)

    console.rule("[bold magenta]RegEngine AI -- End-to-End Pipeline[/bold magenta]", style="magenta")

    _, chunk = await step_ingest(pdf_source, settings)
    audited = await step_extract_and_audit(chunk, settings, offline_agents)
    compiled = step_compile(audited)

    if dry_run:
        console.rule("[bold]Dry run complete[/bold] (steps 4-6 skipped)", style="magenta")
        return 0

    opa_engine = await step_deploy(compiled, opa_url, opa_timeout)
    input_doc, opa_result = await step_simulate(opa_engine, compiled, audited, entity_type, facts_override)
    entry, verification = await step_commit_ledger(ledger_database_url, audited, input_doc, opa_result, broker_id)

    decision, _ = _map_opa_result(opa_result)
    style = _DECISION_STYLE[decision]
    summary = (
        f"Rule       : {audited.rule.rule_id}\n"
        f"Decision   : [{style}]{decision.value.upper()}[/{style}]\n"
        f"Ledger seq : {entry.sequence_num}\n"
        f"Chain      : {'[bold green]VALID[/bold green]' if verification.valid else '[bold red]INVALID[/bold red]'}"
    )
    console.print(Panel(summary, title="[bold magenta]Pipeline Summary[/bold magenta]", border_style="magenta"))

    return 0 if verification.valid else 2


# --------------------------------------------------------------------------
# CLI entrypoint
# --------------------------------------------------------------------------


@click.command(context_settings={"help_option_names": ["-h", "--help"]})
@click.option(
    "--pdf", "pdf_source", default=None,
    help="Local file path or http(s) URL to a SEBI circular PDF. Omit to use a built-in synthetic sample.",
)
@click.option(
    "--offline-agents", is_flag=True, default=False,
    help="Skip the live CrewAI/Anthropic extraction+audit agents and use a canned, deterministic "
         "'Upfront Margin >= 20%' rule instead. Use this when ANTHROPIC_API_KEY isn't configured.",
)
@click.option("--opa-url", default="http://localhost:8181", show_default=True, help="OPA server base URL.")
@click.option("--opa-timeout", default=5.0, show_default=True, help="OPA HTTP request timeout, in seconds.")
@click.option(
    "--ledger-database-url", default="postgresql+asyncpg://regengine_ledger_writer:changeme@localhost:5432/regengine",
    show_default=True, help="Async SQLAlchemy DSN for the audit ledger.",
)
@click.option("--entity-type", default="Stockbroker", show_default=True, help="Simulated transaction's entity_type.")
@click.option("--broker-id", default="BRK-DEMO-001", show_default=True, help="Simulated transaction's broker_id.")
@click.option(
    "--facts", "facts_json", default=None,
    help='JSON object of transaction facts, e.g. \'{"upfront_margin_pct": 25}\'. Defaults to values chosen to '
         "violate every extracted threshold (demonstrates a DENY); override to demonstrate an ALLOW.",
)
@click.option("--dry-run", is_flag=True, default=False, help="Stop after compiling the Rego policy (step 3); never touches OPA or the ledger.")
def main(
    pdf_source: str | None,
    offline_agents: bool,
    opa_url: str,
    opa_timeout: float,
    ledger_database_url: str,
    entity_type: str,
    broker_id: str,
    facts_json: str | None,
    dry_run: bool,
) -> None:
    """Run RegEngine AI's entire pipeline -- ingest, extract, compile,
    deploy, simulate, and audit -- in a single command."""
    facts_override: dict[str, Any] | None = None
    if facts_json is not None:
        try:
            facts_override = json.loads(facts_json)
        except json.JSONDecodeError as exc:
            raise click.BadParameter(f"--facts must be valid JSON: {exc}") from exc
        if not isinstance(facts_override, dict):
            raise click.BadParameter("--facts must be a JSON object.")

    try:
        exit_code = asyncio.run(
            run_pipeline(
                pdf_source=pdf_source,
                offline_agents=offline_agents,
                opa_url=opa_url,
                opa_timeout=opa_timeout,
                ledger_database_url=ledger_database_url,
                entity_type=entity_type,
                broker_id=broker_id,
                facts_override=facts_override,
                dry_run=dry_run,
            )
        )
    except PipelineError as exc:
        console.print(Panel(str(exc.message), title="[bold red]Pipeline failed[/bold red]", border_style="red"))
        sys.exit(1)

    sys.exit(exit_code)


if __name__ == "__main__":
    main()
