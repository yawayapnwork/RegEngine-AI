"""Requirement 3's wire-format output, pure Python -- the direct analog
of native/include/regengine/fix_execution_report.h, for the
non-QuickFIX (raw-socket) Python gateway path. The QuickFIX/Python path
(gateway_application.py) builds a `quickfix.Message` instead, which
QuickFIX itself serializes (including BodyLength/CheckSum) -- this
module exists for a deployment that talks raw FIX wire bytes directly,
matching fix_scanner.py's raw-parsing counterpart.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.fix_gateway.models import ParsedOrder, ValidationOutcome
from app.fix_gateway.tags import (
    ACCOUNT,
    BEGIN_STRING,
    BODY_LENGTH,
    CHECK_SUM,
    CL_ORD_ID,
    CUM_QTY,
    EXEC_ID,
    EXEC_TYPE,
    LEAVES_QTY,
    MSG_TYPE,
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
    MsgType,
    OrdStatus,
)

SOH = "\x01"


@dataclass(frozen=True)
class ExecutionReportContext:
    """Everything the builder needs that isn't already on `ParsedOrder`/
    `ValidationOutcome` -- kept explicit rather than pulled from global
    state, so this function has no hidden clock/ID-generator dependency
    (the caller supplies `sending_time`/`exec_id`), matching
    fix_execution_report.h's ExecutionReportInput."""

    sender_comp_id: str
    sending_time: str
    exec_id: str


def _field(tag: int, value: object) -> str:
    return f"{tag}={value}{SOH}"


def build_execution_report(order: ParsedOrder, outcome: ValidationOutcome, ctx: ExecutionReportContext) -> bytes:
    body = "".join([
        _field(MSG_TYPE, MsgType.EXECUTION_REPORT),
        _field(SENDER_COMP_ID, ctx.sender_comp_id),
        _field(TARGET_COMP_ID, order.sender_comp_id),  # replying TO the original sender
        _field(SENDING_TIME, ctx.sending_time),
        _field(ORDER_ID, "NONE"),  # no venue order ID exists at (pre-)validation time
        _field(CL_ORD_ID, order.cl_ord_id),
        _field(EXEC_ID, ctx.exec_id),
        _field(EXEC_TYPE, ExecType.NEW if outcome.accepted else ExecType.REJECTED),
        _field(ORD_STATUS, OrdStatus.NEW if outcome.accepted else OrdStatus.REJECTED),
        _field(SYMBOL, order.symbol),
        _field(SIDE, order.side),
        _field(ACCOUNT, order.account),
        _field(ORDER_QTY, order.order_qty_raw),
        _field(PRICE, order.price_raw),
        _field(CUM_QTY, 0),
        _field(LEAVES_QTY, order.order_qty_raw if outcome.accepted else 0),
    ])
    if not outcome.accepted and outcome.violated_reason is not None:
        reason = outcome.violated_reason
        body += "".join([
            _field(ORD_REJ_REASON, int(reason.ord_rej_reason)),
            _field(TEXT, reason.text),
            _field(SEBI_CLAUSE_REF, reason.clause_ref),
        ])

    body_bytes = body.encode("ascii")
    header = _field(BEGIN_STRING, "FIX.4.4") + _field(BODY_LENGTH, len(body_bytes))
    message_without_checksum = header.encode("ascii") + body_bytes

    checksum = sum(message_without_checksum) % 256
    trailer = _field(CHECK_SUM, f"{checksum:03d}")
    return message_without_checksum + trailer.encode("ascii")


__all__ = ["ExecutionReportContext", "build_execution_report"]
