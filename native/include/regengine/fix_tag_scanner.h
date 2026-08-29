// Requirement 1's FIX tag parser: an allocation-free, exception-free
// scanner over a raw FIX 4.2/4.4 tag=value wire buffer (SOH-delimited,
// 0x01), extracting exactly the tags this gateway's hot path needs from
// a New Order Single (35=D) into a fixed POD struct of std::string_view
// slices into the CALLER's buffer -- no copy, no heap allocation,
// matching policy_engine.h's "zero heap allocation, zero exceptions on
// the hot path" discipline.
//
// This is deliberately NOT a general FIX engine (no session layer, no
// sequence-number tracking, no repeating groups, no SBE/FIXML) -- it
// parses the flat top-level tag=value pairs of ONE already-received
// message buffer, which is exactly what a co-located gateway needs to
// pull Tag 11/1/38/44 (and the header identity tags needed to address
// the execution report back to the sender) out of a message a session
// layer (QuickFIX/C++, or this project's own minimal session handling)
// has already framed and handed over. See fix_gateway.h for how the
// extracted fields feed regengine::evaluate_raw.
#pragma once

#include <cstddef>
#include <cstdint>
#include <string_view>

namespace regengine::fix {

constexpr char kSOH = '\x01';

enum class ScanResult : std::uint8_t {
    kOk = 0,
    kEmptyMessage = 1,
    kMissingMsgType = 2,
    kNotNewOrderSingle = 3,      // MsgType (35) present but not "D" -- caller's job to route elsewhere, not an error in the scan itself
    kMissingRequiredTag = 4,     // 35=D was present but ClOrdID (11), OrderQty (38), or Price (44) was not
    kMalformedNumber = 5,        // OrderQty/Price present but not parseable as a plain decimal
};

// One tag=value pair as found in the raw buffer, unvalidated -- used
// internally by the scan loop and exposed so a caller wanting a tag
// this struct doesn't name explicitly (e.g. Tag 55 Symbol for logging)
// can still walk the same single pass rather than re-scanning.
struct RawField {
    int tag;
    std::string_view value;
};

// A minimal, allocation-free decimal parser for FIX's plain (non-
// exponential) numeric fields -- Price/OrderQty are always plain
// decimals per the FIX spec (e.g. "2500.50", "100", "0.05"), never
// "1e3", so this deliberately does not handle exponents; a value that
// doesn't match `-?[0-9]+(\.[0-9]+)?` fails closed (returns false)
// rather than guessing. std::stod is avoided because it can throw and
// depends on the current C locale for the decimal point.
[[nodiscard]] inline bool parse_plain_decimal(std::string_view text, double &out) noexcept {
    if (text.empty()) return false;
    std::size_t i = 0;
    bool negative = false;
    if (text[i] == '-') { negative = true; ++i; }
    if (i >= text.size() || text[i] < '0' || text[i] > '9') return false;

    // Accumulate ALL digits (integer + fractional) into one integer
    // mantissa and divide by the appropriate power of 10 exactly once at
    // the end, rather than accumulating the fractional part digit-by-
    // digit with a shrinking `double scale` multiplier -- the latter
    // compounds binary floating-point rounding error at every digit
    // (verified against this file's own test suite: it produced a value
    // for "0.05" that differed in its last bit from the compiler's own
    // `0.05` literal). A single division is the standard technique
    // manual decimal parsers use to minimize rounding error, and is
    // exact for every plain price/quantity value this gateway's example
    // policies compare against.
    //
    // 15 significant digits is comfortably beyond any real order's
    // quantity or price (a double has ~15-17 significant decimal digits
    // of precision; FIX quantities/prices for equity/derivative orders
    // are nowhere near that many digits) -- longer input fails closed
    // rather than silently truncating precision the caller didn't ask to lose.
    constexpr int kMaxSignificantDigits = 15;
    std::uint64_t mantissa = 0;
    int digit_count = 0;
    int frac_digits = 0;
    bool saw_digit = false;
    bool seen_decimal_point = false;

    while (i < text.size()) {
        char c = text[i];
        if (c == '.' && !seen_decimal_point) {
            seen_decimal_point = true;
            ++i;
            continue;
        }
        if (c < '0' || c > '9') break;
        if (digit_count >= kMaxSignificantDigits) return false;
        mantissa = mantissa * 10 + static_cast<std::uint64_t>(c - '0');
        ++digit_count;
        if (seen_decimal_point) ++frac_digits;
        saw_digit = true;
        ++i;
    }
    if (!saw_digit || i != text.size()) return false; // trailing garbage (e.g. "1e3", "12x") or no digits at all fails closed

    // A single division by the exact power of ten (via a lookup table,
    // not `frac_digits` chained `/= 10.0` operations) -- chaining
    // divisions compounds rounding at each step exactly like the
    // digit-by-digit accumulation this replaced; one division by the
    // correct power of ten is the standard, minimal-error technique.
    static constexpr double kPow10[16] = {
        1e0, 1e1, 1e2, 1e3, 1e4, 1e5, 1e6, 1e7,
        1e8, 1e9, 1e10, 1e11, 1e12, 1e13, 1e14, 1e15,
    };
    double value = static_cast<double>(mantissa) / kPow10[frac_digits];
    out = negative ? -value : value;
    return true;
}

// Extracted, typed fields from one New Order Single -- fields not
// present in the message are left as empty string_views / a default
// numeric value with the corresponding `has_*` flag false, so a caller
// never mistakes "absent" for a genuine zero.
struct NewOrderSingleFields {
    std::string_view sender_comp_id;  // Tag 49 (header) -- becomes the execution report's TargetCompID
    std::string_view target_comp_id;  // Tag 56 (header) -- becomes the execution report's SenderCompID
    std::string_view msg_seq_num;     // Tag 34 (header)
    std::string_view cl_ord_id;       // Tag 11 -- required
    std::string_view account;         // Tag 1 -- "Account" in the FIX spec; used by Indian broker OMS integrations to carry the client's Unique Client Code (UCC), hence this gateway's "ClientID" framing
    std::string_view symbol;          // Tag 55
    std::string_view side;            // Tag 54
    std::string_view ord_type;        // Tag 40
    std::string_view order_qty_raw;   // Tag 38, as received (re-emitted verbatim in the execution report)
    std::string_view price_raw;       // Tag 44, as received
    double order_qty = 0.0;           // Tag 38, parsed -- required
    double price = 0.0;               // Tag 44, parsed -- required
    bool has_cl_ord_id = false;
    bool has_order_qty = false;
    bool has_price = false;
};

namespace detail {

// Parses the tag number at the start of `field` (up to but not
// including '='), returning -1 if `field` has no '=' or the prefix
// isn't all digits -- a manual loop rather than std::from_chars for
// consistency with this header's "no exceptions, no locale, no libc
// numeric-parsing surprises" stance, and because FIX tag numbers are
// small (at most 5 digits for any tag this gateway cares about).
[[nodiscard]] inline int parse_tag_and_split(std::string_view field, std::string_view &value) noexcept {
    std::size_t eq = field.find('=');
    if (eq == std::string_view::npos || eq == 0) return -1;
    int tag = 0;
    for (std::size_t i = 0; i < eq; ++i) {
        char c = field[i];
        if (c < '0' || c > '9') return -1;
        tag = tag * 10 + (c - '0');
    }
    value = field.substr(eq + 1);
    return tag;
}

} // namespace detail

// Single pass over `data[0, len)`. Returns kOk only when 35=D and every
// required tag (11, 38, 44) was present and OrderQty/Price parsed as
// plain decimals -- any other outcome means the caller must not proceed
// to policy evaluation (see fix_gateway.h, which fails a scan error
// closed to a business reject, never a silent allow).
[[nodiscard]] inline ScanResult scan_new_order_single(const char *data, std::size_t len, NewOrderSingleFields &out) noexcept {
    if (len == 0) return ScanResult::kEmptyMessage;

    bool saw_msg_type = false;
    bool is_new_order_single = false;

    std::size_t pos = 0;
    while (pos < len) {
        std::size_t next_soh = pos;
        while (next_soh < len && data[next_soh] != kSOH) ++next_soh;
        std::string_view field(data + pos, next_soh - pos);
        pos = (next_soh < len) ? next_soh + 1 : next_soh;

        if (field.empty()) continue;
        std::string_view value;
        int tag = detail::parse_tag_and_split(field, value);
        if (tag < 0) continue; // malformed field -- skip rather than abort, mirroring a lenient wire parser; required-tag checks below still catch a genuinely unusable message

        switch (tag) {
            case 35:
                saw_msg_type = true;
                is_new_order_single = (value == "D");
                break;
            case 49: out.sender_comp_id = value; break;
            case 56: out.target_comp_id = value; break;
            case 34: out.msg_seq_num = value; break;
            case 11: out.cl_ord_id = value; out.has_cl_ord_id = !value.empty(); break;
            case 1:  out.account = value; break;
            case 55: out.symbol = value; break;
            case 54: out.side = value; break;
            case 40: out.ord_type = value; break;
            case 38:
                out.order_qty_raw = value;
                out.has_order_qty = parse_plain_decimal(value, out.order_qty);
                break;
            case 44:
                out.price_raw = value;
                out.has_price = parse_plain_decimal(value, out.price);
                break;
            default:
                break; // every other tag is outside this gateway's declared scope -- ignored, not an error
        }
    }

    if (!saw_msg_type) return ScanResult::kMissingMsgType;
    if (!is_new_order_single) return ScanResult::kNotNewOrderSingle;
    if (!out.has_cl_ord_id) return ScanResult::kMissingRequiredTag;
    if (!out.has_order_qty || !out.has_price) {
        // Distinguish "tag absent" from "tag present but unparsable" only
        // for the caller's diagnostics; both are treated identically by
        // the hot path (refuse to evaluate on incomplete facts).
        return (out.order_qty_raw.empty() || out.price_raw.empty()) ? ScanResult::kMissingRequiredTag : ScanResult::kMalformedNumber;
    }
    return ScanResult::kOk;
}

} // namespace regengine::fix
