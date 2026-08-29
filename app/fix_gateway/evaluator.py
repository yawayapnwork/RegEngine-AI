"""Requirement 2's validation call, Python side -- the direct analog of
native/include/regengine/fix_gateway.h's `validate_new_order`, but
calling into the compiled policy via `regengine_native`'s pybind11
binding (`CompiledPolicy.evaluate`) rather than the raw C ABI.

Deliberately QuickFIX-independent (see models.py's module docstring) --
this is fully unit-testable against the real, already-built
`regengine_native` extension with no FIX library involved at all, which
is exactly how tests/test_fix_gateway.py exercises it.
"""
from __future__ import annotations

import logging
import time

from app.fix_gateway.models import ParsedOrder, ValidationOutcome
from app.fix_gateway.policy_manifest import FIX_DERIVABLE_FIELDS, LoadedFixPolicy
from app.observability.metrics import FIX_GATEWAY_ORDERS_TOTAL, FIX_GATEWAY_VALIDATION_DURATION

logger = logging.getLogger(__name__)


def _resolve_facts_vector(policy: LoadedFixPolicy, order: ParsedOrder) -> list[float] | None:
    """Returns None (fail closed) if any slot this policy references
    can't be resolved from the order -- mirrors
    native/include/regengine/fix_gateway.h's detail::resolve_fact,
    denying rather than evaluating against a fabricated value."""
    values = [0.0] * len(policy.field_slots)
    for field_name, slot in policy.field_slots.items():
        if field_name == "order_qty":
            values[slot] = order.order_qty
        elif field_name == "price":
            values[slot] = order.price
        elif field_name == "notional_value":
            values[slot] = order.order_qty * order.price
        else:
            # policy_manifest.build_loaded_policy already rejects any
            # field outside FIX_DERIVABLE_FIELDS at load time, so this
            # branch is unreachable for a policy that loaded successfully
            # -- defensive, not a real runtime path.
            logger.error("Policy %s references unresolvable field %r at evaluation time; this should have been caught at load time.", policy.rule_id, field_name)
            return None
    return values


def validate_new_order(policies: list[LoadedFixPolicy], order: ParsedOrder, entity_type_hash: int) -> ValidationOutcome:
    """Most-restrictive-wins: the FIRST policy that denies the order
    wins the rejection, mirroring app.execution.evaluator.Evaluator._reduce's
    "any confirmed breach -> DENY" rule and
    native/include/regengine/fix_gateway.h's identical short-circuit.

    The single choke point every Python-side order validation passes
    through -- see FIX_GATEWAY_VALIDATION_DURATION's module comment on
    why this, not some outer caller, is where the metric is recorded."""
    started = time.perf_counter()
    outcome = _validate(policies, order, entity_type_hash)
    FIX_GATEWAY_VALIDATION_DURATION.observe(time.perf_counter() - started)
    FIX_GATEWAY_ORDERS_TOTAL.labels(outcome="accepted" if outcome.accepted else "rejected").inc()
    return outcome


def _validate(policies: list[LoadedFixPolicy], order: ParsedOrder, entity_type_hash: int) -> ValidationOutcome:
    for policy in policies:
        values = _resolve_facts_vector(policy, order)
        if values is None:
            return ValidationOutcome(accepted=False, violated_reason=policy.rejection)
        if not policy.compiled_policy.evaluate(values, entity_type_hash):
            return ValidationOutcome(accepted=False, violated_reason=policy.rejection)
    return ValidationOutcome(accepted=True)


__all__ = ["validate_new_order", "FIX_DERIVABLE_FIELDS"]
