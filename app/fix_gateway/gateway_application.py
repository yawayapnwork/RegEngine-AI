"""QuickFIX/Python integration: a `quickfix.Application` that intercepts
New Order Single (35=D) messages and replies with an immediate
Execution Report (35=8), calling `app.fix_gateway.evaluator.validate_new_order`
against the native-kernel-loaded policy set for the connecting session's
configured entity_type.

IMPORTANT -- read before treating this as the sub-500-microsecond path:
`native/bindings/pybind_module.cpp`'s own docstring is explicit that a
pybind11 call pays real costs (GIL, argument marshaling, interpreter
call-frame overhead) a direct C++/C-FFI call does not, and QuickFIX's
own Python message parsing adds further overhead this module doesn't
control. Use `native/include/regengine/fix_gateway.h` directly from a
co-located C++ OMS/RMS for the literal sub-500-microsecond target; use
THIS integration where a Python-hosted OMS's own per-order latency
budget makes that overhead genuinely negligible (see this package's
`__init__.py` for the full rationale), or for session/admin handling
and testing regardless of which hot path validates the order.

`quickfix` is not a dependency of the main application (see
requirements.txt -- it is intentionally absent, matching every other
optional-native-toolchain dependency in this codebase) and requires a
working C++ build toolchain to install; this module is importable
without it (for testing the surrounding logic), but `RegEngineFixApplication`
can only be INSTANTIATED once it's actually installed.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from app.fix_gateway.evaluator import validate_new_order
from app.fix_gateway.fix_scanner import FixScanError
from app.fix_gateway.models import ParsedOrder, ValidationOutcome
from app.fix_gateway.policy_manifest import LoadedFixPolicy
from app.fix_gateway.tags import (
    ACCOUNT,
    CL_ORD_ID,
    CUM_QTY,
    EXEC_ID,
    EXEC_TYPE,
    LEAVES_QTY,
    MSG_SEQ_NUM,
    ORDER_ID,
    ORDER_QTY,
    ORD_REJ_REASON,
    ORD_STATUS,
    PRICE,
    SEBI_CLAUSE_REF,
    SENDER_COMP_ID,
    SENDING_TIME,
    SIDE,
    SYMBOL,
    TARGET_COMP_ID,
    TEXT,
    ExecType,
    OrdStatus,
)

logger = logging.getLogger(__name__)

try:
    import quickfix
except ImportError:  # pragma: no cover - optional, see this module's docstring
    quickfix = None  # type: ignore[assignment]


class PolicyProvider:
    """Resolves which loaded policies and which RegEngine `entity_type`
    hash apply to a given FIX SessionID -- a NewOrderSingle carries no
    `entity_type` tag at all (see native/include/regengine/fix_gateway.h's
    module comment); which kind of counterparty a session represents is
    a deployment-time configuration decision, not derivable from the
    wire message. A real deployment's implementation reads this from
    `settings.fix_gateway_session_entity_types` (session-qualifier ->
    entity_type string) and app.fix_gateway.hot_reload's currently-loaded
    policy set; this base implementation always returns the same fixed
    set, sufficient for a single-entity-type gateway instance (the
    common case: one FIX acceptor port per counterparty type)."""

    def __init__(self, policies: list[LoadedFixPolicy], entity_type_hash: int) -> None:
        self._policies = policies
        self._entity_type_hash = entity_type_hash

    def policies_for_session(self, session_id: object) -> tuple[list[LoadedFixPolicy], int]:
        del session_id  # unused in this single-entity-type base implementation
        return self._policies, self._entity_type_hash


def _build_parsed_order(message: "quickfix.Message", session_id: "quickfix.SessionID") -> ParsedOrder:
    """Translates a `quickfix.Message` into the QuickFIX-independent
    `ParsedOrder` -- the ONLY function in this module that touches the
    `quickfix.Message` object model, so a QuickFIX version upgrade only
    ever risks breaking this one function, never the validation logic
    itself (see models.py's module docstring)."""
    header = message.getHeader()

    def required(tag: int) -> str:
        try:
            return message.getField(tag)
        except quickfix.FieldNotFound as exc:
            raise FixScanError(f"Missing required tag {tag}.") from exc

    def optional(tag: int, default: str = "") -> str:
        try:
            return message.getField(tag)
        except quickfix.FieldNotFound:
            return default

    order_qty_raw = required(ORDER_QTY)
    price_raw = required(PRICE)
    try:
        order_qty = float(order_qty_raw)
        price = float(price_raw)
    except ValueError as exc:
        raise FixScanError(f"OrderQty/Price not parseable as plain decimals: {order_qty_raw!r}, {price_raw!r}") from exc

    return ParsedOrder(
        sender_comp_id=header.getField(SENDER_COMP_ID) if header.isSetField(SENDER_COMP_ID) else session_id.getSenderCompID(),
        target_comp_id=header.getField(TARGET_COMP_ID) if header.isSetField(TARGET_COMP_ID) else session_id.getTargetCompID(),
        msg_seq_num=header.getField(MSG_SEQ_NUM) if header.isSetField(MSG_SEQ_NUM) else "",
        cl_ord_id=required(CL_ORD_ID),
        account=optional(ACCOUNT),
        symbol=optional(SYMBOL),
        side=optional(SIDE),
        order_qty=order_qty,
        price=price,
        order_qty_raw=order_qty_raw,
        price_raw=price_raw,
    )


def _build_execution_report_message(order: ParsedOrder, outcome: ValidationOutcome, sender_comp_id: str) -> "quickfix.Message":
    report = quickfix.Message()
    header = report.getHeader()
    header.setField(quickfix.MsgType("8"))
    header.setField(quickfix.SenderCompID(sender_comp_id))
    header.setField(quickfix.TargetCompID(order.sender_comp_id))

    report.setField(quickfix.StringField(ORDER_ID, "NONE"))
    report.setField(quickfix.ClOrdID(order.cl_ord_id))
    report.setField(quickfix.StringField(EXEC_ID, str(uuid.uuid4())))
    report.setField(quickfix.ExecType(ExecType.NEW if outcome.accepted else ExecType.REJECTED))
    report.setField(quickfix.OrdStatus(OrdStatus.NEW if outcome.accepted else OrdStatus.REJECTED))
    if order.symbol:
        report.setField(quickfix.Symbol(order.symbol))
    if order.side:
        report.setField(quickfix.Side(order.side))
    report.setField(quickfix.OrderQty(float(order.order_qty_raw)))
    report.setField(quickfix.Price(float(order.price_raw)))
    report.setField(quickfix.StringField(CUM_QTY, "0"))
    report.setField(quickfix.StringField(LEAVES_QTY, order.order_qty_raw if outcome.accepted else "0"))

    if not outcome.accepted and outcome.violated_reason is not None:
        reason = outcome.violated_reason
        report.setField(quickfix.OrdRejReason(int(reason.ord_rej_reason)))
        report.setField(quickfix.Text(reason.text))
        report.setField(quickfix.StringField(SEBI_CLAUSE_REF, reason.clause_ref))

    return report


if quickfix is not None:

    class RegEngineFixApplication(quickfix.Application):
        """Requirement 1-3, wired to QuickFIX/Python's session layer.
        Every mutating method QuickFIX's `Application` interface
        requires is implemented, even the admin/session ones that do
        nothing beyond logging -- QuickFIX calls them unconditionally,
        and a missing override is a common, hard-to-diagnose source of
        a session that connects but never processes application
        messages."""

        def __init__(self, policy_provider: PolicyProvider, sender_comp_id: str) -> None:
            super().__init__()
            self._policy_provider = policy_provider
            self._sender_comp_id = sender_comp_id

        def onCreate(self, sessionID: "quickfix.SessionID") -> None:
            logger.info("FIX session created: %s", sessionID)

        def onLogon(self, sessionID: "quickfix.SessionID") -> None:
            logger.info("FIX session logged on: %s", sessionID)

        def onLogout(self, sessionID: "quickfix.SessionID") -> None:
            logger.info("FIX session logged out: %s", sessionID)

        def toAdmin(self, message: "quickfix.Message", sessionID: "quickfix.SessionID") -> None:
            del message, sessionID  # no admin-message customization (e.g. Logon credentials) needed for this gateway

        def toApp(self, message: "quickfix.Message", sessionID: "quickfix.SessionID") -> None:
            del message, sessionID  # nothing to do -- this gateway only ever sends Execution Reports built in fromApp

        def fromAdmin(self, message: "quickfix.Message", sessionID: "quickfix.SessionID") -> None:
            del message, sessionID

        def fromApp(self, message: "quickfix.Message", sessionID: "quickfix.SessionID") -> None:
            msg_type = message.getHeader().getField(35)
            if msg_type != "D":
                return  # not a New Order Single -- outside this gateway's declared scope, let QuickFIX's own dispatch/other handlers deal with it

            try:
                order = _build_parsed_order(message, sessionID)
            except FixScanError:
                logger.exception("Malformed New Order Single on session %s; no execution report sent (see this module's docstring on failing closed).", sessionID)
                return

            policies, entity_type_hash = self._policy_provider.policies_for_session(sessionID)
            outcome = validate_new_order(policies, order, entity_type_hash)

            report = _build_execution_report_message(order, outcome, self._sender_comp_id)
            quickfix.Session.sendToTarget(report, sessionID)

            if not outcome.accepted:
                logger.warning(
                    "Order %s (ClOrdID=%s) REJECTED: %s", order.cl_ord_id, order.cl_ord_id,
                    outcome.violated_reason.clause_ref if outcome.violated_reason else "unknown reason",
                )

else:  # pragma: no cover - exercised only when `quickfix` genuinely isn't installed

    class RegEngineFixApplication:  # type: ignore[no-redef]
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            raise ImportError(
                "The 'quickfix' package is required to instantiate RegEngineFixApplication. "
                "Install it (requires a working C++ build toolchain) and ensure `import quickfix` "
                "succeeds before enabling settings.fix_gateway_enabled."
            )


def utc_sending_time() -> str:
    """FIX UTCTimestamp format (YYYYMMDD-HH:MM:SS.sss) -- QuickFIX
    itself stamps SendingTime (52) automatically on send in most
    configurations, but this is provided for the raw-socket
    (execution_report.py) path, which has no such automatic behavior."""
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H:%M:%S.%f")[:-3]
