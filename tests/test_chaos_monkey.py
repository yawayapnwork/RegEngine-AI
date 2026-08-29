"""Tests for chaos/monkey/: the Compliance Chaos Monkey's injectors and
validators are exercised against real app.compiler/app.ledger/app.parsing
code (an in-memory SQLite ledger stands in for PostgreSQL, matching
tests/test_ledger.py's convention; app.parsing's two heavy extraction
backends are monkeypatched to raise the way they actually would on a
corrupt file, since neither is installed in this sandbox -- everything
else in extract_pdf runs unmodified).
"""
from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import Settings
from app.ledger.models import compliance_audit_ledger
from app.parsing.exceptions import ParsingError, UnsupportedFileError
from chaos.monkey.fidelity_check import check_operator_fidelity
from chaos.monkey.mutators import UnflippableOperatorError, corrupt_compiled_jsonlogic_operators, flip_threshold_operator
from chaos.monkey.network_faults import NetworkDropoutEngine, NetworkDropoutFault
from chaos.monkey.pdf_faults import empty_bytes, not_a_pdf_bytes, truncated_pdf_bytes
from chaos.monkey.postmortem import render_postmortem, write_postmortem
from chaos.monkey.regression import build_boundary_fixture, run_regression_check
from chaos.monkey.results import ChaosCheckResult, ChaosRunReport
from chaos.monkey.runner import ChaosMonkeyDisabledError, ChaosMonkeyRunner
from chaos.monkey.scenarios import approved_margin_rule
from chaos.monkey.validators import (
    run_scenario_corrupted_policy_logic,
    run_scenario_ledger_network_dropout,
    run_scenario_malformed_pdf_ingestion,
)


class TestMutators:
    def test_flip_threshold_operator_returns_new_object_unmutated_original(self) -> None:
        audited = approved_margin_rule()
        original_operator = audited.rule.deterministic_logic[0].operator

        mutated_rule, mutation = flip_threshold_operator(audited.rule)

        assert audited.rule.deterministic_logic[0].operator == original_operator  # original untouched
        assert mutated_rule.deterministic_logic[0].operator != original_operator
        assert mutation.original_operator == original_operator
        assert mutation.mutated_operator == mutated_rule.deterministic_logic[0].operator
        assert mutation.verbatim_evidence == audited.rule.deterministic_logic[0].verbatim_evidence

    def test_gte_flips_to_lte_and_back(self) -> None:
        from app.agents.schemas import ComparisonOperator

        audited = approved_margin_rule()
        assert audited.rule.deterministic_logic[0].operator == ComparisonOperator.GTE
        mutated, _ = flip_threshold_operator(audited.rule)
        assert mutated.deterministic_logic[0].operator == ComparisonOperator.LTE

    def test_unflippable_operator_raises(self) -> None:
        from app.agents.schemas import ComparisonOperator

        audited = approved_margin_rule()
        eq_threshold = audited.rule.deterministic_logic[0].model_copy(update={"operator": ComparisonOperator.EQ})
        rule = audited.rule.model_copy(update={"deterministic_logic": [eq_threshold]})
        with pytest.raises(UnflippableOperatorError):
            flip_threshold_operator(rule)

    def test_corrupt_compiled_jsonlogic_operators_swaps_in_place_shape(self) -> None:
        logic = {"and": [{"==": [{"var": "entity_type"}, "Stockbroker"]}, {">=": [{"var": "facts.upfront_margin_pct"}, 20]}]}
        mutated = corrupt_compiled_jsonlogic_operators(logic)
        assert mutated == {"and": [{"==": [{"var": "entity_type"}, "Stockbroker"]}, {"<=": [{"var": "facts.upfront_margin_pct"}, 20]}]}
        assert logic["and"][1] == {">=": [{"var": "facts.upfront_margin_pct"}, 20]}  # original untouched


class TestFidelityCheck:
    def test_matching_operator_and_wording_is_not_a_mismatch(self) -> None:
        audited = approved_margin_rule()
        result = check_operator_fidelity(audited.rule.deterministic_logic[0])
        assert result.mismatch is False
        assert result.implied_family == "gte"

    def test_flipped_operator_is_detected_as_mismatch(self) -> None:
        audited = approved_margin_rule()
        mutated, _ = flip_threshold_operator(audited.rule)
        result = check_operator_fidelity(mutated.deterministic_logic[0])
        assert result.mismatch is True
        assert result.implied_family == "gte"
        assert result.matched_phrase is not None


class TestRegressionCheck:
    def test_boundary_fixture_disagreement_is_detected(self) -> None:
        audited = approved_margin_rule()
        threshold = audited.rule.deterministic_logic[0]
        original_logic = {threshold.operator.value: [{"var": "facts.upfront_margin_pct"}, threshold.value]}
        mutated_logic = corrupt_compiled_jsonlogic_operators(original_logic)

        fixture = build_boundary_fixture(threshold)
        result = run_regression_check(original_logic, mutated_logic, fixture)

        assert result.original_matches_expected is True
        assert result.regression_detected is True


class TestNetworkDropoutEngine:
    @pytest_asyncio.fixture
    async def engine(self):
        eng = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with eng.begin() as conn:
            await conn.run_sync(compliance_audit_ledger.metadata.create_all)
        yield eng
        await eng.dispose()

    @pytest.mark.asyncio
    async def test_fault_engine_raises_on_configured_call_and_rolls_back(self, engine) -> None:
        fault_engine = NetworkDropoutEngine(engine, fail_on_call_index=1)
        with pytest.raises(NetworkDropoutFault):
            async with fault_engine.begin() as conn:
                from sqlalchemy import text

                await conn.execute(text("SELECT 1"))

    @pytest.mark.asyncio
    async def test_fault_engine_permits_calls_before_the_configured_index(self, engine) -> None:
        from sqlalchemy import text

        fault_engine = NetworkDropoutEngine(engine, fail_on_call_index=2)
        async with fault_engine.begin() as conn:
            result = await conn.execute(text("SELECT 1"))
            assert result.scalar() == 1
            with pytest.raises(NetworkDropoutFault):
                await conn.execute(text("SELECT 2"))


class TestPdfFaults:
    def test_empty_bytes_has_no_pdf_magic(self) -> None:
        assert empty_bytes() == b""

    def test_not_a_pdf_bytes_has_no_pdf_magic(self) -> None:
        assert b"%PDF-" not in not_a_pdf_bytes()

    def test_truncated_pdf_bytes_has_valid_header_but_no_eof_marker(self) -> None:
        data = truncated_pdf_bytes()
        assert data.startswith(b"%PDF-")
        assert b"%%EOF" not in data


@pytest.mark.asyncio
class TestScenarioValidators:
    async def test_corrupted_policy_logic_scenario_passes(self) -> None:
        result = run_scenario_corrupted_policy_logic()
        assert isinstance(result, ChaosCheckResult)
        assert result.passed is True
        assert result.evidence["source_level"]["fidelity_mismatch_detected"] is True
        assert result.evidence["source_level"]["compilation_blocked"] is True
        assert result.evidence["ast_level"]["structural_validation_missed_it"] is True
        assert result.evidence["ast_level"]["regression_detected"] is True

    async def test_ledger_network_dropout_scenario_passes(self) -> None:
        result = await run_scenario_ledger_network_dropout()
        assert result.passed is True
        assert result.evidence["fault_surfaced_to_append_entry_caller"] is True
        assert result.evidence["chain_verification"]["valid"] is True
        assert result.evidence["chain_verification"]["entries_checked"] == 0
        assert result.evidence["log_evaluation_best_effort_contract_held"] is True

    async def test_malformed_pdf_ingestion_scenario_passes(self) -> None:
        settings = Settings()
        result = await run_scenario_malformed_pdf_ingestion(settings)
        assert result.passed is True
        for case in result.evidence.values():
            assert case["raised_typed_parsing_error"] is True


class TestPostmortem:
    def test_render_postmortem_reports_overall_pass(self) -> None:
        import datetime as dt

        report = ChaosRunReport(
            run_id="test-run",
            started_at=dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc),
            finished_at=dt.datetime(2026, 1, 1, 0, 0, 5, tzinfo=dt.timezone.utc),
            results=[ChaosCheckResult(scenario_id="s1", title="Scenario One", passed=True, summary="ok", evidence={"k": "v"})],
        )
        text = render_postmortem(report)
        assert "PASS" in text
        assert "Scenario One" in text
        assert "Escaped defects" not in text

    def test_render_postmortem_reports_failures_distinctly(self) -> None:
        import datetime as dt

        report = ChaosRunReport(
            run_id="test-run-2",
            started_at=dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc),
            finished_at=dt.datetime(2026, 1, 1, 0, 0, 5, tzinfo=dt.timezone.utc),
            results=[ChaosCheckResult(scenario_id="s1", title="Broken Scenario", passed=False, summary="uh oh", evidence={})],
        )
        text = render_postmortem(report)
        assert "FAIL" in text
        assert "Escaped defects" in text

    def test_write_postmortem_creates_markdown_and_json(self, tmp_path) -> None:
        import datetime as dt

        report = ChaosRunReport(
            run_id="write-test",
            started_at=dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc),
            finished_at=dt.datetime(2026, 1, 1, 0, 0, 5, tzinfo=dt.timezone.utc),
            results=[ChaosCheckResult(scenario_id="s1", title="Scenario One", passed=True, summary="ok", evidence={})],
        )
        md_path = write_postmortem(report, str(tmp_path))
        assert md_path.exists()
        assert (tmp_path / "chaos-run-write-test.json").exists()


@pytest.mark.asyncio
class TestChaosMonkeyRunner:
    async def test_run_all_refuses_when_disabled(self) -> None:
        settings = Settings(chaos_monkey_enabled=False)
        runner = ChaosMonkeyRunner(settings)
        with pytest.raises(ChaosMonkeyDisabledError):
            await runner.run_all()

    async def test_run_all_runs_every_scenario_and_writes_postmortem_when_enabled(self, tmp_path) -> None:
        settings = Settings(chaos_monkey_enabled=True, chaos_monkey_postmortem_dir=str(tmp_path))
        runner = ChaosMonkeyRunner(settings)

        report = await runner.run_all()

        assert len(report.results) == 3
        assert report.all_passed is True
        assert list(tmp_path.glob(f"chaos-run-{report.run_id}.md"))
        assert list(tmp_path.glob(f"chaos-run-{report.run_id}.json"))
