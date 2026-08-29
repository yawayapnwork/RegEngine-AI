// Requirement 2 + 3, wired end to end: raw FIX 35=D bytes in, raw FIX
// 35=8 bytes out, with the SEBI-clause-mapped rejection when a compiled
// policy denies the order. This header is the ONLY new moving part on
// top of the pre-existing, already-benchmarked policy_engine.h kernel
// (native/include/regengine/policy_engine.h) -- it never re-implements
// policy evaluation, only FIX-specific framing around it.
//
// What this gateway can and can't validate from a FIX message alone:
// Tag 38 (OrderQty) and Tag 44 (Price) are wire-native -- a compiled
// policy checking "order quantity <= freeze limit" or "order value
// (qty*price) <= per-order notional cap" is fully evaluable from the
// New Order Single message alone, which is what the shipped example
// policies below check. A policy checking something like "post-trade
// margin utilization <= 80%" needs a fact NO FIX NewOrderSingle carries
// (the client's current margin/collateral position, which lives in the
// broker's own risk engine, not on the wire) -- FactSource::kExternal
// is this header's seam for an OMS integration to supply such
// pre-computed facts, and a policy referencing a slot the caller didn't
// supply DENIES (fails closed), it never silently treats a missing
// external fact as compliant.
#pragma once

#include <cstddef>
#include <cstdint>
#include <cstring>

#include "regengine/fix_execution_report.h"
#include "regengine/fix_tag_scanner.h"
#include "regengine/fix_tags.h"
#include "regengine/policy_engine.h"
#include "regengine/policy_types.h"

namespace regengine::fix {

// Precomputed once, at policy-load time (see the companion
// native/fix_gateway/policy_manifest.py cold path) -- the hot path only
// ever reads these fixed buffers, never formats a string.
struct RejectionMeta {
    int ord_rej_reason = tags::ord_rej_reason::kOther;
    char text[224] = {};           // full human-readable rejection message, INCLUDING the rendered threshold and clause citation (e.g. "Order quantity 15000 exceeds the SEBI-mandated freeze limit of 10000 units (Circular SEBI/HO/MIRSD/2024/100, Clause 4.2.b).")
    char sebi_clause_ref[64] = {}; // machine-parseable "<circular_number>:<clause_number>"
};

// Which FIX-derivable (or externally-supplied) quantity fills one
// policy's facts.values[slot] -- resolved once per policy at load time
// from that policy's pack_policy() field_slots mapping (see
// policy_manifest.py), since slot numbering is assigned independently
// per policy (first-seen order within THAT policy's own AST), not
// shared across the loaded policy set.
enum class FactSource : std::uint8_t {
    kOrderQty = 0,
    kPrice = 1,
    kNotionalValue = 2, // order_qty * price, computed once per order, reused across every policy that references it
    kExternal = 3,      // not derivable from this FIX message -- see this header's module comment
};

inline constexpr std::uint16_t kMaxFactSlots = regengine::kMaxFactSlots;

struct PolicyBundle {
    CompiledPolicy policy;
    RejectionMeta rejection;
    FactSource fact_sources[kMaxFactSlots] = {};
    std::uint16_t num_slots = 0; // highest field_slot referenced by `policy`, plus one
};

// A loaded, ready-to-evaluate policy set for ONE FIX counterparty
// identity. `entity_type_hash` is the RegEngine entity_type (e.g.
// "Stockbroker") this gateway INSTANCE represents -- a FIX
// NewOrderSingle carries no such tag; which entity_type a given session
// maps to is a deployment-time/session-configuration decision (in the
// QuickFIX/Python integration, this is looked up per SessionID -- see
// app/fix_gateway/policy_bridge.py), not something this header derives
// per message.
struct PolicySet {
    const PolicyBundle *bundles = nullptr;
    std::size_t count = 0;
    std::uint32_t entity_type_hash = 0;
};

enum class ValidationOutcome : std::uint8_t {
    kAccepted = 0,
    kRejected = 1,
    kMalformedMessage = 2, // scan_new_order_single failed -- see ScanResult; the caller's session layer should treat this as a business reject with a generic reason, this gateway never guesses at intent from a malformed message
};

struct ValidationResult {
    ValidationOutcome outcome;
    ScanResult scan_result;              // always populated; kOk unless outcome == kMalformedMessage
    const RejectionMeta *violated_meta;  // non-null only when outcome == kRejected -- the FIRST policy that denied (most-restrictive-wins, mirroring app.execution.evaluator.Evaluator._reduce's own "first confirmed breach wins" rule)
    std::size_t report_len;              // bytes written to the caller's output buffer, 0 on kMalformedMessage (no report is built for a message this gateway couldn't parse at all -- see this header's module comment)
};

namespace detail {

inline double resolve_fact(FactSource source, const NewOrderSingleFields &f, bool &ok) noexcept {
    switch (source) {
        case FactSource::kOrderQty: return f.order_qty;
        case FactSource::kPrice: return f.price;
        case FactSource::kNotionalValue: return f.order_qty * f.price;
        case FactSource::kExternal:
        default:
            ok = false; // fail closed -- see this header's module comment on FactSource::kExternal
            return 0.0;
    }
}

inline bool evaluate_bundle(const PolicyBundle &bundle, const NewOrderSingleFields &fields, std::uint32_t entity_type_hash) noexcept {
    double values[kMaxFactSlots];
    bool ok = true;
    for (std::uint16_t slot = 0; slot < bundle.num_slots; ++slot) {
        values[slot] = resolve_fact(bundle.fact_sources[slot], fields, ok);
    }
    if (!ok) return false; // a required external fact wasn't available -- deny rather than evaluate against a fabricated zero
    return regengine::evaluate_raw(bundle.policy, values, bundle.num_slots, entity_type_hash);
}

} // namespace detail

// THE hot path: raw FIX bytes in, raw FIX bytes out. `sender_comp_id`
// is this gateway's own identity (becomes the execution report's Tag
// 49); `sending_time`/`exec_id` are caller-supplied since this header
// has no clock or ID-generation dependency (see
// fix_execution_report.h's ExecutionReportInput).
[[nodiscard]] inline ValidationResult validate_new_order(
    const PolicySet &policy_set,
    const char *raw_message, std::size_t raw_len,
    std::string_view sender_comp_id, std::string_view sending_time, std::string_view exec_id,
    char *report_out, std::size_t report_capacity) noexcept {

    NewOrderSingleFields fields;
    ScanResult scan = scan_new_order_single(raw_message, raw_len, fields);
    if (scan != ScanResult::kOk) {
        return ValidationResult{ValidationOutcome::kMalformedMessage, scan, nullptr, 0};
    }

    const RejectionMeta *violated = nullptr;
    for (std::size_t i = 0; i < policy_set.count && violated == nullptr; ++i) {
        if (!detail::evaluate_bundle(policy_set.bundles[i], fields, policy_set.entity_type_hash)) {
            violated = &policy_set.bundles[i].rejection;
        }
    }

    ExecutionReportInput report_in;
    report_in.sender_comp_id = sender_comp_id;
    report_in.target_comp_id = fields.sender_comp_id; // the ORIGINAL sender is who we're replying TO
    report_in.sending_time = sending_time;
    report_in.cl_ord_id = fields.cl_ord_id;
    report_in.order_id = "NONE"; // no venue order ID exists yet at (pre-)validation time -- see ExecutionReportInput's field comment
    report_in.exec_id = exec_id;
    report_in.symbol = fields.symbol;
    report_in.side = fields.side;
    report_in.order_qty_raw = fields.order_qty_raw;
    report_in.price_raw = fields.price_raw;
    report_in.accepted = (violated == nullptr);
    if (violated != nullptr) {
        report_in.ord_rej_reason = violated->ord_rej_reason;
        report_in.rejection_text = std::string_view(violated->text);
        report_in.sebi_clause_ref = std::string_view(violated->sebi_clause_ref);
    }

    std::size_t report_len = build_execution_report(report_in, report_out, report_capacity);
    return ValidationResult{
        violated == nullptr ? ValidationOutcome::kAccepted : ValidationOutcome::kRejected,
        ScanResult::kOk, violated, report_len,
    };
}

} // namespace regengine::fix
