"""Plain, QuickFIX-independent data contracts -- deliberately decoupled
from `quickfix.Message` so the validation logic (fact resolution,
policy evaluation, rejection mapping) is unit-testable without QuickFIX
installed at all. `gateway_application.py`'s ONLY job is translating a
`quickfix.Message` into a `ParsedOrder` and a `ValidationOutcome` back
into a `quickfix.Message` -- everything else operates on these plain
dataclasses.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from app.fix_gateway.tags import OrdRejReason


@dataclass(frozen=True)
class ParsedOrder:
    """Requirement 1: the fields extracted from one New Order Single."""

    sender_comp_id: str
    target_comp_id: str
    msg_seq_num: str
    cl_ord_id: str       # Tag 11
    account: str         # Tag 1 -- "ClientID" in this domain's usage; see tags.py's ACCOUNT comment
    symbol: str
    side: str
    order_qty: float     # Tag 38
    price: float         # Tag 44
    order_qty_raw: str   # re-emitted verbatim in the execution report
    price_raw: str


class ScanError(str, Enum):
    """Mirrors native/include/regengine/fix_tag_scanner.h's ScanResult
    (minus kOk) -- a malformed New Order Single fails closed to a
    business reject, it is never silently dropped or silently accepted."""

    NOT_NEW_ORDER_SINGLE = "not_new_order_single"
    MISSING_REQUIRED_TAG = "missing_required_tag"
    MALFORMED_NUMBER = "malformed_number"


@dataclass(frozen=True)
class SebiRejectionReason:
    """Requirement 3: a compiled policy's rejection metadata, resolved
    ONCE per policy at load time (see policy_manifest.py) -- the hot
    path only ever reads this, never formats a string, matching
    native/include/regengine/fix_gateway.h's RejectionMeta."""

    ord_rej_reason: OrdRejReason
    sebi_circular_number: str
    sebi_clause_number: str
    text: str  # full human-readable message, INCLUDING the rendered threshold and clause citation

    @property
    def clause_ref(self) -> str:
        return f"{self.sebi_circular_number}:{self.sebi_clause_number}"


@dataclass(frozen=True)
class ValidationOutcome:
    accepted: bool
    violated_reason: SebiRejectionReason | None = None
    scan_error: ScanError | None = None
