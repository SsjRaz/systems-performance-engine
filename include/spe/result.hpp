#pragma once

#include "spe/config.hpp"

#include <array>
#include <cstdint>
#include <iosfwd>
#include <map>
#include <string>
#include <vector>

namespace spe {
enum class Outcome { success, http_error, connection_error, network_error, tls_error, timeout, response_limit, cancelled };

struct Counters {
    std::uint64_t requested = 0;
    std::uint64_t started = 0;
    std::uint64_t completed = 0;
    std::uint64_t successes = 0;
    std::uint64_t http_errors = 0;
    std::uint64_t connection_errors = 0;
    std::uint64_t network_errors = 0;
    std::uint64_t tls_errors = 0;
    std::uint64_t timeouts = 0;
    std::uint64_t response_limit_errors = 0;
    std::uint64_t cancelled = 0;
    std::uint64_t not_started = 0;
    bool valid() const;
};

struct LatencySummary {
    std::uint64_t samples = 0;
    double min = 0;
    double mean = 0;
    double p50 = 0;
    double p95 = 0;
    double p99 = 0;
    double max = 0;
};

// Exact nearest-rank percentiles. Empty sets serialize as null, not zero latency.
LatencySummary summarize_latencies(std::vector<double> samples);

struct SchedulingStats {
    std::uint64_t missed_late = 0;
    std::uint64_t missed_capacity = 0;
    std::uint64_t offered = 0;
    double window_seconds = 0;
    double offered_rps = 0;
    double dispatched_rps = 0;
    LatencySummary dispatch_delay_ms;
};

struct EndpointResult {
    Counters counters;
    std::uint64_t missed_late = 0;
    std::uint64_t missed_capacity = 0;
    LatencySummary success_latency_ms;
    LatencySummary completed_latency_ms;
};

struct RunResult {
    Config config;
    Counters counters;
    SchedulingStats scheduling;
    std::vector<EndpointResult> endpoints;
    LatencySummary success_latency_ms;
    LatencySummary completed_latency_ms;
    std::array<std::uint64_t, 600> http_status_counts{};
    std::map<int, std::uint64_t> transport_error_counts;
    std::string started_at_utc;
    std::string engine_error;
    std::string ca_bundle_sha256;
    std::vector<std::string> header_names;
    std::size_t request_body_bytes = 0;
    bool interrupted = false;
    std::size_t peak_in_flight = 0;
    double elapsed_seconds = 0;
    double completed_throughput_rps = 0;
    double successful_throughput_rps = 0;
};

// Owned and mutated exclusively by the event-loop thread.
class Measurements {
public:
    explicit Measurements(const Config& config, std::string ca_bundle_sha256 = {},
                          std::vector<std::string> header_names = {}, std::size_t request_body_bytes = 0);
    void start(std::optional<double> dispatch_delay_ms = {}, std::size_t endpoint = 0);
    void miss_arrivals(std::uint64_t expired, bool at_capacity, std::uint64_t first = 0);
    void record(Outcome outcome, double duration_ms, long http_status = 0, int transport_error = 0, std::size_t endpoint = 0);
    const Counters& counters() const { return result_.counters; }
    RunResult finish(double elapsed_seconds, std::size_t peak_in_flight,
                     bool interrupted, std::string engine_error = {});

private:
    std::uint64_t remaining_arrivals() const;
    RunResult result_;
    struct Samples {
        std::vector<double> successful;
        std::vector<double> completed;
        void reserve(std::size_t count);
        void record(Counters& counts, Outcome outcome, double duration_ms);
    };
    Samples global_samples_;
    std::vector<Samples> endpoint_samples_;
    std::vector<double> dispatch_delays_;
};

void write_json(std::ostream& output, const RunResult& result);
void write_summary(std::ostream& output, const RunResult& result);
std::string_view version();
}
