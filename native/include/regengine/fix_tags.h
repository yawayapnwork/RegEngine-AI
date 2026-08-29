// FIX tag numbers and enumerated values this gateway reads or writes.
// Standard tags/values are exactly the FIX 4.2/4.4 dictionary (Volume 4
// of the spec) -- not reinvented here. One custom tag is added, in the
// user-defined range the FIX spec reserves for exactly this purpose
// (5000-9999 in FIX 4.2/4.4; some venues instead use 10000+ -- 9001 is
// arbitrary within that reserved band and MUST be agreed bilaterally
// with whatever OMS/RMS consumes this gateway's execution reports
// before going live, exactly like any other custom tag).
#pragma once

namespace regengine::fix::tags {

// --- Standard header tags ---
inline constexpr int kBeginString = 8;
inline constexpr int kBodyLength = 9;
inline constexpr int kMsgType = 35;
inline constexpr int kSenderCompID = 49;
inline constexpr int kTargetCompID = 56;
inline constexpr int kMsgSeqNum = 34;
inline constexpr int kSendingTime = 52;
inline constexpr int kCheckSum = 10;

// --- New Order Single (35=D) tags this gateway reads ---
inline constexpr int kClOrdID = 11;       // Requirement 1
inline constexpr int kAccount = 1;        // Requirement 1 -- "ClientID" in this domain's usage (see fix_tag_scanner.h)
inline constexpr int kSymbol = 55;
inline constexpr int kSide = 54;
inline constexpr int kOrdType = 40;
inline constexpr int kOrderQty = 38;      // Requirement 1
inline constexpr int kPrice = 44;         // Requirement 1

// --- Execution Report (35=8) tags this gateway writes ---
inline constexpr int kOrderID = 37;
inline constexpr int kExecID = 17;
inline constexpr int kExecType = 150;
inline constexpr int kOrdStatus = 39;
inline constexpr int kCumQty = 14;
inline constexpr int kLeavesQty = 151;
inline constexpr int kText = 58;
inline constexpr int kOrdRejReason = 103; // Requirement 3

// --- RegEngine custom tag: the specific SEBI circular/clause a
// rejection is grounded in. FIX's standard OrdRejReason (103) enum has
// no notion of a jurisdiction-specific regulatory citation -- it's a
// small, fixed set of generic reasons (see OrdRejReason below).
// Overloading Tag 58 (Text) with a machine-parseable citation would
// work but conflates a human-readable message with a structured field;
// a dedicated custom tag lets a receiving OMS/RMS parse the clause
// reference programmatically without scraping free text. ---
inline constexpr int kSebiClauseRef = 9001; // custom, user-defined range -- e.g. "SEBI/HO/MIRSD/2024/100:4.2.b"

namespace msg_type {
inline constexpr char kNewOrderSingle = 'D';
inline constexpr char kExecutionReport = '8';
}

// FIX 4.2/4.4 standard OrdStatus (Tag 39) values this gateway can emit.
namespace ord_status {
inline constexpr char kNew = '0';
inline constexpr char kRejected = '8';
}

// FIX 4.2/4.4 standard ExecType (Tag 150) values this gateway can emit.
namespace exec_type {
inline constexpr char kNew = '0';
inline constexpr char kRejected = '8';
}

// FIX 4.2/4.4 standard OrdRejReason (Tag 103) enumeration -- the
// nearest STANDARD reason each RegEngine denial maps to; the exact SEBI
// clause is always additionally carried in kSebiClauseRef + kText, this
// enum alone is never precise enough to cite regulation.
namespace ord_rej_reason {
inline constexpr int kBrokerExchangeOption = 0;
inline constexpr int kUnknownSymbol = 1;
inline constexpr int kExchangeClosed = 2;
inline constexpr int kOrderExceedsLimit = 3;   // quantity/notional/price-band breaches map here
inline constexpr int kTooLateToEnter = 4;
inline constexpr int kDuplicateOrder = 6;
inline constexpr int kIncorrectQuantity = 13;
inline constexpr int kOther = 99;              // any compliance denial without a closer-fitting standard code
}

} // namespace regengine::fix::tags
