// Requirement 2's Python binding.
//
// IMPORTANT (read before using this for a hot-path latency claim): a
// call through pybind11 pays real, unavoidable costs a direct C++ call
// or a C-FFI call from a C++ OMS does not -- acquiring the GIL,
// converting a Python list/dict into C++ types, and the interpreter's
// own call-frame overhead. native/benchmarks/bench_policy_eval.cpp
// measures this binding's overhead SEPARATELY from the pure-C++/C-FFI
// hot path specifically so this module is never mistaken for "the"
// sub-microsecond path -- it exists for policy testing, backtesting
// integration (a natural fit alongside app.backtest.jsonlogic_evaluator,
// which this module's `evaluate_facts` deliberately mirrors the input
// shape of), and any Python-hosted OMS component where the pybind11
// call overhead is genuinely negligible relative to that system's own
// per-order budget. The literal "under 1 microsecond" target is a
// claim about the C++/C-FFI evaluate() call embedded directly in a
// co-located C++ trading engine (see policy_engine.h's module
// docstring), not about this binding.
#include <pybind11/pybind11.h>
#include <pybind11/stl.h>

#include <stdexcept>
#include <string>
#include <unordered_map>
#include <vector>

#include "regengine/policy_engine.h"
#include "regengine/policy_loader.h"

namespace py = pybind11;

namespace {

const char *load_result_message(regengine::LoadResult r) {
    switch (r) {
        case regengine::LoadResult::kOk: return "ok";
        case regengine::LoadResult::kTruncated: return "buffer is shorter than the RPKB1 header/records declare";
        case regengine::LoadResult::kBadMagic: return "bad magic bytes -- not an RPKB1 policy buffer";
        case regengine::LoadResult::kUnsupportedVersion: return "unsupported RPKB1 format_version";
        case regengine::LoadResult::kRuleIdTooLong: return "rule_id exceeds RULE_ID_MAX_LEN";
        case regengine::LoadResult::kTooManyChecks: return "num_checks exceeds MAX_CHECKS_PER_POLICY";
        case regengine::LoadResult::kFieldSlotOutOfRange: return "a check's field_slot exceeds MAX_FACT_SLOTS";
    }
    return "unknown load error";
}

// Mirrors native/tools/pack_policy.py's `_fnv1a` bit-for-bit; kept as a
// free function here (rather than reusing regengine::fnv1a_hash
// directly against a py::str) only to centralize the std::string
// conversion in one place.
std::uint32_t hash_entity_type(const std::string &entity_type) {
    return regengine::fnv1a_hash(entity_type.data(), entity_type.size());
}

class PyCompiledPolicy {
public:
    // `data`: RPKB1 bytes from native/tools/pack_policy.py's
    // `pack_policy()`. `field_slots`: that same call's second return
    // value (`{"upfront_margin_pct": 0, ...}`) -- the binary format
    // deliberately does not embed field names (see c_api.h's docstring
    // on why), so this binding needs them passed in explicitly to
    // support the ergonomic `evaluate_facts(dict)` path.
    PyCompiledPolicy(const py::bytes &data, std::unordered_map<std::string, std::uint16_t> field_slots)
        : field_slots_(std::move(field_slots)) {
        const std::string buffer = data; // one copy, at construction time only -- never on evaluate()
        const auto result = regengine::load_policy(reinterpret_cast<const std::uint8_t *>(buffer.data()), buffer.size(), compiled_);
        if (result != regengine::LoadResult::kOk) {
            throw std::runtime_error(std::string("Failed to load RPKB1 policy: ") + load_result_message(result));
        }
    }

    std::string rule_id() const { return std::string(compiled_.rule_id); }
    std::uint32_t entity_type_hash() const { return compiled_.entity_type_hash; }
    std::uint16_t num_checks() const { return compiled_.num_checks; }

    // The closest thing to the real hot path this binding exposes:
    // caller pre-resolves its own facts vector (see field_slots()) and
    // pays only the pybind11 argument-conversion cost, not a dict walk.
    bool evaluate(const std::vector<double> &values, std::uint32_t entity_type_hash) const {
        return regengine::evaluate_raw(compiled_, values.data(), values.size(), entity_type_hash);
    }

    // Ergonomic path matching app.execution.evaluator's
    // `{"entity_type": ..., "facts": {...}}` input_doc shape and
    // app.backtest.jsonlogic_evaluator's evaluate_jsonlogic signature --
    // intended for tests/backtesting parity checks against the Python
    // evaluators, not the per-order hot path (see this module's
    // docstring).
    bool evaluate_facts(const py::dict &input_doc) const {
        std::vector<double> values(field_slots_.size(), 0.0);
        bool have_all_referenced_fields = true;

        if (input_doc.contains("facts")) {
            py::dict facts = input_doc["facts"];
            for (auto &[name, slot] : field_slots_) {
                py::str key(name);
                if (facts.contains(key)) {
                    values[slot] = facts[key].cast<double>();
                } else {
                    have_all_referenced_fields = false; // matches evaluate_raw's "missing -> DENY" semantics for THIS slot
                }
            }
        } else {
            have_all_referenced_fields = field_slots_.empty();
        }

        std::uint32_t hash = 0;
        if (input_doc.contains("entity_type")) {
            hash = hash_entity_type(input_doc["entity_type"].cast<std::string>());
        }

        if (!have_all_referenced_fields) {
            // A referenced field is genuinely absent from the input --
            // evaluate_raw already denies on an out-of-range slot for
            // this reason; a same-size-but-unset slot needs this
            // explicit check since 0.0 is a valid facts value we must
            // not silently substitute for "missing".
            return false;
        }
        return evaluate(values, hash);
    }

    const std::unordered_map<std::string, std::uint16_t> &field_slots() const { return field_slots_; }

private:
    regengine::CompiledPolicy compiled_{};
    std::unordered_map<std::string, std::uint16_t> field_slots_;
};

} // namespace

PYBIND11_MODULE(regengine_native, m) {
    m.doc() =
        "RegEngine AI ultra-low-latency policy evaluation kernel -- Python binding. "
        "See this module's C++ source (native/bindings/pybind_module.cpp) for why this "
        "binding is NOT the sub-microsecond hot path itself.";

    m.def("hash_entity_type", &hash_entity_type, py::arg("entity_type"),
          "FNV-1a hash matching native/tools/pack_policy.py's packaging-time hash.");

    py::class_<PyCompiledPolicy>(m, "CompiledPolicy")
        .def(py::init<const py::bytes &, std::unordered_map<std::string, std::uint16_t>>(),
             py::arg("data"), py::arg("field_slots"),
             "Load an RPKB1-packaged policy (native/tools/pack_policy.py's pack_policy() output) "
             "plus its field_slots mapping.")
        .def_property_readonly("rule_id", &PyCompiledPolicy::rule_id)
        .def_property_readonly("entity_type_hash", &PyCompiledPolicy::entity_type_hash)
        .def_property_readonly("num_checks", &PyCompiledPolicy::num_checks)
        .def_property_readonly("field_slots", &PyCompiledPolicy::field_slots)
        .def("evaluate", &PyCompiledPolicy::evaluate, py::arg("values"), py::arg("entity_type_hash"),
             "Fast path: caller supplies an already-slot-resolved facts vector.")
        .def("evaluate_facts", &PyCompiledPolicy::evaluate_facts, py::arg("input_doc"),
             "Ergonomic path: input_doc is {'entity_type': ..., 'facts': {...}}, matching "
             "app.execution.evaluator's input_doc / app.backtest.jsonlogic_evaluator's data shape.");
}
