// The hot path: evaluating one CompiledPolicy against one OrderFacts.
//
// Deliberately header-only and `inline` -- in a statically-linked
// co-located OMS build (Requirement 1's ".so"/".dll" packaging is for
// dynamically-loaded deployments; the genuinely fastest deployment
// shape is compiling this header directly into the trading engine's own
// translation unit so the compiler can fully inline evaluate() at the
// call site, eliminating even a function-call/indirect-branch cost from
// the measurement -- see native/benchmarks/bench_policy_eval.cpp, which
// benchmarks both the inlined-call and the exported-symbol (.so/.dll,
// pybind11, C-FFI) shapes separately so the overhead of crossing that
// boundary is measured, not assumed away).
//
// No heap allocation, no exceptions, no virtual dispatch, no string
// comparison -- every branch is over a fixed-size POD array already
// resident in cache by the time an order-management system would call
// this (CompiledPolicy is typically well under a cache line's multiple;
// see policy_types.h's static_assert on ThresholdCheck's size).
#pragma once

#include "regengine/policy_types.h"

namespace regengine {

[[nodiscard]] inline bool evaluate(const CompiledPolicy &policy, const OrderFacts &facts) noexcept {
    // The entity_type constraint is just another ANDed equality check,
    // exactly as app.compiler.jsonlogic_compiler._entity_logic compiles
    // it (`{"==": [{"var": "entity_type"}, "Stockbroker"]}`, ANDed with
    // the thresholds) -- earlier revisions of this engine special-cased
    // a mismatch as "not applicable -> true", which SEEMED like the
    // safe interpretation but was verified WRONG against the real
    // evaluator: app.backtest.jsonlogic_evaluator.evaluate_jsonlogic
    // has no such special case, it evaluates the literal `and`, so a
    // mismatched entity_type makes the whole AST false (not-satisfied),
    // not true. This engine must be bit-compatible with that evaluator
    // (both claim to compute the exact same compiled artifact's
    // result), so it now matches it exactly. Scoping which policies
    // even get evaluated for a given order's entity_type is the
    // CALLER's job, mirroring app.execution.evaluator.Evaluator's own
    // `policies_for(entity_type)` pre-filter -- this function computes
    // one policy's raw AST result, nothing more.
    if (policy.entity_type_hash != 0 && policy.entity_type_hash != facts.entity_type_hash) {
        return false;
    }

    for (std::uint16_t i = 0; i < policy.num_checks; ++i) {
        const ThresholdCheck &check = policy.checks[i];
        const double value = facts.values[check.field_slot];
        bool satisfied;
        switch (check.op) {
            case Operator::kGte: satisfied = value >= check.threshold; break;
            case Operator::kGt:  satisfied = value >  check.threshold; break;
            case Operator::kLte: satisfied = value <= check.threshold; break;
            case Operator::kLt:  satisfied = value <  check.threshold; break;
            case Operator::kEq:  satisfied = value == check.threshold; break;
            default:              satisfied = false; break; // unreachable for a policy that passed policy_loader's validation; fails closed rather than reading past the switch
        }
        if (!satisfied) {
            return false; // most-restrictive-wins within one policy: the first unmet threshold denies, mirroring compile_rule_to_jsonlogic's `and` of all thresholds
        }
    }
    return true;
}

[[nodiscard]] inline Decision evaluate_decision(const CompiledPolicy &policy, const OrderFacts &facts) noexcept {
    return evaluate(policy, facts) ? Decision::kAllow : Decision::kDeny;
}

// Pointer+length variant used directly by the C-FFI (c_api.cpp) so a
// single evaluate() call never pays the cost of copying the caller's
// facts vector into a local OrderFacts first. `values`/`num_values` is
// the OMS's already-resolved fact vector; any `field_slot` a policy
// references at or beyond `num_values` is treated as "fact not
// supplied" and fails the check it's part of (DENY), mirroring OPA's
// own "missing input -> undefined -> safe-by-default" semantics
// (app.compiler.rego_compiler's module docstring) rather than reading
// past the caller's buffer.
[[nodiscard]] inline bool evaluate_raw(const CompiledPolicy &policy, const double *values, std::size_t num_values, std::uint32_t entity_type_hash) noexcept {
    // See evaluate()'s comment above -- must stay bit-compatible with
    // the literal-AND semantics of app.backtest.jsonlogic_evaluator.
    if (policy.entity_type_hash != 0 && policy.entity_type_hash != entity_type_hash) {
        return false;
    }
    for (std::uint16_t i = 0; i < policy.num_checks; ++i) {
        const ThresholdCheck &check = policy.checks[i];
        if (check.field_slot >= num_values) {
            return false;
        }
        const double value = values[check.field_slot];
        bool satisfied;
        switch (check.op) {
            case Operator::kGte: satisfied = value >= check.threshold; break;
            case Operator::kGt:  satisfied = value >  check.threshold; break;
            case Operator::kLte: satisfied = value <= check.threshold; break;
            case Operator::kLt:  satisfied = value <  check.threshold; break;
            case Operator::kEq:  satisfied = value == check.threshold; break;
            default:              satisfied = false; break;
        }
        if (!satisfied) {
            return false;
        }
    }
    return true;
}

// FNV-1a, 32-bit -- fast, dependency-free, and stable across processes/
// platforms (unlike std::hash<std::string>, which is NOT specified to
// be stable across standard library versions or program runs -- this
// hash is computed once at policy-PACKAGING time in Python
// (native/tools/pack_policy.py) and again at ORDER time by the OMS
// integration, and the two must agree byte-for-byte forever).
[[nodiscard]] inline std::uint32_t fnv1a_hash(const char *data, std::size_t len) noexcept {
    std::uint32_t hash = 0x811c9dc5u;
    for (std::size_t i = 0; i < len; ++i) {
        hash ^= static_cast<std::uint8_t>(data[i]);
        hash *= 0x01000193u;
    }
    return hash;
}

} // namespace regengine
