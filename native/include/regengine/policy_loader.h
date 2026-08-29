// Loads RegEngine AI's compact native policy binary format ("RPKB1",
// produced by native/tools/pack_policy.py from a real
// app.compiler.jsonlogic_compiler.JsonLogicRule) into a `CompiledPolicy`.
//
// This is the ONLY place in the C++ side that parses untrusted bytes --
// every check here exists because `data`/`len` may come from a file on
// disk, a network-delivered policy bundle, or a Python `bytes` object
// handed across the pybind11 boundary, none of which this loader
// assumes were produced by a well-behaved packer. `load_policy` never
// throws and never reads past `data + len`; a malformed input fails
// closed (returns false, `out` left default-constructed / all-deny)
// rather than risking undefined behavior in a process that is, by
// construction, running inside a latency-critical trading engine where
// a crash is far worse than a rejected policy load.
//
// Assumes a little-endian host (x86_64, ARM64 -- every realistic
// co-located HFT deployment target) and does not attempt to be portable
// to big-endian architectures; add an explicit byteswap path if that
// ever changes.
#pragma once

#include <cstdint>
#include <cstring>

#include "regengine/policy_types.h"

namespace regengine {

inline constexpr std::uint32_t kRpkbMagic = 0x314B5052u; // "RPK1" read as a little-endian uint32
inline constexpr std::uint16_t kRpkbFormatVersion = 1;

#pragma pack(push, 1)
struct RpkbHeader {
    std::uint32_t magic;
    std::uint16_t format_version;
    std::uint16_t rule_id_len;
    std::uint32_t entity_type_hash;
    std::uint16_t num_checks;
    std::uint16_t reserved;
};
#pragma pack(pop)
static_assert(sizeof(RpkbHeader) == 16, "RpkbHeader must match pack_policy.py's _HEADER struct.Struct('<4sHHIHH') layout exactly.");

enum class LoadResult : std::uint8_t {
    kOk = 0,
    kTruncated,        // fewer bytes than the header, or than the header declares
    kBadMagic,
    kUnsupportedVersion,
    kRuleIdTooLong,    // rule_id_len exceeds kRuleIdMaxLen
    kTooManyChecks,    // num_checks exceeds kMaxChecksPerPolicy
    kFieldSlotOutOfRange, // a check's field_slot exceeds kMaxFactSlots
};

[[nodiscard]] inline LoadResult load_policy(const std::uint8_t *data, std::size_t len, CompiledPolicy &out) noexcept {
    out = CompiledPolicy{};

    if (len < sizeof(RpkbHeader)) {
        return LoadResult::kTruncated;
    }

    RpkbHeader header;
    std::memcpy(&header, data, sizeof(RpkbHeader));

    if (header.magic != kRpkbMagic) {
        return LoadResult::kBadMagic;
    }
    if (header.format_version != kRpkbFormatVersion) {
        return LoadResult::kUnsupportedVersion;
    }
    if (header.rule_id_len > kRuleIdMaxLen) {
        return LoadResult::kRuleIdTooLong;
    }
    if (header.num_checks > kMaxChecksPerPolicy) {
        return LoadResult::kTooManyChecks;
    }

    const std::size_t checks_offset = sizeof(RpkbHeader) + header.rule_id_len;
    const std::size_t checks_bytes = static_cast<std::size_t>(header.num_checks) * sizeof(ThresholdCheck);
    if (len < checks_offset + checks_bytes) {
        return LoadResult::kTruncated;
    }

    std::memcpy(out.rule_id, data + sizeof(RpkbHeader), header.rule_id_len);
    // out.rule_id is value-initialized to all zero above, and
    // rule_id_len <= kRuleIdMaxLen was just checked, so this is always
    // NUL-terminated within bounds -- no separate terminator write needed.

    out.entity_type_hash = header.entity_type_hash;
    out.num_checks = header.num_checks;
    std::memcpy(out.checks, data + checks_offset, checks_bytes);

    for (std::uint16_t i = 0; i < out.num_checks; ++i) {
        if (out.checks[i].field_slot >= kMaxFactSlots) {
            out = CompiledPolicy{}; // don't hand back a partially-valid policy
            return LoadResult::kFieldSlotOutOfRange;
        }
    }

    return LoadResult::kOk;
}

} // namespace regengine
