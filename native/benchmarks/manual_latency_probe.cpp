// Dependency-free companion to bench_policy_eval.cpp (the Google
// Benchmark harness) -- built and RUN on this project's actual dev
// machine (no network access needed, unlike Google Benchmark's
// FetchContent fetch), so its numbers below are real measurements, not
// a projection. Compile directly, e.g.:
//
//   MSVC:  cl /std:c++17 /EHsc /O2 benchmarks\manual_latency_probe.cpp
//          src\c_api.cpp /Iinclude /DREGENGINE_BUILD_DLL
//   GCC:   g++ -std=c++17 -O3 -pthread benchmarks/manual_latency_probe.cpp
//          src/c_api.cpp -Iinclude -DREGENGINE_BUILD_DLL -o manual_latency_probe
//
// Methodology note (read before trusting any of this to two significant
// figures): per-call wall-clock timing via std::chrono pays the cost of
// the clock call itself on every iteration, which on most platforms is
// tens of nanoseconds -- NOT negligible next to a single-comparison
// evaluate_raw() call. This probe measures and reports that floor
// (`clock_overhead_ns`) explicitly, alongside the raw evaluate_raw()
// numbers, precisely so a reader can judge how much of the reported
// latency is the measurement apparatus itself vs. the function under
// test -- exactly the calibration discipline real HFT tooling applies,
// rather than reporting an unadjusted number and letting it imply more
// precision than a chrono-per-call loop can actually deliver.
#include <algorithm>
#include <atomic>
#include <chrono>
#include <cstdio>
#include <thread>
#include <vector>

#include "regengine/policy_engine.h"
#include "regengine/policy_loader.h"

// Prevents the optimizer from eliding the "unused" evaluate_raw() call
// entirely -- a volatile sink, deliberately simpler than
// benchmark::DoNotOptimize (this file has no Google Benchmark
// dependency by design; see this file's header comment).
volatile bool g_sink = false;
void benchmark_blackbox(bool v) { g_sink = v; }

namespace {

using Clock = std::chrono::steady_clock;

const std::uint8_t kMarginRuleRpkb1[] = {
    0x52, 0x50, 0x4b, 0x31, 0x01, 0x00, 0x0b, 0x00, 0x24, 0xb2, 0xbe, 0x3a,
    0x01, 0x00, 0x00, 0x00, 0x6d, 0x61, 0x72, 0x67, 0x69, 0x6e, 0x2d, 0x72,
    0x75, 0x6c, 0x65, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x34, 0x40,
};

struct Percentiles {
    double p50, p90, p99, p999, max;
};

Percentiles compute_percentiles(std::vector<double> &samples_ns) {
    std::sort(samples_ns.begin(), samples_ns.end());
    auto at = [&](double q) {
        std::size_t idx = std::min(samples_ns.size() - 1, static_cast<std::size_t>(samples_ns.size() * q));
        return samples_ns[idx];
    };
    return {at(0.50), at(0.90), at(0.99), at(0.999), samples_ns.back()};
}

void print_percentiles(const char *label, const Percentiles &p) {
    std::printf("%-42s p50=%8.1fns  p90=%8.1fns  p99=%8.1fns  p99.9=%8.1fns  max=%10.1fns\n",
                label, p.p50, p.p90, p.p99, p.p999, p.max);
}

double measure_clock_overhead_ns(int iterations) {
    std::vector<double> samples;
    samples.reserve(iterations);
    for (int i = 0; i < iterations; ++i) {
        const auto t0 = Clock::now();
        const auto t1 = Clock::now();
        samples.push_back(std::chrono::duration<double, std::nano>(t1 - t0).count());
    }
    std::sort(samples.begin(), samples.end());
    return samples[samples.size() / 2]; // median back-to-back now()/now() cost
}

void run_single_threaded(const regengine::CompiledPolicy &policy, std::uint32_t entity_hash, int iterations) {
    // Warm-up: let the branch predictor, instruction cache, and (on
    // Windows) the process's QPC path settle before the timed run.
    double value = 25.0;
    for (int i = 0; i < 10000; ++i) {
        value = (value == 25.0) ? 15.0 : 25.0;
        benchmark_blackbox(regengine::evaluate_raw(policy, &value, 1, entity_hash));
    }

    std::vector<double> samples;
    samples.reserve(iterations);
    for (int i = 0; i < iterations; ++i) {
        value = (value == 25.0) ? 15.0 : 25.0;
        const auto t0 = Clock::now();
        const bool result = regengine::evaluate_raw(policy, &value, 1, entity_hash);
        const auto t1 = Clock::now();
        benchmark_blackbox(result);
        samples.push_back(std::chrono::duration<double, std::nano>(t1 - t0).count());
    }

    print_percentiles("evaluate_raw() single-threaded (per-call timing)", compute_percentiles(samples));
}

// The per-call loop above times ONE evaluate_raw() call per
// Clock::now()/now() pair -- on this machine (see the printed
// clock-overhead calibration line), that resolution turned out to be
// too coarse to resolve a function this fast at all (most samples read
// back as 0ns; the timer's own tick granularity, not evaluate_raw(),
// is what a naive per-call loop actually measures here). Batched
// timing -- time a whole block of N calls, divide by N -- is the
// standard fix: it amortizes the clock's own overhead/resolution across
// many calls, at the cost of no longer being a true per-call histogram
// (branch-prediction/cache effects from the surrounding loop are
// included in each sample, same as they would be in real order-handling
// code, which arguably makes this the MORE representative number of
// the two, not a lesser one).
void run_single_threaded_batched(const regengine::CompiledPolicy &policy, std::uint32_t entity_hash, int num_batches, int batch_size) {
    double value = 25.0;
    for (int i = 0; i < 10000; ++i) {
        value = (value == 25.0) ? 15.0 : 25.0;
        benchmark_blackbox(regengine::evaluate_raw(policy, &value, 1, entity_hash));
    }

    std::vector<double> per_call_ns;
    per_call_ns.reserve(static_cast<std::size_t>(num_batches));
    for (int b = 0; b < num_batches; ++b) {
        const auto t0 = Clock::now();
        for (int i = 0; i < batch_size; ++i) {
            value = (value == 25.0) ? 15.0 : 25.0;
            benchmark_blackbox(regengine::evaluate_raw(policy, &value, 1, entity_hash));
        }
        const auto t1 = Clock::now();
        per_call_ns.push_back(std::chrono::duration<double, std::nano>(t1 - t0).count() / batch_size);
    }

    char label[80];
    std::snprintf(label, sizeof(label), "evaluate_raw() single-threaded (batches of %d)", batch_size);
    print_percentiles(label, compute_percentiles(per_call_ns));
}

void run_concurrent(const regengine::CompiledPolicy &policy, std::uint32_t entity_hash, int num_threads, int iterations_per_thread) {
    std::vector<std::vector<double>> per_thread_samples(static_cast<std::size_t>(num_threads));
    std::atomic<int> ready_count{0};
    std::atomic<bool> go{false};

    auto worker = [&](int thread_idx) {
        double value = 25.0 + thread_idx;
        for (int i = 0; i < 5000; ++i) { // per-thread warm-up
            value = (value > 40.0) ? 10.0 : value + 0.01;
            benchmark_blackbox(regengine::evaluate_raw(policy, &value, 1, entity_hash));
        }

        ready_count.fetch_add(1, std::memory_order_relaxed);
        while (!go.load(std::memory_order_acquire)) { /* spin until every thread has finished warm-up */
        }

        auto &samples = per_thread_samples[static_cast<std::size_t>(thread_idx)];
        samples.reserve(static_cast<std::size_t>(iterations_per_thread));
        for (int i = 0; i < iterations_per_thread; ++i) {
            value = (value > 40.0) ? 10.0 : value + 0.01;
            const auto t0 = Clock::now();
            const bool result = regengine::evaluate_raw(policy, &value, 1, entity_hash);
            const auto t1 = Clock::now();
            benchmark_blackbox(result);
            samples.push_back(std::chrono::duration<double, std::nano>(t1 - t0).count());
        }
    };

    std::vector<std::thread> threads;
    threads.reserve(static_cast<std::size_t>(num_threads));
    for (int i = 0; i < num_threads; ++i) {
        threads.emplace_back(worker, i);
    }
    while (ready_count.load(std::memory_order_relaxed) < num_threads) { /* wait for all warm-ups */
    }
    go.store(true, std::memory_order_release);
    for (auto &t : threads) {
        t.join();
    }

    std::vector<double> merged;
    for (auto &v : per_thread_samples) {
        merged.insert(merged.end(), v.begin(), v.end());
    }

    char label[64];
    std::snprintf(label, sizeof(label), "evaluate_raw() concurrent, %d threads", num_threads);
    print_percentiles(label, compute_percentiles(merged));
}

} // namespace

int main() {
    regengine::CompiledPolicy policy;
    const auto load_result = regengine::load_policy(kMarginRuleRpkb1, sizeof(kMarginRuleRpkb1), policy);
    if (load_result != regengine::LoadResult::kOk) {
        std::fprintf(stderr, "Failed to load fixture policy.\n");
        return 1;
    }
    const std::uint32_t entity_hash = regengine::fnv1a_hash("Stockbroker", 11);

    std::printf("RegEngine AI native policy kernel -- manual latency probe\n");
    std::printf("(std::chrono::steady_clock; see this file's header comment on clock-overhead calibration)\n\n");

    const double clock_overhead_ns = measure_clock_overhead_ns(200000);
    std::printf("measurement floor: back-to-back Clock::now()/now() median = %.1f ns\n\n", clock_overhead_ns);

    run_single_threaded(policy, entity_hash, 2000000);
    run_single_threaded_batched(policy, entity_hash, 5000, 1000);
    run_concurrent(policy, entity_hash, 1, 500000);
    run_concurrent(policy, entity_hash, 4, 500000);
    run_concurrent(policy, entity_hash, 16, 200000);
    run_concurrent(policy, entity_hash, 64, 50000);

    std::printf("\nHardware concurrency reported by std::thread: %u\n", std::thread::hardware_concurrency());
    return 0;
}
