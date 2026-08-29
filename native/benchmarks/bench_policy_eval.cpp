// Requirement 3: Google Benchmark micro-benchmark suite for
// regengine::evaluate/evaluate_raw, including p99.9 latency under
// high-concurrency (multi-threaded) order evaluation.
//
// Build (fetches Google Benchmark via CMake FetchContent -- needs
// network access at CMake-configure time):
//     cmake -S native -B native/build -DCMAKE_BUILD_TYPE=Release
//     cmake --build native/build --target bench_policy_eval --config Release
//     ./native/build/bench_policy_eval --benchmark_repetitions=50
//
// Percentiles: Google Benchmark's built-in `--benchmark_repetitions` +
// `ComputeStatistics` mechanism is used to report p50/p90/p99/p99.9
// across repetitions of each benchmark, rather than relying on a single
// run's mean -- a single-shot mean hides exactly the tail latency an
// HFT pre-trade check cares about. See `RegisterPercentileStats` below.
//
// A companion hand-rolled `<chrono>`-based harness
// (native/benchmarks/manual_latency_probe.cpp) reports the SAME
// percentiles with zero external dependencies, for a CI environment or
// sandbox (such as the one that produced this code) where fetching
// Google Benchmark over the network isn't available -- see that file
// for the actual numbers measured on this project's dev machine.
#include <algorithm>
#include <atomic>
#include <cstdint>
#include <cstring>
#include <thread>
#include <vector>

#include <benchmark/benchmark.h>

#include "regengine/policy_engine.h"
#include "regengine/policy_loader.h"

namespace {

// Same RPKB1 fixture native/tests/test_policy_engine.cpp uses -- a real
// pack_policy() artifact for "Upfront Margin >= 20% for Stockbroker",
// not a synthetic/hand-built CompiledPolicy.
const std::uint8_t kMarginRuleRpkb1[] = {
    0x52, 0x50, 0x4b, 0x31, 0x01, 0x00, 0x0b, 0x00, 0x24, 0xb2, 0xbe, 0x3a,
    0x01, 0x00, 0x00, 0x00, 0x6d, 0x61, 0x72, 0x67, 0x69, 0x6e, 0x2d, 0x72,
    0x75, 0x6c, 0x65, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x34, 0x40,
};

regengine::CompiledPolicy load_fixture_policy() {
    regengine::CompiledPolicy policy;
    const auto result = regengine::load_policy(kMarginRuleRpkb1, sizeof(kMarginRuleRpkb1), policy);
    if (result != regengine::LoadResult::kOk) {
        std::abort(); // a benchmark against a broken fixture is worse than no benchmark
    }
    return policy;
}

// Registers p50/p90/p99/p99.9 as Google Benchmark's own
// `--benchmark_repetitions` aggregate statistics, computed across
// repetitions of the SAME benchmark run (each repetition already
// internally averages over many loop iterations per Google Benchmark's
// own auto-tuned iteration count) -- this is Google Benchmark's
// documented mechanism for percentile reporting; see
// https://github.com/google/benchmark/blob/main/docs/user_guide.md#custom-statistics.
void RegisterPercentileStats(benchmark::internal::Benchmark *b) {
    b->ComputeStatistics("p50", [](const std::vector<double> &v) {
        std::vector<double> sorted(v);
        std::sort(sorted.begin(), sorted.end());
        return sorted[static_cast<std::size_t>(sorted.size() * 0.50)];
    });
    b->ComputeStatistics("p99", [](const std::vector<double> &v) {
        std::vector<double> sorted(v);
        std::sort(sorted.begin(), sorted.end());
        return sorted[static_cast<std::size_t>(sorted.size() * 0.99)];
    });
    b->ComputeStatistics("p99.9", [](const std::vector<double> &v) {
        std::vector<double> sorted(v);
        std::sort(sorted.begin(), sorted.end());
        const std::size_t idx = std::min(sorted.size() - 1, static_cast<std::size_t>(sorted.size() * 0.999));
        return sorted[idx];
    });
}

} // namespace

// --------------------------------------------------------------------------
// Single-threaded latency: the pure evaluate() call, nothing else --
// establishes the floor.
// --------------------------------------------------------------------------
static void BM_EvaluateRaw_SingleThread(benchmark::State &state) {
    const regengine::CompiledPolicy policy = load_fixture_policy();
    const std::uint32_t entity_hash = regengine::fnv1a_hash("Stockbroker", 11);
    double value = 25.0;

    for (auto _ : state) {
        // Alternate the input so the branch predictor can't fully learn
        // a single always-true/always-false pattern -- a more honest
        // (harder) measurement than a compile-time-constant input the
        // optimizer might otherwise partially fold.
        value = (value == 25.0) ? 15.0 : 25.0;
        benchmark::DoNotOptimize(value);
        const bool result = regengine::evaluate_raw(policy, &value, 1, entity_hash);
        benchmark::DoNotOptimize(result);
    }
}
BENCHMARK(BM_EvaluateRaw_SingleThread)->Apply(RegisterPercentileStats)->Repetitions(50)->ReportAggregatesOnly(true);

// --------------------------------------------------------------------------
// High-concurrency: Requirement 3's explicit ask. `->Threads(N)` runs N
// concurrent copies of this benchmark body, each on its own thread,
// against the SAME (read-only, immutable-after-load) CompiledPolicy --
// exactly the access pattern a multi-strategy OMS sharing one loaded
// policy across order-handling threads has. No locking anywhere in
// evaluate_raw (it only reads `policy`), so this measures true
// concurrent-read scaling, not lock contention.
// --------------------------------------------------------------------------
static void BM_EvaluateRaw_Concurrent(benchmark::State &state) {
    static regengine::CompiledPolicy policy = load_fixture_policy(); // constructed once, shared read-only across threads
    const std::uint32_t entity_hash = regengine::fnv1a_hash("Stockbroker", 11);
    double value = 25.0 + static_cast<double>(state.thread_index());

    for (auto _ : state) {
        value = (value > 40.0) ? 10.0 : value + 0.01;
        benchmark::DoNotOptimize(value);
        const bool result = regengine::evaluate_raw(policy, &value, 1, entity_hash);
        benchmark::DoNotOptimize(result);
    }
}
BENCHMARK(BM_EvaluateRaw_Concurrent)
    ->Apply(RegisterPercentileStats)
    ->Repetitions(20)
    ->ReportAggregatesOnly(true)
    ->Threads(1)
    ->Threads(4)
    ->Threads(16)
    ->Threads(64); // 64: the "high-concurrency" end of Requirement 3's ask -- oversubscribed on most dev machines, deliberately, to surface scheduler/cache-contention effects a low thread count would hide

// --------------------------------------------------------------------------
// The C-FFI boundary's own overhead, measured separately (Requirement
// 2's "in-memory without network overhead" claim is about THIS call,
// not a network round trip -- worth confirming the FFI indirection
// itself doesn't reintroduce a hidden cost).
// --------------------------------------------------------------------------
#include "regengine/c_api.h"

static void BM_CFfiEvaluate_SingleThread(benchmark::State &state) {
    regengine_load_result load_result;
    regengine_policy *handle = regengine_policy_load(kMarginRuleRpkb1, sizeof(kMarginRuleRpkb1), &load_result);
    const std::uint32_t entity_hash = regengine_hash_entity_type("Stockbroker", 11);
    double value = 25.0;

    for (auto _ : state) {
        value = (value == 25.0) ? 15.0 : 25.0;
        benchmark::DoNotOptimize(value);
        const int result = regengine_evaluate(handle, &value, 1, entity_hash);
        benchmark::DoNotOptimize(result);
    }

    regengine_policy_free(handle);
}
BENCHMARK(BM_CFfiEvaluate_SingleThread)->Apply(RegisterPercentileStats)->Repetitions(50)->ReportAggregatesOnly(true);

BENCHMARK_MAIN();
