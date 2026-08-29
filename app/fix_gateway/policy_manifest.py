"""The cold path (Requirement 2's "embedded OPA Rego policies" made
fast enough): turns a compiled, DB-persisted policy into a loaded
native policy plus its resolved rejection metadata -- runs once per
policy publish/hot-reload (see hot_reload.py), NEVER per order.

Reuses `app.compiler.jsonlogic_compiler`'s output (the SAME
`jsonlogic_ast` a `CompiledRule` already carries -- see
`app.db.models.CompiledRule.jsonlogic_ast`) and `native/tools/pack_policy.py`
(the SAME packager `native/`'s own C++ loader tests were captured
against) rather than inventing a second policy representation. This
module's only real job is resolving each policy's FIX-derivable field
slots (order_qty, price, notional_value) and its SEBI clause citation --
concerns specific to the FIX gateway that neither the compiler nor the
native packager needs to know about.
"""
from __future__ import annotations

import logging
import sys
from dataclasses import dataclass
from pathlib import Path

from app.compiler.jsonlogic_compiler import compile_rule_to_jsonlogic
from app.db.models import CompiledRule
from app.fix_gateway.models import SebiRejectionReason
from app.fix_gateway.tags import OrdRejReason

logger = logging.getLogger(__name__)

_NATIVE_ROOT = Path(__file__).resolve().parent.parent.parent / "native"

# FIX-derivable field names this gateway can supply without any
# external (non-FIX) input -- must match native/include/regengine/fix_gateway.h's
# FactSource enum exactly (order matters: index here == the enum's
# integer value, since the packaged manifest's C++ consumer indexes by
# the same convention). "notional_value" is a DERIVED fact (order_qty *
# price), computed once per order by whichever integration builds the
# facts vector -- app.compiler.naming.metric_field_name never produces
# this name for a real extracted rule (no clause metric is literally
# named "Notional Value" today), so a rule referencing it is one this
# gateway's OWN example policies define, not something the LLM
# extraction pipeline would emit unprompted; a future extracted rule
# using the same field name would automatically wire up to this same
# slot, which is the intended, low-friction behavior.
FIX_DERIVABLE_FIELDS = ("order_qty", "price", "notional_value")


class UnsupportedForFixGatewayError(ValueError):
    """A compiled rule's JSON-Logic AST is either outside
    native/tools/pack_policy.py's restricted grammar (see that module's
    docstring) or references a fact this gateway cannot derive from a
    New Order Single alone and no external-facts overlay was supplied --
    raised at LOAD time, not per order, so a misconfigured rule is
    caught at policy-publish time, never silently mis-evaluated live."""


@dataclass(frozen=True)
class LoadedFixPolicy:
    """One compiled, packaged, ready-to-evaluate policy plus its
    resolved rejection metadata -- the Python-side equivalent of
    native/include/regengine/fix_gateway.h's PolicyBundle."""

    rule_id: str
    compiled_policy: "object"  # regengine_native.CompiledPolicy -- typed as object since the native extension is an optional, deferred import (see _import_native below)
    field_slots: dict[str, int]  # facts.<field> -> slot index, from pack_policy()
    rejection: SebiRejectionReason


def _import_native():
    """Deferred import, matching this codebase's established convention
    for optional heavy/native dependencies (e.g. app.localization.ocr's
    tesseract binding) -- a deployment that never enables
    `settings.fix_gateway_enabled` never needs the compiled extension
    installed. Adds `native/src` to `sys.path` since that's where this
    repo's own prebuilt extension lives (see native/setup.py); a
    packaged deployment would instead `pip install` a wheel built from
    native/ and skip this path entirely."""
    try:
        import regengine_native  # type: ignore[import-not-found]

        return regengine_native
    except ImportError:
        native_src = str(_NATIVE_ROOT / "src")
        if native_src not in sys.path:
            sys.path.insert(0, native_src)
        import regengine_native  # type: ignore[import-not-found]

        return regengine_native


def build_loaded_policy(compiled_rule: CompiledRule) -> LoadedFixPolicy:
    """`compiled_rule.clause` and `compiled_rule.clause.circular` must
    already be loaded (e.g. via `selectinload` in the caller's query) --
    this function does not itself touch the database, keeping it usable
    from both an async SQLAlchemy call site and a synchronous test."""
    regengine_native = _import_native()

    from native.tools.pack_policy import UnsupportedPolicyShapeError, pack_policy  # deferred: only needed when this path actually runs

    if compiled_rule.jsonlogic_ast is None:
        raise UnsupportedForFixGatewayError(f"CompiledRule {compiled_rule.id} (rule_id={compiled_rule.rule_id}) has no jsonlogic_ast to package.")

    try:
        rpkb1_bytes, field_slots = pack_policy(compiled_rule.rule_id, compiled_rule.jsonlogic_ast)
    except UnsupportedPolicyShapeError as exc:
        raise UnsupportedForFixGatewayError(f"CompiledRule {compiled_rule.id}: {exc}") from exc

    unsupported_fields = set(field_slots) - set(FIX_DERIVABLE_FIELDS)
    if unsupported_fields:
        raise UnsupportedForFixGatewayError(
            f"CompiledRule {compiled_rule.id} references field(s) {sorted(unsupported_fields)} that this FIX "
            f"gateway cannot derive from a New Order Single alone (only {FIX_DERIVABLE_FIELDS} are supported); "
            "this rule needs an OMS-supplied external-facts overlay, or must run through the full "
            "app.execution.evaluator/OPA path instead of this fast path."
        )

    compiled_policy = regengine_native.CompiledPolicy(rpkb1_bytes, field_slots)
    rejection = _build_rejection_reason(compiled_rule, compiled_policy.num_checks)

    return LoadedFixPolicy(rule_id=compiled_rule.rule_id, compiled_policy=compiled_policy, field_slots=field_slots, rejection=rejection)


def _build_rejection_reason(compiled_rule: CompiledRule, num_checks: int) -> SebiRejectionReason:
    circular_number = compiled_rule.clause.circular.circular_number if compiled_rule.clause and compiled_rule.clause.circular else "unknown circular"
    clause_number = compiled_rule.clause.clause_number if compiled_rule.clause else "unscoped"

    text = (
        f"Order rejected: does not satisfy compiled rule {compiled_rule.rule_id} "
        f"({num_checks} threshold check(s)), Circular {circular_number}, Clause {clause_number}."
    )
    return SebiRejectionReason(
        ord_rej_reason=OrdRejReason.ORDER_EXCEEDS_LIMIT,
        sebi_circular_number=circular_number,
        sebi_clause_number=clause_number,
        text=text,
    )
