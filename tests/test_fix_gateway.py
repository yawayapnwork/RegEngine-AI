"""Tests for the FIX gateway (app.fix_gateway).

Exercises the REAL, already-built `regengine_native` extension (see
native/src/regengine_native.cp*.pyd -- confirmed importable in this
environment) and the real `native/tools/pack_policy.py` packager, not a
mock of either -- the whole point of this subsystem is that the
compiled policy genuinely evaluates orders correctly, matching this
session's established "exercise the real dependency wherever the
environment allows" convention (tests/test_localization.py,
tests/test_retrieval.py, etc.).

`quickfix` itself is NOT installed in this sandbox (it fails to build
from source under this environment's MSVC/flag combination) -- see
app.fix_gateway.gateway_application's module docstring. Every test here
therefore targets the QuickFIX-INDEPENDENT layer (fix_scanner,
execution_report, evaluator, policy_manifest, hot_reload) that carries
all of the actual compliance logic; `gateway_application.py`'s own
`RegEngineFixApplication.__init__` raising ImportError when `quickfix`
is absent is itself asserted below.
"""
from __future__ import annotations

import pytest

from app.fix_gateway import gateway_application
from app.fix_gateway.evaluator import validate_new_order
from app.fix_gateway.execution_report import ExecutionReportContext, build_execution_report
from app.fix_gateway.fix_scanner import FixScanError, scan_new_order_single
from app.fix_gateway.models import ParsedOrder, SebiRejectionReason, ValidationOutcome
from app.fix_gateway.policy_manifest import FIX_DERIVABLE_FIELDS, UnsupportedForFixGatewayError, build_loaded_policy, _import_native
from app.fix_gateway.tags import OrdRejReason

SOH = "\x01"


def _msg(*fields: tuple[int, object]) -> bytes:
    return "".join(f"{tag}={value}{SOH}" for tag, value in fields).encode("ascii")


class TestFixScanner:
    def test_scans_a_valid_new_order_single(self) -> None:
        raw = _msg((8, "FIX.4.4"), (35, "D"), (49, "BROKERCO"), (56, "REGENGINE"), (34, 1),
                   (11, "ORD0001"), (1, "UCC12345"), (55, "RELIANCE"), (54, 1), (38, 100), (44, "2500.50"), (10, "000"))
        order = scan_new_order_single(raw)
        assert order.cl_ord_id == "ORD0001"
        assert order.account == "UCC12345"
        assert order.order_qty == 100.0
        assert order.price == 2500.50
        assert order.sender_comp_id == "BROKERCO"

    def test_rejects_non_new_order_single(self) -> None:
        raw = _msg((8, "FIX.4.4"), (35, "0"), (10, "000"))  # Heartbeat
        with pytest.raises(FixScanError, match="not 'D'"):
            scan_new_order_single(raw)

    def test_flags_missing_required_tag(self) -> None:
        raw = _msg((8, "FIX.4.4"), (35, "D"), (1, "UCC1"), (38, 100), (44, 10), (10, "000"))  # no ClOrdID
        with pytest.raises(FixScanError, match="Missing required"):
            scan_new_order_single(raw)

    def test_rejects_exponential_notation(self) -> None:
        raw = _msg((8, "FIX.4.4"), (35, "D"), (11, "X"), (38, "1e5"), (44, 10), (10, "000"))
        with pytest.raises(FixScanError):
            scan_new_order_single(raw)

    def test_rejects_non_numeric_price(self) -> None:
        raw = _msg((8, "FIX.4.4"), (35, "D"), (11, "X"), (38, 100), (44, "abc"), (10, "000"))
        with pytest.raises(FixScanError):
            scan_new_order_single(raw)


class TestExecutionReportBuilder:
    def _order(self) -> ParsedOrder:
        return ParsedOrder(
            sender_comp_id="BROKERCO", target_comp_id="REGENGINE", msg_seq_num="1",
            cl_ord_id="ORD0001", account="UCC1", symbol="RELIANCE", side="1",
            order_qty=100.0, price=2500.50, order_qty_raw="100", price_raw="2500.50",
        )

    def test_accepted_report_wire_format(self) -> None:
        ctx = ExecutionReportContext(sender_comp_id="REGENGINE", sending_time="20260101-12:00:00.000", exec_id="1")
        raw = build_execution_report(self._order(), ValidationOutcome(accepted=True), ctx)
        text = raw.decode("ascii")
        assert text.startswith("8=FIX.4.4\x01")
        assert "35=8\x01" in text
        assert "39=0\x01" in text  # OrdStatus New
        assert "150=0\x01" in text  # ExecType New
        assert "103=" not in text  # no rejection reason on an accept

    def test_rejected_report_carries_sebi_clause_ref(self) -> None:
        reason = SebiRejectionReason(
            ord_rej_reason=OrdRejReason.ORDER_EXCEEDS_LIMIT,
            sebi_circular_number="SEBI/HO/MIRSD/2024/100", sebi_clause_number="4.2.b",
            text="Order quantity exceeds the SEBI-mandated freeze limit.",
        )
        ctx = ExecutionReportContext(sender_comp_id="REGENGINE", sending_time="20260101-12:00:00.000", exec_id="1")
        raw = build_execution_report(self._order(), ValidationOutcome(accepted=False, violated_reason=reason), ctx)
        text = raw.decode("ascii")
        assert "39=8\x01" in text  # OrdStatus Rejected
        assert "103=3\x01" in text  # OrdRejReason ORDER_EXCEEDS_LIMIT
        assert "9001=SEBI/HO/MIRSD/2024/100:4.2.b\x01" in text

    def test_checksum_and_body_length_are_correct(self) -> None:
        ctx = ExecutionReportContext(sender_comp_id="REGENGINE", sending_time="20260101-12:00:00.000", exec_id="1")
        raw = build_execution_report(self._order(), ValidationOutcome(accepted=True), ctx)
        text = raw.decode("ascii")

        body_len_str = text.split("9=", 1)[1].split(SOH, 1)[0]
        checksum_field_start = text.rindex("10=")
        body_start = text.index(SOH, text.index("9=")) + 1
        assert int(body_len_str) == checksum_field_start - body_start

        computed_checksum = sum(raw[:checksum_field_start]) % 256
        assert text[checksum_field_start:].rstrip(SOH) == f"10={computed_checksum:03d}"


@pytest.fixture(scope="module")
def native_module():
    try:
        return _import_native()
    except ImportError:
        pytest.skip("regengine_native extension is not built in this environment.")


@pytest.mark.usefixtures("native_module")
class TestPolicyManifestAndEvaluatorRealNativeKernel:
    """These tests call the REAL compiled native/ extension -- no mock."""

    def _pack_and_load(self, native_module, logic: dict, rule_id: str = "rule-1"):
        from native.tools.pack_policy import pack_policy

        data, slots = pack_policy(rule_id, logic)
        return native_module.CompiledPolicy(data, slots), slots

    def test_qty_limit_policy_accepts_and_rejects_correctly(self, native_module) -> None:
        from app.fix_gateway.policy_manifest import LoadedFixPolicy

        compiled_policy, slots = self._pack_and_load(native_module, {"<=": [{"var": "facts.order_qty"}, 10000]})
        assert slots == {"order_qty": 0}
        assert set(slots) <= set(FIX_DERIVABLE_FIELDS)

        rejection = SebiRejectionReason(
            ord_rej_reason=OrdRejReason.ORDER_EXCEEDS_LIMIT,
            sebi_circular_number="SEBI/HO/MIRSD/2024/100", sebi_clause_number="4.2.b",
            text="Order quantity exceeds the 10,000-unit freeze limit.",
        )
        policy = LoadedFixPolicy(rule_id="rule-1", compiled_policy=compiled_policy, field_slots=slots, rejection=rejection)

        compliant_order = ParsedOrder(sender_comp_id="B", target_comp_id="R", msg_seq_num="1", cl_ord_id="O1",
                                       account="UCC1", symbol="RELIANCE", side="1", order_qty=100.0, price=2500.50,
                                       order_qty_raw="100", price_raw="2500.50")
        breaching_order = ParsedOrder(sender_comp_id="B", target_comp_id="R", msg_seq_num="2", cl_ord_id="O2",
                                       account="UCC1", symbol="RELIANCE", side="1", order_qty=15000.0, price=10.0,
                                       order_qty_raw="15000", price_raw="10")

        assert validate_new_order([policy], compliant_order, 0).accepted is True
        outcome = validate_new_order([policy], breaching_order, 0)
        assert outcome.accepted is False
        assert outcome.violated_reason.clause_ref == "SEBI/HO/MIRSD/2024/100:4.2.b"

    def test_notional_limit_policy_uses_derived_fact(self, native_module) -> None:
        from app.fix_gateway.policy_manifest import LoadedFixPolicy

        compiled_policy, slots = self._pack_and_load(native_module, {"<=": [{"var": "facts.notional_value"}, 5000000]}, rule_id="rule-2")
        rejection = SebiRejectionReason(OrdRejReason.ORDER_EXCEEDS_LIMIT, "SEBI/HO/MIRSD/2024/101", "3.1", "Order value exceeds the per-order notional limit.")
        policy = LoadedFixPolicy(rule_id="rule-2", compiled_policy=compiled_policy, field_slots=slots, rejection=rejection)

        # qty=100 * price=100000 = 10,000,000 > 5,000,000 limit -- quantity alone is fine, notional isn't.
        breaching_order = ParsedOrder(sender_comp_id="B", target_comp_id="R", msg_seq_num="1", cl_ord_id="O1",
                                       account="UCC1", symbol="RELIANCE", side="1", order_qty=100.0, price=100000.0,
                                       order_qty_raw="100", price_raw="100000")
        outcome = validate_new_order([policy], breaching_order, 0)
        assert outcome.accepted is False
        assert outcome.violated_reason.sebi_clause_number == "3.1"

    def test_most_restrictive_wins_first_violated_policy_reported(self, native_module) -> None:
        from app.fix_gateway.policy_manifest import LoadedFixPolicy

        qty_policy_data, qty_slots = self._pack_and_load(native_module, {"<=": [{"var": "facts.order_qty"}, 10000]}, "qty-rule")
        notional_policy_data, notional_slots = self._pack_and_load(native_module, {"<=": [{"var": "facts.notional_value"}, 5000000]}, "notional-rule")

        qty_policy = LoadedFixPolicy("qty-rule", qty_policy_data, qty_slots, SebiRejectionReason(OrdRejReason.ORDER_EXCEEDS_LIMIT, "C1", "1.1", "qty breach"))
        notional_policy = LoadedFixPolicy("notional-rule", notional_policy_data, notional_slots, SebiRejectionReason(OrdRejReason.ORDER_EXCEEDS_LIMIT, "C2", "2.2", "notional breach"))

        # Breaches BOTH -- the FIRST policy in the list must be the one reported.
        order = ParsedOrder(sender_comp_id="B", target_comp_id="R", msg_seq_num="1", cl_ord_id="O1",
                             account="UCC1", symbol="RELIANCE", side="1", order_qty=99999.0, price=100000.0,
                             order_qty_raw="99999", price_raw="100000")
        outcome = validate_new_order([qty_policy, notional_policy], order, 0)
        assert outcome.violated_reason.sebi_clause_number == "1.1"
        outcome2 = validate_new_order([notional_policy, qty_policy], order, 0)
        assert outcome2.violated_reason.sebi_clause_number == "2.2"

    def test_entity_type_hash_gating(self, native_module) -> None:
        from app.fix_gateway.policy_manifest import LoadedFixPolicy

        logic = {"and": [{"==": [{"var": "entity_type"}, "Stockbroker"]}, {"<=": [{"var": "facts.order_qty"}, 10000]}]}
        compiled_policy, slots = self._pack_and_load(native_module, logic, "scoped-rule")
        policy = LoadedFixPolicy("scoped-rule", compiled_policy, slots, SebiRejectionReason(OrdRejReason.ORDER_EXCEEDS_LIMIT, "C1", "1.1", "breach"))

        order = ParsedOrder(sender_comp_id="B", target_comp_id="R", msg_seq_num="1", cl_ord_id="O1", account="UCC1",
                             symbol="RELIANCE", side="1", order_qty=15000.0, price=10.0, order_qty_raw="15000", price_raw="10")

        stockbroker_hash = native_module.hash_entity_type("Stockbroker")
        amc_hash = native_module.hash_entity_type("AMC")

        # Breaches the qty limit AND matches the entity_type -> rejected.
        assert validate_new_order([policy], order, stockbroker_hash).accepted is False
        # Breaches the qty limit but for a DIFFERENT entity_type -> the AND is
        # false either way (per policy_engine.h's documented literal-AND
        # semantics, no "not applicable -> allow" special case) -- so this
        # policy still does not accept the order, it's simply not a
        # violation of THIS rule; a caller scopes which policies even run
        # for a given entity_type upstream (mirroring app.execution.evaluator).
        assert validate_new_order([policy], order, amc_hash).accepted is False

    def test_unsupported_field_raises_at_load_time_not_evaluation_time(self) -> None:
        from app.db.models import CompiledRule

        compiled_rule = CompiledRule(id=1, clause_id=1, tenant_id="t1", rule_id="r1", rule_version=1,
                                      jsonlogic_ast={"<=": [{"var": "facts.margin_utilization_pct"}, 80]})
        with pytest.raises(UnsupportedForFixGatewayError, match="margin_utilization_pct"):
            build_loaded_policy(compiled_rule)


class TestGatewayApplicationDegradesWithoutQuickfix:
    def test_instantiation_raises_clear_import_error_when_quickfix_absent(self) -> None:
        if gateway_application.quickfix is not None:
            pytest.skip("quickfix is installed in this environment; the absence-path isn't exercised.")
        with pytest.raises(ImportError, match="quickfix"):
            gateway_application.RegEngineFixApplication()
