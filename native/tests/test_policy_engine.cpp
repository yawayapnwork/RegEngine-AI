// Standalone C++ correctness test -- no test framework dependency, so
// it builds and runs even with no package manager reachable (see
// CMakeLists.txt's REGENGINE_BUILD_TESTS target). Exits non-zero on any
// failed assertion so `ctest`/CI can gate on it directly.
//
// The reference RPKB1 bytes below are the ACTUAL output of
// native/tools/pack_policy.py's pack_policy() for the JSON-Logic AST
// app.compiler.jsonlogic_compiler.compile_rule_to_jsonlogic produces
// for "Upfront Margin >= 20% for Stockbroker" -- captured by running
// that Python function once (see the comment above the byte array) --
// not hand-invented, so this test also stands as a frozen fixture that
// would catch a binary-format drift between the Python packer and this
// C++ loader.
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>

#include "regengine/c_api.h"
#include "regengine/policy_engine.h"
#include "regengine/policy_loader.h"

namespace {

int g_failures = 0;

void check(bool condition, const char *what) {
    if (!condition) {
        std::fprintf(stderr, "FAIL: %s\n", what);
        ++g_failures;
    } else {
        std::printf("ok: %s\n", what);
    }
}

// Generated via:
//   python -c "from pack_policy import pack_policy; ..."
// for rule_id="margin-rule", logic={"and": [
//   {"==": [{"var": "entity_type"}, "Stockbroker"]},
//   {">=": [{"var": "facts.upfront_margin_pct"}, 20]}]}
// field_slots == {"upfront_margin_pct": 0}
const std::uint8_t kMarginRuleRpkb1[] = {
    0x52, 0x50, 0x4b, 0x31, 0x01, 0x00, 0x0b, 0x00, 0x24, 0xb2, 0xbe, 0x3a,
    0x01, 0x00, 0x00, 0x00, 0x6d, 0x61, 0x72, 0x67, 0x69, 0x6e, 0x2d, 0x72,
    0x75, 0x6c, 0x65, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x34, 0x40,
};

void test_load_and_metadata() {
    regengine::CompiledPolicy policy;
    const auto result = regengine::load_policy(kMarginRuleRpkb1, sizeof(kMarginRuleRpkb1), policy);
    check(result == regengine::LoadResult::kOk, "load_policy succeeds on a real pack_policy() artifact");
    check(std::string(policy.rule_id) == "margin-rule", "rule_id round-trips exactly");
    check(policy.num_checks == 1, "num_checks matches the single threshold in the source AST");
}

void test_evaluate_boundary_and_entity_matching() {
    regengine::CompiledPolicy policy;
    regengine::load_policy(kMarginRuleRpkb1, sizeof(kMarginRuleRpkb1), policy);

    const std::uint32_t stockbroker_hash = regengine::fnv1a_hash("Stockbroker", 11);
    const std::uint32_t other_hash = regengine::fnv1a_hash("DepositoryParticipant", 21);

    regengine::OrderFacts facts_25{};
    facts_25.entity_type_hash = stockbroker_hash;
    facts_25.values[0] = 25.0;
    check(regengine::evaluate(policy, facts_25) == true, "25% margin, matching entity -> ALLOW");

    regengine::OrderFacts facts_20{};
    facts_20.entity_type_hash = stockbroker_hash;
    facts_20.values[0] = 20.0;
    check(regengine::evaluate(policy, facts_20) == true, ">= is inclusive: exactly 20% -> ALLOW");

    regengine::OrderFacts facts_19_99{};
    facts_19_99.entity_type_hash = stockbroker_hash;
    facts_19_99.values[0] = 19.99;
    check(regengine::evaluate(policy, facts_19_99) == false, "19.99% margin -> DENY");

    regengine::OrderFacts facts_wrong_entity{};
    facts_wrong_entity.entity_type_hash = other_hash;
    facts_wrong_entity.values[0] = 99.0;
    check(regengine::evaluate(policy, facts_wrong_entity) == false,
          "entity mismatch denies (literal AND semantics, matches app.backtest.jsonlogic_evaluator -- see policy_engine.h)");
}

void test_malformed_input_fails_closed() {
    regengine::CompiledPolicy policy;

    check(regengine::load_policy(nullptr, 0, policy) == regengine::LoadResult::kTruncated, "empty buffer -> kTruncated, no crash");

    std::uint8_t garbage[16] = {0xff, 0xff, 0xff, 0xff};
    check(regengine::load_policy(garbage, sizeof(garbage), policy) == regengine::LoadResult::kBadMagic, "garbage magic -> kBadMagic");

    // Truncated: valid header claims 1 check, but no check bytes follow.
    std::uint8_t truncated[16];
    std::memcpy(truncated, kMarginRuleRpkb1, 16);
    check(regengine::load_policy(truncated, sizeof(truncated), policy) == regengine::LoadResult::kTruncated,
          "header-only buffer with a nonzero rule_id_len/num_checks -> kTruncated, never reads past the buffer");
}

void test_c_ffi_surface() {
    regengine_load_result c_result;
    regengine_policy *handle = regengine_policy_load(kMarginRuleRpkb1, sizeof(kMarginRuleRpkb1), &c_result);
    check(handle != nullptr, "C-FFI regengine_policy_load succeeds");
    check(c_result == REGENGINE_LOAD_OK, "C-FFI load result is REGENGINE_LOAD_OK");

    const std::uint32_t stockbroker_hash = regengine_hash_entity_type("Stockbroker", 11);
    const double values_25[] = {25.0};
    check(regengine_evaluate(handle, values_25, 1, stockbroker_hash) == 1, "C-FFI regengine_evaluate: 25% -> 1 (allow)");

    const double values_10[] = {10.0};
    check(regengine_evaluate(handle, values_10, 1, stockbroker_hash) == 0, "C-FFI regengine_evaluate: 10% -> 0 (deny)");

    check(std::string(regengine_policy_rule_id(handle)) == "margin-rule", "C-FFI regengine_policy_rule_id round-trips");

    regengine_policy_free(handle);
    check(regengine_policy_load(nullptr, 0, &c_result) == nullptr, "C-FFI load of a null buffer fails closed, does not crash");
}

} // namespace

int main() {
    test_load_and_metadata();
    test_evaluate_boundary_and_entity_matching();
    test_malformed_input_fails_closed();
    test_c_ffi_surface();

    if (g_failures == 0) {
        std::printf("\nAll checks passed.\n");
        return 0;
    }
    std::fprintf(stderr, "\n%d check(s) FAILED.\n", g_failures);
    return 1;
}
