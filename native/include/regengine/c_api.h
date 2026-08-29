/* Requirement 2's C-FFI surface: a pure C ABI (no C++ name mangling, no
 * STL types crossing the boundary) so any order management system --
 * C, C++, Rust via bindgen, Java/C# via JNI/P-Invoke -- can dlopen()/
 * LoadLibrary() this shared library and call straight into the hot
 * path with zero network overhead and zero language-runtime marshaling
 * beyond a plain function call.
 *
 * This header has NO dependency on regengine/policy_types.h or any C++
 * header -- it is included as-is by C compilers. The implementation
 * (src/c_api.cpp) is C++ internally and links against the same
 * evaluate()/load_policy() every other binding uses; this header is
 * purely the ABI contract.
 */
#ifndef REGENGINE_C_API_H
#define REGENGINE_C_API_H

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#if defined(_WIN32)
#  if defined(REGENGINE_BUILD_DLL)
#    define REGENGINE_API __declspec(dllexport)
#  else
#    define REGENGINE_API __declspec(dllimport)
#  endif
#else
#  define REGENGINE_API __attribute__((visibility("default")))
#endif

/* Opaque handle -- the OMS never sees CompiledPolicy's layout directly,
 * so that struct's internal shape can evolve without breaking the C
 * ABI (only regengine_evaluate's semantics, not its signature, are the
 * compatibility contract). */
typedef struct regengine_policy regengine_policy;

typedef enum regengine_load_result {
    REGENGINE_LOAD_OK = 0,
    REGENGINE_LOAD_TRUNCATED = 1,
    REGENGINE_LOAD_BAD_MAGIC = 2,
    REGENGINE_LOAD_UNSUPPORTED_VERSION = 3,
    REGENGINE_LOAD_RULE_ID_TOO_LONG = 4,
    REGENGINE_LOAD_TOO_MANY_CHECKS = 5,
    REGENGINE_LOAD_FIELD_SLOT_OUT_OF_RANGE = 6,
    REGENGINE_LOAD_ALLOC_FAILED = 7,
} regengine_load_result;

/* Parses `data`/`len` (an RPKB1 buffer -- see policy_loader.h and
 * native/tools/pack_policy.py) into a newly-allocated policy handle.
 * The ONLY allocation in this entire API: it happens once, at policy
 * load/hot-reload time, never on the per-order evaluate() call.
 * Returns NULL on any failure; `out_result` (may be NULL if the caller
 * doesn't need the reason) is set to why. The returned handle must be
 * freed with regengine_policy_free exactly once. */
REGENGINE_API regengine_policy *regengine_policy_load(const uint8_t *data, size_t len, regengine_load_result *out_result);

REGENGINE_API void regengine_policy_free(regengine_policy *policy);

/* FNV-1a, 32-bit -- MUST match native/tools/pack_policy.py's `_fnv1a`
 * and regengine::fnv1a_hash bit-for-bit; used to turn an order's
 * `entity_type` string into the same hash a packaged policy's
 * `entity_type_hash` field carries. An OMS integration typically calls
 * this once per distinct entity_type value it sees (there are only a
 * handful, e.g. "Stockbroker"), caching the result, rather than
 * per-order -- though it is cheap enough (a tight byte loop, no
 * allocation) to call per-order if that's simpler to wire up. */
REGENGINE_API uint32_t regengine_hash_entity_type(const char *entity_type, size_t len);

/* THE hot path. `facts_values`/`num_values` is the order's pre-resolved
 * fact vector, indexed by the field_slot values `pack_policy.py`
 * returned alongside this policy's packed bytes (see that module's
 * `pack_policy` docstring) -- resolving a field NAME to a slot index is
 * a one-time, policy-load-time cost the caller pays via
 * `regengine_policy_field_slot`, never here.
 *
 * Returns 1 for ALLOW, 0 for DENY. Never allocates, never throws past
 * the C++/C boundary (any internal exception -- there should never be
 * one on this path, since `policy`/`facts_values` are both
 * already-validated POD data -- is caught and mapped to DENY, the
 * fail-safe default; see src/c_api.cpp). `policy` must be non-NULL and
 * a handle previously returned by regengine_policy_load; passing a
 * freed or NULL handle is undefined behavior, exactly like any other
 * use-after-free/null-deref in this ABI -- this function does not pay
 * a NULL check on the hot path so a correctly-integrated OMS pays zero
 * cost for a mistake it isn't making. */
REGENGINE_API int regengine_evaluate(const regengine_policy *policy, const double *facts_values, size_t num_values, uint32_t entity_type_hash);

REGENGINE_API const char *regengine_policy_rule_id(const regengine_policy *policy);

/* Field-slot wiring is NOT queryable from a loaded handle -- the RPKB1
 * binary deliberately does not embed field NAMES at all (only the
 * numeric slots checks reference), so the hot-path artifact stays as
 * small and simple to load as possible. `pack_policy()`'s returned
 * `field_slots` dict (native/tools/pack_policy.py) is the source of
 * truth for "which facts_values[] index does upfront_margin_pct map
 * to" -- a real deployment persists it as a small companion JSON
 * manifest next to the `.rpkb` binary (e.g. `<rule_id>.schema.json`)
 * and reads it ONCE at OMS startup, the same one-time cost as loading
 * the policy binary itself. */

#ifdef __cplusplus
} /* extern "C" */
#endif

#endif /* REGENGINE_C_API_H */
