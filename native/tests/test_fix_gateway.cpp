// Standalone C++ correctness test for the FIX gateway layer -- same
// no-framework, assert-and-exit-nonzero-on-failure style as
// test_policy_engine.cpp, so it builds and runs with nothing but a
// C++17 compiler.
//
// The RPKB1 byte arrays below are the ACTUAL output of
// native/tools/pack_policy.py's pack_policy() for two real example
// SEBI-style order-parameter rules -- both fully derivable from a New
// Order Single alone (see fix_gateway.h's module comment on why these
// two, not a margin-percentage rule, were chosen as the worked
// example), captured via:
//
//   Rule A ("qty-limit-rule"): order quantity shall not exceed 10,000
//     units per order --
//     pack_policy("qty-limit-rule", {"<=": [{"var": "facts.order_qty"}, 10000]})
//   Rule B ("notional-limit-rule"): order value (quantity * price)
//     shall not exceed Rs. 50,00,000 per order --
//     pack_policy("notional-limit-rule", {"<=": [{"var": "facts.notional_value"}, 5000000]})
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>

#include "regengine/fix_execution_report.h"
#include "regengine/fix_gateway.h"
#include "regengine/fix_tag_scanner.h"
#include "regengine/policy_loader.h"

namespace {

const std::uint8_t kQtyLimitPolicyBytes[] = {
    0x52, 0x50, 0x4b, 0x31, 0x01, 0x00, 0x0e, 0x00, 0x00, 0x00, 0x00, 0x00, 0x01, 0x00, 0x00, 0x00,
    0x71, 0x74, 0x79, 0x2d, 0x6c, 0x69, 0x6d, 0x69, 0x74, 0x2d, 0x72, 0x75, 0x6c, 0x65, 0x00, 0x00,
    0x02, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x88, 0xc3, 0x40,
};

const std::uint8_t kNotionalLimitPolicyBytes[] = {
    0x52, 0x50, 0x4b, 0x31, 0x01, 0x00, 0x13, 0x00, 0x00, 0x00, 0x00, 0x00, 0x01, 0x00, 0x00, 0x00,
    0x6e, 0x6f, 0x74, 0x69, 0x6f, 0x6e, 0x61, 0x6c, 0x2d, 0x6c, 0x69, 0x6d, 0x69, 0x74, 0x2d, 0x72,
    0x75, 0x6c, 0x65, 0x00, 0x00, 0x02, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0xd0,
    0x12, 0x53, 0x41,
};

} // namespace

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

// --- FIX tag scanner tests ---

void test_scan_valid_new_order_single() {
    const char msg[] =
        "8=FIX.4.4\x01" "9=145\x01" "35=D\x01" "49=BROKERCO\x01" "56=REGENGINE\x01" "34=1\x01"
        "52=20260101-12:00:00.000\x01" "11=ORD0001\x01" "1=UCC12345\x01" "55=RELIANCE\x01"
        "54=1\x01" "40=2\x01" "38=100\x01" "44=2500.50\x01" "10=000\x01";
    regengine::fix::NewOrderSingleFields fields;
    auto result = regengine::fix::scan_new_order_single(msg, sizeof(msg) - 1, fields);
    check(result == regengine::fix::ScanResult::kOk, "valid NewOrderSingle scans as kOk");
    check(fields.cl_ord_id == "ORD0001", "ClOrdID (11) extracted correctly");
    check(fields.account == "UCC12345", "Account (1) extracted correctly");
    check(fields.order_qty == 100.0, "OrderQty (38) parsed as 100.0");
    check(fields.price == 2500.50, "Price (44) parsed as 2500.50");
    check(fields.sender_comp_id == "BROKERCO", "SenderCompID (49) extracted correctly");
    check(fields.symbol == "RELIANCE", "Symbol (55) extracted correctly");
}

void test_scan_rejects_non_new_order_single() {
    const char msg[] = "8=FIX.4.4\x01" "35=0\x01" "10=000\x01"; // Heartbeat
    regengine::fix::NewOrderSingleFields fields;
    auto result = regengine::fix::scan_new_order_single(msg, sizeof(msg) - 1, fields);
    check(result == regengine::fix::ScanResult::kNotNewOrderSingle, "a Heartbeat (35=0) is correctly identified as not a NewOrderSingle");
}

void test_scan_flags_missing_required_tag() {
    const char msg[] = "8=FIX.4.4\x01" "35=D\x01" "1=UCC1\x01" "38=100\x01" "44=10\x01" "10=000\x01"; // no ClOrdID
    regengine::fix::NewOrderSingleFields fields;
    auto result = regengine::fix::scan_new_order_single(msg, sizeof(msg) - 1, fields);
    check(result == regengine::fix::ScanResult::kMissingRequiredTag, "a NewOrderSingle missing ClOrdID (11) is flagged, not silently accepted");
}

void test_scan_flags_malformed_number() {
    const char msg[] = "8=FIX.4.4\x01" "35=D\x01" "11=X\x01" "38=1e5\x01" "44=10\x01" "10=000\x01"; // exponential notation, not a plain decimal
    regengine::fix::NewOrderSingleFields fields;
    auto result = regengine::fix::scan_new_order_single(msg, sizeof(msg) - 1, fields);
    check(result == regengine::fix::ScanResult::kMalformedNumber, "OrderQty in exponential notation ('1e5') is rejected, not silently mis-parsed");
}

void test_parse_plain_decimal_edge_cases() {
    double v;
    check(regengine::fix::parse_plain_decimal("100", v) && v == 100.0, "parse_plain_decimal: integer");
    check(regengine::fix::parse_plain_decimal("2500.50", v) && v == 2500.50, "parse_plain_decimal: decimal");
    check(regengine::fix::parse_plain_decimal("-5.25", v) && v == -5.25, "parse_plain_decimal: negative");
    check(regengine::fix::parse_plain_decimal("0.05", v) && v == 0.05, "parse_plain_decimal: leading zero");
    check(!regengine::fix::parse_plain_decimal("", v), "parse_plain_decimal: empty string rejected");
    check(!regengine::fix::parse_plain_decimal("12x", v), "parse_plain_decimal: trailing garbage rejected");
    check(!regengine::fix::parse_plain_decimal("1e3", v), "parse_plain_decimal: exponential notation rejected");
    check(!regengine::fix::parse_plain_decimal(".", v), "parse_plain_decimal: lone decimal point rejected");
}

// --- Execution report builder tests ---

void test_execution_report_wire_format() {
    regengine::fix::ExecutionReportInput in;
    in.sender_comp_id = "REGENGINE";
    in.target_comp_id = "BROKERCO";
    in.sending_time = "20260101-12:00:00.000";
    in.cl_ord_id = "ORD0001";
    in.order_id = "NONE";
    in.exec_id = "1";
    in.symbol = "RELIANCE";
    in.side = "1";
    in.order_qty_raw = "100";
    in.price_raw = "2500.50";
    in.accepted = false;
    in.ord_rej_reason = regengine::fix::tags::ord_rej_reason::kOrderExceedsLimit;
    in.rejection_text = "Order quantity 100 exceeds the SEBI-mandated limit.";
    in.sebi_clause_ref = "SEBI/HO/MIRSD/2024/100:4.2.b";

    char out[512];
    std::size_t len = regengine::fix::build_execution_report(in, out, sizeof(out));
    check(len > 0, "build_execution_report writes a non-empty message");

    std::string_view msg(out, len);
    check(msg.substr(0, 5) == "8=FIX", "message starts with BeginString (8=)");
    check(msg.find("35=8\x01") != std::string_view::npos, "MsgType is 8 (Execution Report)");
    check(msg.find("39=8\x01") != std::string_view::npos, "OrdStatus is 8 (Rejected)");
    check(msg.find("150=8\x01") != std::string_view::npos, "ExecType is 8 (Rejected)");
    check(msg.find("103=3\x01") != std::string_view::npos, "OrdRejReason (103) is 3 (OrderExceedsLimit)");
    check(msg.find("9001=SEBI/HO/MIRSD/2024/100:4.2.b\x01") != std::string_view::npos, "custom SEBI clause tag (9001) carries the exact citation");
    check(msg.substr(len - 7, 3) == "10=", "message ends with the CheckSum field (10=NNN + SOH, the last 7 bytes)");

    // Verify the checksum is actually correct, not just present.
    std::size_t checksum_field_start = msg.rfind("10=");
    std::uint32_t computed = 0;
    for (std::size_t i = 0; i < checksum_field_start; ++i) computed += static_cast<std::uint8_t>(out[i]);
    computed %= 256;
    char expected[4];
    std::snprintf(expected, sizeof(expected), "%03u", computed);
    check(msg.substr(checksum_field_start + 3, 3) == std::string_view(expected, 3), "CheckSum (10) is arithmetically correct (mod-256 sum of all preceding bytes)");

    // Verify BodyLength (9=) is arithmetically correct: it must equal the
    // byte count from immediately after that field's SOH through the SOH
    // right before the CheckSum field.
    std::size_t body_len_field_start = msg.find("9=") + 2;
    std::size_t body_len_field_end = msg.find(regengine::fix::kSOH, body_len_field_start);
    int declared_body_len = std::atoi(std::string(msg.substr(body_len_field_start, body_len_field_end - body_len_field_start)).c_str());
    std::size_t body_start = body_len_field_end + 1;
    std::size_t actual_body_len = checksum_field_start - body_start;
    check(static_cast<std::size_t>(declared_body_len) == actual_body_len, "BodyLength (9) matches the actual byte count of the body it measures");
}

// --- End-to-end: raw FIX bytes -> validate_new_order -> raw FIX bytes ---

void copy_into_fixed(char *dest, std::size_t dest_size, const char *src) {
    std::size_t i = 0;
    for (; i < dest_size - 1 && src[i] != '\0'; ++i) dest[i] = src[i];
    dest[i] = '\0';
}

regengine::fix::PolicyBundle load_bundle(const std::uint8_t *rpkb1, std::size_t len, regengine::fix::FactSource source0,
                                          const char *sebi_circular_clause, const char *rejection_text) {
    regengine::fix::PolicyBundle bundle;
    regengine::LoadResult lr = regengine::load_policy(rpkb1, len, bundle.policy);
    check(lr == regengine::LoadResult::kOk, "RPKB1 policy bytes load successfully");
    bundle.num_slots = 1;
    bundle.fact_sources[0] = source0;
    bundle.rejection.ord_rej_reason = regengine::fix::tags::ord_rej_reason::kOrderExceedsLimit;
    copy_into_fixed(bundle.rejection.text, sizeof(bundle.rejection.text), rejection_text);
    copy_into_fixed(bundle.rejection.sebi_clause_ref, sizeof(bundle.rejection.sebi_clause_ref), sebi_circular_clause);
    return bundle;
}

void test_validate_new_order_end_to_end() {
    regengine::fix::PolicyBundle bundles[2] = {
        load_bundle(kQtyLimitPolicyBytes, sizeof(kQtyLimitPolicyBytes), regengine::fix::FactSource::kOrderQty,
                    "SEBI/HO/MIRSD/2024/100:4.2.b", "Order quantity exceeds the 10,000-unit freeze limit."),
        load_bundle(kNotionalLimitPolicyBytes, sizeof(kNotionalLimitPolicyBytes), regengine::fix::FactSource::kNotionalValue,
                    "SEBI/HO/MIRSD/2024/101:3.1", "Order value exceeds the Rs. 50,00,000 per-order limit."),
    };
    regengine::fix::PolicySet policy_set{bundles, 2, 0}; // entity_type_hash=0 -- no entity constraint on either example policy

    char report[512];

    // Compliant order: qty=100, price=2500.50 -> notional=250,050 -- well under both limits.
    {
        const char msg[] =
            "8=FIX.4.4\x01" "35=D\x01" "49=BROKERCO\x01" "56=REGENGINE\x01" "34=1\x01"
            "11=ORD0001\x01" "1=UCC1\x01" "55=RELIANCE\x01" "54=1\x01" "38=100\x01" "44=2500.50\x01" "10=000\x01";
        auto result = regengine::fix::validate_new_order(policy_set, msg, sizeof(msg) - 1, "REGENGINE", "20260101-12:00:00.000", "1", report, sizeof(report));
        check(result.outcome == regengine::fix::ValidationOutcome::kAccepted, "a compliant order (qty=100, notional=250,050) is ACCEPTED");
        check(result.report_len > 0, "an execution report is built for an accepted order");
        std::string_view rep(report, result.report_len);
        check(rep.find("39=0\x01") != std::string_view::npos, "accepted order's OrdStatus (39) is 0 (New)");
    }

    // Quantity breach: qty=15000 > 10,000 limit.
    {
        const char msg[] =
            "8=FIX.4.4\x01" "35=D\x01" "49=BROKERCO\x01" "56=REGENGINE\x01" "34=2\x01"
            "11=ORD0002\x01" "1=UCC1\x01" "55=RELIANCE\x01" "54=1\x01" "38=15000\x01" "44=10\x01" "10=000\x01";
        auto result = regengine::fix::validate_new_order(policy_set, msg, sizeof(msg) - 1, "REGENGINE", "20260101-12:00:01.000", "2", report, sizeof(report));
        check(result.outcome == regengine::fix::ValidationOutcome::kRejected, "an order exceeding the quantity limit (15,000 > 10,000) is REJECTED");
        check(result.violated_meta != nullptr && std::strcmp(result.violated_meta->sebi_clause_ref, "SEBI/HO/MIRSD/2024/100:4.2.b") == 0,
              "the rejection cites the quantity-limit rule's SEBI clause, not the notional-limit rule's");
        std::string_view rep(report, result.report_len);
        check(rep.find("9001=SEBI/HO/MIRSD/2024/100:4.2.b\x01") != std::string_view::npos, "the execution report's custom tag 9001 carries the correct clause citation");
    }

    // Notional breach: qty=100, price=100000 -> notional=10,000,000 > 50,00,000 limit (quantity itself is fine).
    {
        const char msg[] =
            "8=FIX.4.4\x01" "35=D\x01" "49=BROKERCO\x01" "56=REGENGINE\x01" "34=3\x01"
            "11=ORD0003\x01" "1=UCC1\x01" "55=RELIANCE\x01" "54=1\x01" "38=100\x01" "44=100000\x01" "10=000\x01";
        auto result = regengine::fix::validate_new_order(policy_set, msg, sizeof(msg) - 1, "REGENGINE", "20260101-12:00:02.000", "3", report, sizeof(report));
        check(result.outcome == regengine::fix::ValidationOutcome::kRejected, "an order exceeding the notional limit is REJECTED even though quantity alone is compliant");
        check(result.violated_meta != nullptr && std::strcmp(result.violated_meta->sebi_clause_ref, "SEBI/HO/MIRSD/2024/101:3.1") == 0,
              "the rejection cites the notional-limit rule's SEBI clause, not the quantity-limit rule's");
    }

    // Malformed message: missing ClOrdID entirely.
    {
        const char msg[] = "8=FIX.4.4\x01" "35=D\x01" "1=UCC1\x01" "38=100\x01" "44=10\x01" "10=000\x01";
        auto result = regengine::fix::validate_new_order(policy_set, msg, sizeof(msg) - 1, "REGENGINE", "20260101-12:00:03.000", "4", report, sizeof(report));
        check(result.outcome == regengine::fix::ValidationOutcome::kMalformedMessage, "a message missing ClOrdID is flagged kMalformedMessage, never silently accepted or evaluated");
        check(result.report_len == 0, "no execution report is built for a message this gateway couldn't parse");
    }
}

} // namespace

int main() {
    test_scan_valid_new_order_single();
    test_scan_rejects_non_new_order_single();
    test_scan_flags_missing_required_tag();
    test_scan_flags_malformed_number();
    test_parse_plain_decimal_edge_cases();
    test_execution_report_wire_format();
    test_validate_new_order_end_to_end();

    if (g_failures == 0) {
        std::printf("\nALL FIX GATEWAY TESTS PASSED\n");
        return 0;
    }
    std::fprintf(stderr, "\n%d FIX GATEWAY TEST(S) FAILED\n", g_failures);
    return 1;
}
