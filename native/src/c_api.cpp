#define REGENGINE_BUILD_DLL
#include "regengine/c_api.h"

#include <cstring>
#include <new>

#include "regengine/policy_engine.h"
#include "regengine/policy_loader.h"

namespace {

regengine_load_result map_load_result(regengine::LoadResult r) {
    switch (r) {
        case regengine::LoadResult::kOk: return REGENGINE_LOAD_OK;
        case regengine::LoadResult::kTruncated: return REGENGINE_LOAD_TRUNCATED;
        case regengine::LoadResult::kBadMagic: return REGENGINE_LOAD_BAD_MAGIC;
        case regengine::LoadResult::kUnsupportedVersion: return REGENGINE_LOAD_UNSUPPORTED_VERSION;
        case regengine::LoadResult::kRuleIdTooLong: return REGENGINE_LOAD_RULE_ID_TOO_LONG;
        case regengine::LoadResult::kTooManyChecks: return REGENGINE_LOAD_TOO_MANY_CHECKS;
        case regengine::LoadResult::kFieldSlotOutOfRange: return REGENGINE_LOAD_FIELD_SLOT_OUT_OF_RANGE;
    }
    return REGENGINE_LOAD_BAD_MAGIC; // unreachable for a valid enum value; fails closed
}

} // namespace

// Opaque to C callers (see c_api.h); a thin, allocation-once-at-load
// wrapper around the same POD CompiledPolicy every other binding uses.
struct regengine_policy {
    regengine::CompiledPolicy compiled;
};

extern "C" {

REGENGINE_API regengine_policy *regengine_policy_load(const uint8_t *data, size_t len, regengine_load_result *out_result) {
    auto *handle = new (std::nothrow) regengine_policy();
    if (handle == nullptr) {
        if (out_result) *out_result = REGENGINE_LOAD_ALLOC_FAILED;
        return nullptr;
    }

    const regengine::LoadResult result = regengine::load_policy(data, len, handle->compiled);
    if (out_result) *out_result = map_load_result(result);

    if (result != regengine::LoadResult::kOk) {
        delete handle;
        return nullptr;
    }
    return handle;
}

REGENGINE_API void regengine_policy_free(regengine_policy *policy) {
    delete policy;
}

REGENGINE_API uint32_t regengine_hash_entity_type(const char *entity_type, size_t len) {
    return regengine::fnv1a_hash(entity_type, len);
}

REGENGINE_API int regengine_evaluate(const regengine_policy *policy, const double *facts_values, size_t num_values, uint32_t entity_type_hash) {
    // The one place this ABI is defensive despite the header's
    // documented "policy must be non-NULL" contract: a NULL/garbage
    // facts pointer with a nonzero num_values would be a caller bug,
    // but a NULL policy handle is cheap enough to guard against that
    // failing closed (DENY) is strictly better than undefined behavior
    // in a process this critical, even at the cost of one branch on
    // the hot path.
    if (policy == nullptr) {
        return 0;
    }
    return regengine::evaluate_raw(policy->compiled, facts_values, num_values, entity_type_hash) ? 1 : 0;
}

REGENGINE_API const char *regengine_policy_rule_id(const regengine_policy *policy) {
    if (policy == nullptr) {
        return "";
    }
    return policy->compiled.rule_id;
}

} // extern "C"
