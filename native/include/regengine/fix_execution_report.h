// Requirement 3's wire-format output: builds a FIX 4.4 Execution Report
// (35=8) into a caller-supplied fixed buffer -- no heap allocation, no
// std::string. BodyLength (9) and CheckSum (10) are computed exactly
// per the FIX spec (BodyLength = byte count from the field after 9= up
// to and including the SOH before 10=; CheckSum = sum of all preceding
// bytes mod 256, rendered as a zero-padded 3-digit decimal).
#pragma once

#include <cstddef>
#include <cstdint>
#include <string_view>

#include "regengine/fix_tag_scanner.h"
#include "regengine/fix_tags.h"

namespace regengine::fix {

namespace detail {

// Appends `n` in decimal to `buf[pos]`, advancing `pos` -- no
// allocation, no snprintf (locale-independent, avoids libc call
// overhead on the hot path). `n` is never negative for any value this
// builder writes (sequence numbers, lengths, checksums, quantities are
// all non-negative by construction).
inline void append_uint(char *buf, std::size_t &pos, std::uint64_t n) noexcept {
    char digits[20];
    int count = 0;
    if (n == 0) {
        digits[count++] = '0';
    } else {
        while (n > 0) { digits[count++] = static_cast<char>('0' + (n % 10)); n /= 10; }
    }
    while (count > 0) buf[pos++] = digits[--count];
}

inline void append_str(char *buf, std::size_t &pos, std::string_view s) noexcept {
    for (char c : s) buf[pos++] = c;
}

inline void append_field_int(char *buf, std::size_t &pos, int tag, int value) noexcept {
    append_uint(buf, pos, static_cast<std::uint64_t>(tag));
    buf[pos++] = '=';
    append_uint(buf, pos, static_cast<std::uint64_t>(value));
    buf[pos++] = kSOH;
}

inline void append_field_str(char *buf, std::size_t &pos, int tag, std::string_view value) noexcept {
    append_uint(buf, pos, static_cast<std::uint64_t>(tag));
    buf[pos++] = '=';
    append_str(buf, pos, value);
    buf[pos++] = kSOH;
}

inline void append_field_char(char *buf, std::size_t &pos, int tag, char value) noexcept {
    append_uint(buf, pos, static_cast<std::uint64_t>(tag));
    buf[pos++] = '=';
    buf[pos++] = value;
    buf[pos++] = kSOH;
}

} // namespace detail

// Everything the caller (fix_gateway.h) already knows or has computed
// before it's time to render bytes -- kept as one struct so
// build_execution_report itself has no branching on "is this a
// rejection," just field presence.
struct ExecutionReportInput {
    std::string_view sender_comp_id;  // this gateway's own identity -- becomes the wire message's Tag 49
    std::string_view target_comp_id;  // the original sender -- becomes the wire message's Tag 56
    std::string_view sending_time;    // caller-supplied (UTCTimestamp, e.g. "20260101-12:00:00.000") -- this header has no clock dependency, matching its "no hidden syscalls on the hot path" discipline
    std::string_view cl_ord_id;
    std::string_view order_id;        // Tag 37 -- "NONE" is a valid placeholder for a rejected order that never received a real venue order ID
    std::string_view exec_id;         // Tag 17 -- caller-supplied (e.g. a monotonic counter formatted by the caller); left as a string so the caller controls uniqueness/format, this header does not generate IDs
    std::string_view symbol;
    std::string_view side;
    std::string_view order_qty_raw;   // re-emitted verbatim (see fix_tag_scanner.h's NewOrderSingleFields)
    std::string_view price_raw;

    bool accepted = false;
    // Populated only when accepted == false:
    int ord_rej_reason = tags::ord_rej_reason::kOther;
    std::string_view rejection_text;      // Tag 58 -- human-readable, includes the clause citation in prose too
    std::string_view sebi_clause_ref;     // Tag 9001 -- machine-parseable "<circular_number>:<clause_number>"
};

// Writes the full wire message (including BeginString/BodyLength/
// CheckSum) into `out[0, out_capacity)`. Returns the number of bytes
// written, or 0 if `out_capacity` was too small for this message (the
// caller's buffer should be sized generously -- a few hundred bytes
// comfortably covers every field this builder ever writes; see
// fix_gateway.h's kExecutionReportBufferSize).
[[nodiscard]] inline std::size_t build_execution_report(const ExecutionReportInput &in, char *out, std::size_t out_capacity) noexcept {
    // Body is built first (everything after the BodyLength field) so its
    // exact byte length is known before BodyLength itself is written --
    // FIX's own header ordering requires BodyLength to precede the body
    // it measures.
    char body[512];
    std::size_t body_len = 0;

    detail::append_field_char(body, body_len, tags::kMsgType, tags::msg_type::kExecutionReport);
    detail::append_field_str(body, body_len, tags::kSenderCompID, in.sender_comp_id);
    detail::append_field_str(body, body_len, tags::kTargetCompID, in.target_comp_id);
    detail::append_field_str(body, body_len, tags::kSendingTime, in.sending_time);
    detail::append_field_str(body, body_len, tags::kOrderID, in.order_id);
    detail::append_field_str(body, body_len, tags::kClOrdID, in.cl_ord_id);
    detail::append_field_str(body, body_len, tags::kExecID, in.exec_id);
    detail::append_field_char(body, body_len, tags::kExecType, in.accepted ? tags::exec_type::kNew : tags::exec_type::kRejected);
    detail::append_field_char(body, body_len, tags::kOrdStatus, in.accepted ? tags::ord_status::kNew : tags::ord_status::kRejected);
    detail::append_field_str(body, body_len, tags::kSymbol, in.symbol);
    detail::append_field_str(body, body_len, tags::kSide, in.side);
    detail::append_field_str(body, body_len, tags::kOrderQty, in.order_qty_raw);
    detail::append_field_str(body, body_len, tags::kPrice, in.price_raw);
    detail::append_field_int(body, body_len, tags::kCumQty, 0);
    detail::append_field_str(body, body_len, tags::kLeavesQty, in.accepted ? in.order_qty_raw : std::string_view("0"));
    if (!in.accepted) {
        detail::append_field_int(body, body_len, tags::kOrdRejReason, in.ord_rej_reason);
        detail::append_field_str(body, body_len, tags::kText, in.rejection_text);
        detail::append_field_str(body, body_len, tags::kSebiClauseRef, in.sebi_clause_ref);
    }

    // 8=FIX.4.4 (10 bytes incl. SOH) + 9=<len> (up to ~8 bytes incl. SOH) + body + 10=<3 digits> (7 bytes incl. SOH)
    if (out_capacity < body_len + 32) return 0;

    std::size_t pos = 0;
    detail::append_field_str(out, pos, tags::kBeginString, "FIX.4.4");
    detail::append_field_int(out, pos, tags::kBodyLength, static_cast<int>(body_len));
    detail::append_str(out, pos, std::string_view(body, body_len));

    std::uint32_t checksum = 0;
    for (std::size_t i = 0; i < pos; ++i) checksum += static_cast<std::uint8_t>(out[i]);
    checksum %= 256;

    // CheckSum is always rendered as exactly 3 digits, zero-padded, per
    // the FIX spec -- append_uint alone would emit "7" for a checksum of
    // 7, which is wire-format-invalid.
    out[pos++] = '1'; out[pos++] = '0'; out[pos++] = '=';
    out[pos++] = static_cast<char>('0' + (checksum / 100));
    out[pos++] = static_cast<char>('0' + ((checksum / 10) % 10));
    out[pos++] = static_cast<char>('0' + (checksum % 10));
    out[pos++] = kSOH;

    return pos;
}

} // namespace regengine::fix
