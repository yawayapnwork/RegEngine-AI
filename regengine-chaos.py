#!/usr/bin/env python
"""regengine-chaos -- Compliance Chaos Monkey CLI.

Runs application-level fault injection (chaos/monkey/) against THIS
process's own imported copy of RegEngine AI's code -- a corrupted
policy operator, a simulated network dropout mid hash-chain write, and
a malformed/truncated SEBI PDF feed -- and confirms each is caught or
safely contained, then writes an automated post-mortem (Markdown +
JSON) to `settings.chaos_monkey_postmortem_dir`.

Distinct from chaos/experiments (K8s Chaos Mesh/Litmus specs) and
chaos/load (external HTTP load generators): this tool exercises
in-process application logic directly and needs no running OPA server,
Postgres instance, or Kubernetes cluster -- it uses an in-memory SQLite
ledger and real (non-network) compiler/parsing code paths. It refuses
to run unless `CHAOS_MONKEY_ENABLED=true` is set, by design -- see
app.config.Settings.chaos_monkey_enabled's docstring.

Usage:
    CHAOS_MONKEY_ENABLED=true python regengine-chaos.py
    CHAOS_MONKEY_ENABLED=true python regengine-chaos.py --output-dir ./chaos/postmortems
"""
from __future__ import annotations

import asyncio
import sys

import typer
from rich.console import Console
from rich.table import Table

from app.config import get_settings
from chaos.monkey.runner import ChaosMonkeyDisabledError, ChaosMonkeyRunner

app = typer.Typer(add_completion=False, help="Compliance Chaos Monkey -- application-level fault injection for staging.")
console = Console()


@app.command()
def run(
    output_dir: str = typer.Option(None, help="Override settings.chaos_monkey_postmortem_dir for this run."),
    fail_on_defect: bool = typer.Option(True, help="Exit non-zero if any scenario found an uncaught fault (recommended for CI)."),
) -> None:
    """Run all three chaos scenarios once and print + persist the post-mortem."""
    settings = get_settings()
    if output_dir:
        settings.chaos_monkey_postmortem_dir = output_dir

    runner = ChaosMonkeyRunner(settings)
    try:
        report = asyncio.run(runner.run_all())
    except ChaosMonkeyDisabledError as exc:
        console.print(f"[bold red]Refused to run:[/bold red] {exc}")
        raise typer.Exit(code=2) from exc

    table = Table(title=f"Chaos Monkey Run {report.run_id}")
    table.add_column("Scenario")
    table.add_column("Result")
    table.add_column("Summary")
    for r in report.results:
        style = "green" if r.passed else "bold red"
        table.add_row(r.title, f"[{style}]{'PASS' if r.passed else 'FAIL'}[/{style}]", r.summary)
    console.print(table)

    if not report.all_passed and fail_on_defect:
        console.print("[bold red]One or more scenarios found an uncaught fault -- see the post-mortem for detail.[/bold red]")
        raise typer.Exit(code=1)


if __name__ == "__main__":
    app()
