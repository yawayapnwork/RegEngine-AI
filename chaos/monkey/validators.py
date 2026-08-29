"""Validation hooks: one function per chaos scenario, each returning a
`chaos.monkey.results.ChaosCheckResult`. Every check below runs against
REAL production code (app.compiler, app.ledger, app.parsing,
app.execution) with only the fault itself simulated -- see each
scenario's docstring for exactly what is real and what is injected.
"""
from __future__ import annotations

import datetime as dt

from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.compiler.hitl import has_blocking_flags
from app.compiler.jsonlogic_validator import validate_json_logic_ast
from app.compiler.pipeline import compile_audited_rule
from app.execution.models import Decision, EvaluationResult, PolicyOutcome, SourceChannel, TransactionPayload
from app.ledger.integration import log_evaluation
from app.ledger.models import ComplianceEvaluationEvent, EvaluationOutcome, compliance_audit_ledger
from app.ledger.service import LedgerService
from app.ledger.verifier import verify_chain
from app.observability.metrics import REGISTRY
from app.parsing import extractor as extractor_module
from app.parsing.exceptions import ParsingError
from chaos.monkey.fidelity_check import build_audit_from_fidelity_check, check_operator_fidelity
from chaos.monkey.mutators import corrupt_compiled_jsonlogic_operators, flip_audited_rule_operator
from chaos.monkey.network_faults import NetworkDropoutEngine, NetworkDropoutFault
from chaos.monkey.pdf_faults import empty_bytes, not_a_pdf_bytes, truncated_pdf_bytes
from chaos.monkey.regression import build_boundary_fixture, run_regression_check
from chaos.monkey.results import ChaosCheckResult
from chaos.monkey.scenarios import approved_margin_rule


# ---------------------------------------------------------------------------
# Scenario 1 -- corrupted policy logic
# ---------------------------------------------------------------------------


def run_scenario_corrupted_policy_logic() -> ChaosCheckResult:
    """Injects an operator swap (`>=` -> `<=`) at two different points and
    confirms each has a real defense that catches it:

    1. SOURCE level (before compilation): the mutated rule's operator no
       longer matches its own `verbatim_evidence` wording. A deterministic
       proxy for the Logic Auditor Agent's fidelity check
       (chaos.monkey.fidelity_check -- see its docstring on why this is a
       proxy, not the real LLM agent) should reject it, and that
       rejection, fed into the REAL `app.compiler.pipeline.compile_audited_rule`
       / `app.compiler.hitl` gate, should block compilation outright.
    2. COMPILED-AST level (after compilation): the same swap applied
       directly to an already-compiled JSON-Logic AST is structurally
       invisible to `validate_json_logic_ast` (confirmed below) -- the
       real defense there is a golden-fixture regression replay
       (chaos.monkey.regression, using the real
       `app.backtest.jsonlogic_evaluator`), which must detect that the
       mutation flips a known fixture's outcome.
    """
    baseline = approved_margin_rule()
    threshold = baseline.rule.deterministic_logic[0]

    mutated_audited, mutation = flip_audited_rule_operator(baseline)

    # --- Layer 1: source-level fidelity check + real compilation gate ---
    fidelity_result = check_operator_fidelity(mutated_audited.rule.deterministic_logic[0])
    proxy_audit = build_audit_from_fidelity_check(mutated_audited.rule.rule_id, [fidelity_result])
    corrupted_audited = mutated_audited.model_copy(update={"audit": proxy_audit})

    compile_result = compile_audited_rule(corrupted_audited)
    source_level_caught = fidelity_result.mismatch and not compile_result.compiled and has_blocking_flags(compile_result.hitl_flags)

    # --- Layer 2: compiled-AST-level mutation vs. structural validation ---
    original_compiled = compile_audited_rule(baseline)
    assert original_compiled.compiled and original_compiled.json_logic is not None  # baseline fixture must compile cleanly

    original_logic = original_compiled.json_logic.logic
    mutated_logic = corrupt_compiled_jsonlogic_operators(original_logic)

    structural_validation_missed_it = False
    try:
        validate_json_logic_ast(mutated_logic)
        structural_validation_missed_it = True  # expected: structurally valid, so no exception
    except Exception:  # noqa: BLE001 - would mean the corruption WAS structurally invalid, which is not this scenario
        structural_validation_missed_it = False

    fixture = build_boundary_fixture(threshold)
    regression = run_regression_check(original_logic, mutated_logic, fixture)
    ast_level_caught = regression.regression_detected and regression.original_matches_expected

    passed = source_level_caught and structural_validation_missed_it and ast_level_caught

    summary = (
        "Both defenses caught the injected operator swap: the fidelity-check-driven audit gate blocked "
        "compilation, and the golden-fixture regression replay detected the compiled-AST-level mutation."
        if passed
        else "One or more defenses FAILED to catch the injected operator swap -- see evidence for which layer."
    )

    return ChaosCheckResult(
        scenario_id="corrupted-policy-logic",
        title="Injected operator swap (>= -> <=) into a compiled policy",
        passed=passed,
        summary=summary,
        evidence={
            "original_operator": mutation.original_operator.value,
            "mutated_operator": mutation.mutated_operator.value,
            "verbatim_evidence": mutation.verbatim_evidence,
            "source_level": {
                "fidelity_mismatch_detected": fidelity_result.mismatch,
                "implied_operator_family": fidelity_result.implied_family,
                "matched_phrase": fidelity_result.matched_phrase,
                "compilation_blocked": not compile_result.compiled,
                "blocking_hitl_flags": [f.reason_code.value for f in compile_result.hitl_flags if f.severity.value == "blocking"],
            },
            "ast_level": {
                "structural_validation_missed_it": structural_validation_missed_it,
                "original_logic": original_logic,
                "mutated_logic": mutated_logic,
                "fixture": fixture.description,
                "original_result": regression.original_result,
                "mutated_result": regression.mutated_result,
                "regression_detected": regression.regression_detected,
            },
        },
    )


# ---------------------------------------------------------------------------
# Scenario 2 -- network dropout mid hash-chain write
# ---------------------------------------------------------------------------


async def run_scenario_ledger_network_dropout(engine: AsyncEngine | None = None) -> ChaosCheckResult:
    """Injects a simulated network dropout on the INSERT statement of a
    real `LedgerService.append_entry` transaction (against a real,
    in-memory SQLite engine unless `engine` is supplied) and confirms:

    1. The dropout is surfaced to the caller (never silently swallowed
       into a false "written" result).
    2. The hash chain is left with NO partial/corrupt row -- `verify_chain`
       (the real chain-integrity verifier) reports a clean, empty chain
       afterward, exactly as if the write attempt had never happened.
    3. `app.ledger.integration.log_evaluation`'s documented best-effort
       contract holds: a ledger outage during an evaluation's audit
       write does not raise back into the caller (a live compliance
       decision must never be blocked by an audit-trail durability
       failure -- see that module's docstring) and is observably counted
       via the real `AUDIT_LEDGER_WRITE_FAILURES_TOTAL` Prometheus metric,
       so the outage is never silent even though it isn't fatal.
    """
    own_engine = engine is None
    if own_engine:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as conn:
            await conn.run_sync(compliance_audit_ledger.metadata.create_all)

    fault_engine = NetworkDropoutEngine(engine, fail_on_call_index=2)  # SQLite skips the advisory-lock call, so call #2 is the INSERT
    service = LedgerService(fault_engine)

    event = ComplianceEvaluationEvent(
        broker_id="CHAOS_BRK0001",
        transaction_id="CHAOS_TXN0001",
        circular_id="SEBI/HO/MIRSD/DOP/CIR/P/2026/042",
        clause_hash="c" * 64,
        section_reference="4.2.b",
        rule_id="c" * 64 + ":4.2.b",
        evaluation_result=EvaluationOutcome.PASS,
    )

    fault_surfaced = False
    fault_type_correct = False
    try:
        await service.append_entry(event)
    except NetworkDropoutFault:
        fault_surfaced = True
        fault_type_correct = True
    except Exception:  # noqa: BLE001 - some OTHER exception leaked through, which is itself a finding
        fault_surfaced = True
        fault_type_correct = False

    chain_result = await verify_chain(engine)
    chain_clean = chain_result.valid and chain_result.entries_checked == 0

    metric_before = _counter_value("audit_ledger_write_failures_total")
    transaction = TransactionPayload(
        transaction_id="CHAOS_TXN0002",
        entity_type="Stockbroker",
        facts={"upfront_margin_pct": 25},
        source_channel=SourceChannel.REST_SYNC,
        broker_id="CHAOS_BRK0001",
    )
    eval_result = EvaluationResult(
        transaction_id=transaction.transaction_id,
        decision=Decision.ALLOW,
        matched_policies=[
            PolicyOutcome(rule_id="c" * 64 + ":4.2.b", package="sebi.broking.margin", allow=True, circular_number="SEBI/HO/MIRSD/DOP/CIR/P/2026/042", clause_number="4.2.b")
        ],
    )

    best_effort_raised = False
    try:
        await log_evaluation(service, transaction, eval_result)
    except Exception:  # noqa: BLE001 - log_evaluation raising at all is the failure this check watches for
        best_effort_raised = True

    metric_after = _counter_value("audit_ledger_write_failures_total")
    metric_incremented = metric_before is not None and metric_after is not None and metric_after > metric_before

    passed = fault_surfaced and fault_type_correct and chain_clean and not best_effort_raised and metric_incremented

    summary = (
        "Network dropout was surfaced, the hash chain remained clean with zero partial writes, and the "
        "best-effort ledger-write contract held (the live decision path was never blocked, and the "
        "outage was still counted, not silenced)."
        if passed
        else "The system did NOT fully fail safe under a mid-write network dropout -- see evidence."
    )

    result = ChaosCheckResult(
        scenario_id="ledger-network-dropout",
        title="Simulated network dropout mid hash-chain write",
        passed=passed,
        summary=summary,
        evidence={
            "fault_surfaced_to_append_entry_caller": fault_surfaced,
            "fault_was_the_injected_type": fault_type_correct,
            "chain_verification": {
                "valid": chain_result.valid,
                "entries_checked": chain_result.entries_checked,
                "breaks": [b.model_dump(mode="json") for b in chain_result.breaks],
            },
            "log_evaluation_best_effort_contract_held": not best_effort_raised,
            "audit_ledger_write_failures_total_incremented": metric_incremented,
        },
    )

    if own_engine:
        await engine.dispose()
    return result


def _counter_value(metric_name: str) -> float | None:
    for family in REGISTRY.collect():
        for sample in family.samples:
            if sample.name == metric_name:
                return sample.value
    return None


# ---------------------------------------------------------------------------
# Scenario 3 -- malformed / truncated SEBI PDF feed
# ---------------------------------------------------------------------------


def _partition_raises(*args, **kwargs):
    raise ValueError("Simulated parser failure on a truncated/corrupted PDF stream (chaos.monkey.pdf_faults).")


async def run_scenario_malformed_pdf_ingestion(settings) -> ChaosCheckResult:
    """Feeds three malformed inputs into the real `app.parsing.extractor.extract_pdf`:
    empty bytes, non-PDF bytes, and a truncated-but-header-valid PDF (with
    both extraction backends monkeypatched to raise the way
    `unstructured`/`tika` actually do on a corrupt stream, since neither
    heavy dependency is installed in this sandbox -- see
    chaos/monkey/pdf_faults.py's module docstring). A safe failure here
    means a typed `ParsingError` subclass every time -- never an
    unhandled exception, and never a silently-empty/garbage result
    treated as a successfully ingested circular."""
    checks: dict[str, dict] = {}

    for label, payload in (("empty_bytes", empty_bytes()), ("not_a_pdf_bytes", not_a_pdf_bytes())):
        raised_parsing_error = False
        exception_type = None
        try:
            await extractor_module.extract_pdf(file_bytes=payload, source_path=_dummy_path(), filename="chaos.pdf", settings=settings)
        except ParsingError as exc:
            raised_parsing_error = True
            exception_type = type(exc).__name__
        except Exception as exc:  # noqa: BLE001 - an unhandled crash is exactly what this check is watching for
            exception_type = type(exc).__name__
        checks[label] = {"raised_typed_parsing_error": raised_parsing_error, "exception_type": exception_type}

    original_unstructured = extractor_module._partition_with_unstructured
    original_tika = extractor_module._partition_with_tika
    extractor_module._partition_with_unstructured = _partition_raises
    extractor_module._partition_with_tika = _partition_raises
    try:
        raised_parsing_error = False
        exception_type = None
        try:
            await extractor_module.extract_pdf(file_bytes=truncated_pdf_bytes(), source_path=_dummy_path(), filename="chaos_truncated.pdf", settings=settings)
        except ParsingError as exc:
            raised_parsing_error = True
            exception_type = type(exc).__name__
        except Exception as exc:  # noqa: BLE001
            exception_type = type(exc).__name__
        checks["truncated_pdf_bytes"] = {"raised_typed_parsing_error": raised_parsing_error, "exception_type": exception_type}
    finally:
        extractor_module._partition_with_unstructured = original_unstructured
        extractor_module._partition_with_tika = original_tika

    passed = all(c["raised_typed_parsing_error"] for c in checks.values())

    return ChaosCheckResult(
        scenario_id="malformed-pdf-ingestion",
        title="Malformed/truncated SEBI PDF feed injected into the ingestion engine",
        passed=passed,
        summary=(
            "Every malformed input raised a typed ParsingError (safely DLQ-routable), with no unhandled crash "
            "and no silently-corrupted circular ingested."
            if passed
            else "At least one malformed input did NOT fail safely -- see evidence for which case and exception type."
        ),
        evidence=checks,
    )


def _dummy_path():
    from pathlib import Path

    return Path("chaos_injected.pdf")
