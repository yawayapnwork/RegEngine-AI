// Dependency-free latency probe for the full FIX gateway hot path --
// raw NewOrderSingle bytes in, raw ExecutionReport bytes out -- mirroring
// manual_latency_probe.cpp's style (std::chrono only, no Google
// Benchmark) specifically so this can be built and run with a single
// compiler invocation and no network-fetched dependency, and so its
// numbers are always this project's own dev machine's REAL measured
// latency, not a claim.
//
// This measures validate_new_order() end to end: FIX tag scanning +
// evaluating every loaded policy + building the execution report bytes.
// It does NOT include: any FIX session-layer overhead (sequence number
// tracking, heartbeats, TCP itself), which a real deployment's
// QuickFIX/C++ (or hand-rolled) session layer adds on top -- see this
// project's README note on why the literal sub-500-microsecond target
// is a claim about THIS function, called directly from a co-located
// process, not about a full network-attached FIX session round trip.
//
// Compile directly (same convention as manual_latency_probe.cpp -- no
// CMake/network dependency needed):
//
//   MSVC:  cl /std:c++17 /EHsc /O2 benchmarks\bench_fix_gateway.cpp /Iinclude
//   GCC:   g++ -std=c++17 -O3 benchmarks/bench_fix_gateway.cpp -Iinclude -o bench_fix_gateway
//
// ACTUAL measured result, this project's dev machine, 200,000 iterations
// (2 loaded policies, /O2, MSVC 19.51/VS 2026, Windows, unpinned thread
// on a general-purpose dev machine -- not an isolated/pinned real-time
// core, which is why `max` below is an outlier next to p99.9):
//
//     mean :    443.5 ns        p50  :    400.0 ns
//     p90  :    500.0 ns        p99  :    600.0 ns
//     p99.9:   1600.0 ns        max  : 387600.0 ns
//
// At steady state this is roughly 800-1000x under the 500-microsecond
// (500,000ns) budget -- the margin exists precisely BECAUSE this path
// never touches OPA-over-HTTP (app.execution.opa_engine's own docstring
// states "low single-digit milliseconds" for that path, 3-4 orders of
// magnitude over budget) or the pybind11 binding (which pybind_module.cpp's
// own docstring disclaims for exactly this kind of claim) -- only the
// pre-existing, already-benchmarked native kernel (policy_engine.h)
// plus this file's allocation-free FIX framing around it.
#include <algorithm>
#include <chrono>
#include <cstdio>
#include <cstring>
#include <vector>

#include "regengine/fix_gateway.h"
#include "regengine/policy_loader.h"

namespace {

// Same RPKB1 bytes as native/tests/test_fix_gateway.cpp (see that
// file's header comment for the exact pack_policy() calls that
// produced them) -- kept in sync manually since this benchmark has no
// build-time dependency on the Python packager.
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

regengine::fix::PolicyBundle load_bundle(const std::uint8_t *rpkb1, std::size_t len, regengine::fix::FactSource source0) {
    regengine::fix::PolicyBundle bundle;
    const auto load_result = regengine::load_policy(rpkb1, len, bundle.policy);
    if (load_result != regengine::LoadResult::kOk) {
        std::fprintf(stderr, "fatal: failed to load benchmark fixture policy\n");
        std::exit(1);
    }
    bundle.num_slots = 1;
    bundle.fact_sources[0] = source0;
    bundle.rejection.ord_rej_reason = regengine::fix::tags::ord_rej_reason::kOrderExceedsLimit;
    std::snprintf(bundle.rejection.text, sizeof(bundle.rejection.text), "Order parameter exceeds a compiled SEBI limit.");
    std::snprintf(bundle.rejection.sebi_clause_ref, sizeof(bundle.rejection.sebi_clause_ref), "SEBI/HO/MIRSD/2024/100:4.2.b");
    return bundle;
}

double percentile(std::vector<double> &sorted_ns, double p) {
    std::size_t idx = static_cast<std::size_t>(p * static_cast<double>(sorted_ns.size() - 1));
    return sorted_ns[idx];
}

} // namespace

int main() {
    regengine::fix::PolicyBundle bundles[2] = {
        load_bundle(kQtyLimitPolicyBytes, sizeof(kQtyLimitPolicyBytes), regengine::fix::FactSource::kOrderQty),
        load_bundle(kNotionalLimitPolicyBytes, sizeof(kNotionalLimitPolicyBytes), regengine::fix::FactSource::kNotionalValue),
    };
    regengine::fix::PolicySet policy_set{bundles, 2, 0};

    const char msg[] =
        "8=FIX.4.4\x01" "35=D\x01" "49=BROKERCO\x01" "56=REGENGINE\x01" "34=1\x01"
        "11=ORD0001\x01" "1=UCC12345\x01" "55=RELIANCE\x01" "54=1\x01" "38=100\x01" "44=2500.50\x01" "10=000\x01";
    const std::size_t msg_len = sizeof(msg) - 1;

    constexpr int kWarmupIters = 10000;
    constexpr int kMeasuredIters = 200000;
    char report[512];

    for (int i = 0; i < kWarmupIters; ++i) {
        auto warmup_result = regengine::fix::validate_new_order(policy_set, msg, msg_len, "REGENGINE", "20260101-12:00:00.000", "1", report, sizeof(report));
        (void)warmup_result;
    }

    std::vector<double> samples_ns;
    samples_ns.reserve(kMeasuredIters);
    for (int i = 0; i < kMeasuredIters; ++i) {
        auto start = std::chrono::high_resolution_clock::now();
        auto result = regengine::fix::validate_new_order(policy_set, msg, msg_len, "REGENGINE", "20260101-12:00:00.000", "1", report, sizeof(report));
        auto end = std::chrono::high_resolution_clock::now();
        if (result.outcome != regengine::fix::ValidationOutcome::kAccepted) {
            std::fprintf(stderr, "unexpected outcome in benchmark loop\n");
            return 1;
        }
        samples_ns.push_back(std::chrono::duration<double, std::nano>(end - start).count());
    }

    std::sort(samples_ns.begin(), samples_ns.end());
    double sum = 0;
    for (double v : samples_ns) sum += v;

    std::printf("validate_new_order() end-to-end latency, %d iterations (2 loaded policies, direct C++ call, no FIX session layer):\n", kMeasuredIters);
    std::printf("  mean : %8.1f ns\n", sum / samples_ns.size());
    std::printf("  p50  : %8.1f ns\n", percentile(samples_ns, 0.50));
    std::printf("  p90  : %8.1f ns\n", percentile(samples_ns, 0.90));
    std::printf("  p99  : %8.1f ns\n", percentile(samples_ns, 0.99));
    std::printf("  p99.9: %8.1f ns\n", percentile(samples_ns, 0.999));
    std::printf("  max  : %8.1f ns\n", samples_ns.back());
    std::printf("\n500 microseconds = 500,000 ns, for reference.\n");
    return 0;
}
