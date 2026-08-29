"""Requirement 1's FIX tag parser, pure Python -- the direct analog of
native/include/regengine/fix_tag_scanner.h, for a Python-hosted gateway
that parses raw wire bytes itself rather than going through QuickFIX's
own message object (see gateway_application.py for the QuickFIX-based
alternative). Both paths converge on the same `ParsedOrder`.
"""
from __future__ import annotations

from app.fix_gateway.models import ParsedOrder
from app.fix_gateway.tags import ACCOUNT, CL_ORD_ID, MSG_SEQ_NUM, MSG_TYPE, ORDER_QTY, PRICE, SENDER_COMP_ID, SIDE, SYMBOL, TARGET_COMP_ID, MsgType

SOH = "\x01"


class FixScanError(ValueError):
    """Raised for anything that must fail closed rather than proceed to
    policy evaluation -- a malformed or incomplete New Order Single, or
    a message that isn't one at all."""


def _parse_plain_decimal(text: str) -> float:
    """Same restricted grammar as
    native/include/regengine/fix_tag_scanner.h::parse_plain_decimal:
    `-?[0-9]+(\\.[0-9]+)?`, no exponents. Python's own `float()` accepts
    far more (exponents, 'inf', 'nan', leading/trailing whitespace) than
    FIX's plain-decimal fields ever legitimately contain, so this
    gateway does not simply call `float(text)` -- a wire value like
    "nan" or "1e400" must be rejected, not silently parsed into a
    nonsensical comparison against a compiled threshold."""
    body = text[1:] if text.startswith("-") else text
    if not body or not all(c.isdigit() or c == "." for c in body) or body.count(".") > 1:
        raise FixScanError(f"{text!r} is not a plain FIX decimal.")
    if body.startswith(".") or body.endswith(".") or not any(c.isdigit() for c in body):
        raise FixScanError(f"{text!r} is not a plain FIX decimal.")
    return float(text)


def scan_new_order_single(raw: bytes) -> ParsedOrder:
    """Raises FixScanError for anything short of a complete, well-formed
    New Order Single -- never returns a partially-populated ParsedOrder."""
    fields: dict[int, str] = {}
    text = raw.decode("ascii", errors="strict")
    for field in text.split(SOH):
        if not field:
            continue
        tag_str, _, value = field.partition("=")
        if not tag_str.isdigit():
            continue  # malformed field -- skip rather than abort, mirroring the C++ scanner's leniency on a single bad field
        fields[int(tag_str)] = value

    if MSG_TYPE not in fields:
        raise FixScanError("No MsgType (35) tag present.")
    if fields[MSG_TYPE] != MsgType.NEW_ORDER_SINGLE:
        raise FixScanError(f"MsgType is {fields[MSG_TYPE]!r}, not {MsgType.NEW_ORDER_SINGLE!r} (New Order Single).")

    missing = [tag for tag in (CL_ORD_ID, ORDER_QTY, PRICE) if tag not in fields or fields[tag] == ""]
    if missing:
        raise FixScanError(f"Missing required tag(s): {missing}.")

    order_qty = _parse_plain_decimal(fields[ORDER_QTY])
    price = _parse_plain_decimal(fields[PRICE])

    return ParsedOrder(
        sender_comp_id=fields.get(SENDER_COMP_ID, ""),
        target_comp_id=fields.get(TARGET_COMP_ID, ""),
        msg_seq_num=fields.get(MSG_SEQ_NUM, ""),
        cl_ord_id=fields[CL_ORD_ID],
        account=fields.get(ACCOUNT, ""),
        symbol=fields.get(SYMBOL, ""),
        side=fields.get(SIDE, ""),
        order_qty=order_qty,
        price=price,
        order_qty_raw=fields[ORDER_QTY],
        price_raw=fields[PRICE],
    )


__all__ = ["scan_new_order_single", "FixScanError"]
