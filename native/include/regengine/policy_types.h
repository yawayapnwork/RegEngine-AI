// Core POD types for RegEngine AI's native policy evaluation kernel.
//
// This is NOT a general Rego/OPA interpreter. It is a purpose-built,
// allocation-free evaluator for exactly the restricted grammar
// app.compiler.jsonlogic_compiler ever emits: an AND of numeric
// threshold comparisons (>=, >, <=, <, ==) over a flat `facts` map,
// optionally gated by an `entity_type` equality check -- see that
// module's docstring and app.backtest.jsonlogic_evaluator's module
// docstring ("deliberately NOT a general-purpose JSON-Logic
// implementation... bit-for-bit fidelity with what the compiler
// actually emits") for why that narrower scope is a deliberate,
// load-bearing design choice there, and doubly so here: a real OPA/Wasm
// interpreter's bytecode dispatch, bounds checks, and generic value
// representation make a true sub-microsecond target unreachable. A flat
// array of pre-resolved (field_slot, operator, threshold) triples that
// the compiler walks with zero heap allocation, zero virtual dispatch,
// and zero string hashing on the hot path is what actually gets there.
//
// The cold path (turning a JsonLogicRule.logic AST + its data_schema
// into this flat representation) lives in Python
// (native/tools/pack_policy.py) and in policy_loader.h/.cpp -- resolving
// `{"var": "facts.upfront_margin_pct"}` into a fixed integer slot index
// happens ONCE, when a policy is published/hot-reloaded, never per order.
#pragma once

#include <cstddef>
#include <cstdint>

namespace regengine {

// Fixed capacity, not std::vector -- CompiledPolicy is a POD struct with
// no heap allocation anywhere in it, so it can be memory-mapped directly
// from the packaged binary (see policy_loader.h) or embedded as a
// read-only static array in a statically-linked build, and evaluate()
// never touches the allocator regardless of which load path was used.
// 32 comfortably covers every compiled SEBI rule this system has
// produced to date (app.compiler.hitl's flag_conflicting_thresholds
// already treats more than a couple of thresholds on one field as
// suspicious) -- raise it, and PolicyBinaryHeader::checksum/format
// version, if a future rule genuinely needs more.
inline constexpr std::uint16_t kMaxChecksPerPolicy = 32;
inline constexpr std::uint16_t kMaxFactSlots = 64;
inline constexpr std::size_t kRuleIdMaxLen = 80; // matches ExtractedComplianceRule.rule_id's "<64-hex-sha256>:<clause_number>" shape with room to spare

enum class Operator : std::uint8_t {
    kGte = 0, // ">="
    kGt = 1,  // ">"
    kLte = 2, // "<="
    kLt = 3,  // "<"
    kEq = 4,  // "=="
};

// One `{"<op>": [{"var": "facts.<field>"}, <threshold>]}` node, with
// `field` already resolved to its slot index in the caller's OrderFacts
// layout -- see FactSchema in policy_loader.h for how that resolution
// happens exactly once, at load time.
struct ThresholdCheck {
    std::uint16_t field_slot;
    Operator op;
    std::uint8_t _pad[5] = {}; // explicit padding -- keeps `threshold`'s alignment obvious and the struct's layout stable across compilers/ABIs, since this struct is also the packaged binary format's on-disk record shape (see policy_loader.h)
    double threshold;
};
static_assert(sizeof(ThresholdCheck) == 16, "ThresholdCheck's layout is the on-disk binary format's record shape -- changing its size is a binary-format break, not just a recompile.");

// The result of app.compiler.jsonlogic_compiler.compile_rule_to_jsonlogic
// for one rule, flattened. `entity_type_hash == 0` means "no entity_type
// constraint" (app.compiler.jsonlogic_compiler._entity_logic returns
// None when a rule has no target_entities) -- 0 is never a valid FNV-1a
// hash of a real, non-empty entity_type string with high enough
// probability to treat a genuine collision as anything but a
// wildly-unlikely edge case a real deployment would catch via the
// correctness test suite (native/tests/), not a hot-path check.
struct CompiledPolicy {
    char rule_id[kRuleIdMaxLen] = {};
    std::uint32_t entity_type_hash = 0;
    std::uint16_t num_checks = 0;
    std::uint8_t _pad[2] = {};
    ThresholdCheck checks[kMaxChecksPerPolicy] = {};
};

// The OMS-side input document, pre-resolved into the SAME field_slot
// layout the policy's checks reference -- filling this in is the OMS
// integration's one-time-per-order-shape responsibility (via
// FactSchema::resolve_slot at policy-load time, then a direct array
// write per order), so evaluate() itself never resolves a field name.
struct OrderFacts {
    std::uint32_t entity_type_hash = 0;
    double values[kMaxFactSlots] = {};
};

enum class Decision : std::uint8_t {
    kAllow = 1,
    kDeny = 0,
};

} // namespace regengine
