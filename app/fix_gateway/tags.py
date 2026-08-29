"""FIX 4.2/4.4 tag numbers and enumerated values this gateway reads or
writes -- the Python mirror of native/include/regengine/fix_tags.h,
kept in lock-step with it (both are plain data, no logic, so drift is
low-risk, but a change to one must be mirrored in the other).
"""
from __future__ import annotations

from enum import IntEnum

# --- Standard header tags ---
BEGIN_STRING = 8
BODY_LENGTH = 9
MSG_TYPE = 35
SENDER_COMP_ID = 49
TARGET_COMP_ID = 56
MSG_SEQ_NUM = 34
SENDING_TIME = 52
CHECK_SUM = 10

# --- New Order Single (35=D) tags Requirement 1 names ---
CL_ORD_ID = 11    # Requirement 1: unique order identifier assigned by the buy-side/OMS
ACCOUNT = 1       # Requirement 1's "ClientID" -- FIX's own name for Tag 1 is "Account"; Indian broker OMS
                  # integrations conventionally carry the client's Unique Client Code (UCC) here, which is
                  # this gateway's "ClientID" framing -- documented explicitly since Tag 1 is NOT literally
                  # named ClOrdID/ClientID in the FIX dictionary, and a reviewer cross-checking against the
                  # spec should not conclude this mapping is a mistake.
SYMBOL = 55
SIDE = 54
ORD_TYPE = 40
ORDER_QTY = 38    # Requirement 1
PRICE = 44        # Requirement 1

# --- Execution Report (35=8) tags this gateway writes ---
ORDER_ID = 37
EXEC_ID = 17
EXEC_TYPE = 150
ORD_STATUS = 39
CUM_QTY = 14
LEAVES_QTY = 151
TEXT = 58
ORD_REJ_REASON = 103  # Requirement 3

# RegEngine's own custom tag for the specific SEBI circular/clause a
# rejection is grounded in -- see native/include/regengine/fix_tags.h's
# comment on why this is a dedicated tag (in FIX's user-defined range)
# rather than overloading Text (58) or a standard field.
SEBI_CLAUSE_REF = 9001


class MsgType:
    NEW_ORDER_SINGLE = "D"
    EXECUTION_REPORT = "8"


class OrdStatus:
    NEW = "0"
    REJECTED = "8"


class ExecType:
    NEW = "0"
    REJECTED = "8"


class OrdRejReason(IntEnum):
    """Standard FIX 4.2/4.4 OrdRejReason (Tag 103) values this gateway
    can emit -- the nearest STANDARD reason each RegEngine denial maps
    to. This enum alone is never precise enough to cite regulation; the
    exact SEBI clause always additionally rides in SEBI_CLAUSE_REF + TEXT."""

    BROKER_EXCHANGE_OPTION = 0
    UNKNOWN_SYMBOL = 1
    EXCHANGE_CLOSED = 2
    ORDER_EXCEEDS_LIMIT = 3   # quantity/notional/price-band breaches map here
    TOO_LATE_TO_ENTER = 4
    DUPLICATE_ORDER = 6
    INCORRECT_QUANTITY = 13
    OTHER = 99                # any compliance denial without a closer-fitting standard code


# Tag -> human-readable name, purely for logging/debugging -- never used
# to build a wire message (see gateway_application.py, which always
# addresses fields by their numeric tag, matching QuickFIX's own API).
TAG_NAMES: dict[int, str] = {
    BEGIN_STRING: "BeginString", BODY_LENGTH: "BodyLength", MSG_TYPE: "MsgType",
    SENDER_COMP_ID: "SenderCompID", TARGET_COMP_ID: "TargetCompID", MSG_SEQ_NUM: "MsgSeqNum",
    SENDING_TIME: "SendingTime", CHECK_SUM: "CheckSum", CL_ORD_ID: "ClOrdID", ACCOUNT: "Account",
    SYMBOL: "Symbol", SIDE: "Side", ORD_TYPE: "OrdType", ORDER_QTY: "OrderQty", PRICE: "Price",
    ORDER_ID: "OrderID", EXEC_ID: "ExecID", EXEC_TYPE: "ExecType", ORD_STATUS: "OrdStatus",
    CUM_QTY: "CumQty", LEAVES_QTY: "LeavesQty", TEXT: "Text", ORD_REJ_REASON: "OrdRejReason",
    SEBI_CLAUSE_REF: "SebiClauseRef (RegEngine custom)",
}
