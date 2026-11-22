#include "spe/result.hpp"
#include "spe/build_info.hpp"
#include "spe/tls.hpp"
#include "spe/headers.hpp"

#include <curl/curl.h>

#include <algorithm>
#include <chrono>
#include <cmath>
#include <ctime>
#include <iomanip>
#include <locale>
#include <numeric>
#include <ostream>
#include <sstream>
#include <stdexcept>
#include <utility>

namespace spe {
namespace {
void quoted(std::ostream& out, std::string_view value) {
    constexpr char hex[] = "0123456789abcdef";
    out << '"';
    for (const char character : value) {
        const auto c = static_cast<unsigned char>(character);
        switch (c) {
        case '"': out << "\\\""; break;
        case '\\': out << "\\\\"; break;
        case '\n': out << "\\n"; break;
        case '\r': out << "\\r"; break;
        case '\t': out << "\\t"; break;
        default:
            if (c < 32) out << "\\u00" << hex[c >> 4] << hex[c & 15];
            else out << static_cast<char>(c);
        }
    }
    out << '"';
}

double throughput(std::uint64_t count, double elapsed_seconds) {
    return elapsed_seconds > 0 ? static_cast<double>(count) / elapsed_seconds : 0;
}

void counts_json(std::ostream& out, const Counters& c) {
    out << "{\"requested\":" << c.requested << ",\"started\":" << c.started
        << ",\"completed\":" << c.completed << ",\"successes\":" << c.successes
        << ",\"http_errors\":" << c.http_errors << ",\"connection_errors\":" << c.connection_errors
        << ",\"network_errors\":" << c.network_errors << ",\"tls_errors\":" << c.tls_errors << ",\"timeouts\":" << c.timeouts
        << ",\"response_limit_errors\":" << c.response_limit_errors << ",\"cancelled\":" << c.cancelled
        << ",\"not_started\":" << c.not_started << '}';
}

void latency_json(std::ostream& out, const LatencySummary& stats) {
    out << "{\"samples\":" << stats.samples;
    const auto field = [&](std::string_view name, double value) {
        out << ",\"" << name << "\":";
        if (stats.samples == 0) out << "null";
        else out << value;
    };
    field("min", stats.min);
    field("mean", stats.mean);
    field("p50", stats.p50);
    field("p95", stats.p95);
    field("p99", stats.p99);
    field("max", stats.max);
    out << '}';
}

std::string utc_timestamp() {
    const auto now = std::chrono::system_clock::to_time_t(std::chrono::system_clock::now());
    std::tm utc{};
    if (gmtime_r(&now, &utc) == nullptr) throw std::runtime_error("cannot format timestamp");
    std::ostringstream out;
    out << std::put_time(&utc, "%Y-%m-%dT%H:%M:%SZ");
    return out.str();
}
}

bool Counters::valid() const {
    return requested == started + not_started && started == completed + cancelled &&
           completed == successes + http_errors + connection_errors + network_errors + tls_errors + timeouts + response_limit_errors;
}

LatencySummary summarize_latencies(std::vector<double> samples) {
    if (samples.empty()) return {};
    if (std::any_of(samples.begin(), samples.end(), [](double x) { return !std::isfinite(x) || x < 0; })) {
        throw std::invalid_argument("latency samples must be finite and nonnegative");
    }
    std::sort(samples.begin(), samples.end());
    const auto rank = [&](std::size_t percent) {
        // Exact ceil(N * percent / 100), with N bounded by max_requests.
        return samples[(samples.size() * percent + 99) / 100 - 1];
    };
    const auto sum = std::accumulate(samples.begin(), samples.end(), 0.0L);
    return {static_cast<std::uint64_t>(samples.size()), samples.front(),
            static_cast<double>(sum / static_cast<long double>(samples.size())),
            rank(50), rank(95), rank(99), samples.back()};
}

Measurements::Measurements(const Config& config, std::string ca_bundle_sha256,
                           std::vector<std::string> header_names, std::size_t request_body_bytes) {
    validate_config(config);
    if (config.ca_file.empty() != ca_bundle_sha256.empty() || (!ca_bundle_sha256.empty() &&
        (ca_bundle_sha256.size() != 64 || ca_bundle_sha256.find_first_not_of("0123456789abcdef") != std::string::npos)))
        throw std::invalid_argument("custom CA bundle requires a prepared SHA-256 identity");
    if (header_names.size() > 32 || (config.headers_file.empty() && !header_names.empty()) ||
        !std::is_sorted(header_names.begin(), header_names.end()) ||
        std::adjacent_find(header_names.begin(), header_names.end()) != header_names.end() ||
        std::any_of(header_names.begin(), header_names.end(), [](const auto& name) { return !is_normalized_header_name(name); }))
        throw std::invalid_argument("invalid prepared header metadata");
    result_.config = config;
    result_.ca_bundle_sha256 = std::move(ca_bundle_sha256);
    result_.header_names = std::move(header_names);
    if (request_body_bytes > max_request_body_bytes || (request_body_bytes && config.body_file.empty()))
        throw std::invalid_argument("invalid prepared request body size");
    result_.request_body_bytes = request_body_bytes;
    const auto budget = static_cast<std::size_t>(config.request_budget());
    result_.counters.requested = budget;
    result_.started_at_utc = utc_timestamp();
    global_samples_.reserve(budget);
    if (config.mix) {
        result_.endpoints.resize(config.mix->size);
        endpoint_samples_.resize(config.mix->size);
        for (std::size_t i = 0; i < result_.endpoints.size(); ++i) {
            const auto planned = config.mix->count_before(i, budget);
            result_.endpoints[i].counters.requested = planned;
            endpoint_samples_[i].reserve(static_cast<std::size_t>(planned));
        }
    }
    if (config.fixed_rate) dispatch_delays_.reserve(budget);
}

std::uint64_t Measurements::remaining_arrivals() const {
    const auto& c = result_.counters;
    return c.requested - c.started - result_.scheduling.missed_late - result_.scheduling.missed_capacity;
}

void Measurements::start(std::optional<double> dispatch_delay_ms, std::size_t endpoint) {
    if (result_.config.fixed_rate.has_value() != dispatch_delay_ms.has_value())
        throw std::logic_error("dispatch timing must match the workload model");
    if (remaining_arrivals() == 0) {
        throw std::logic_error("request count exceeded");
    }
    if (!result_.endpoints.empty()) {
        const auto& e = result_.endpoints.at(endpoint);
        if (e.counters.started + e.missed_capacity + e.missed_late >= e.counters.requested)
            throw std::logic_error("endpoint request budget exhausted");
    }
    if (dispatch_delay_ms) {
        if (!std::isfinite(*dispatch_delay_ms) || *dispatch_delay_ms < 0)
            throw std::logic_error("invalid dispatch delay");
        dispatch_delays_.push_back(*dispatch_delay_ms);
    }
    if (!result_.endpoints.empty()) ++result_.endpoints.at(endpoint).counters.started;
    ++result_.counters.started;
}

void Measurements::miss_arrivals(std::uint64_t expired, bool at_capacity, std::uint64_t first) {
    const auto remaining = remaining_arrivals();
    if (!result_.config.fixed_rate || expired > remaining || (at_capacity && expired == remaining))
        throw std::logic_error("invalid missed arrival count");
    if (const auto& mix = result_.config.mix; mix && (expired || at_capacity)) {
        if (first != result_.counters.requested - remaining) throw std::logic_error("non-contiguous arrival range");
        const auto end = first + expired;
        for (std::size_t i = 0; i < result_.endpoints.size(); ++i)
            result_.endpoints[i].missed_late += mix->count_before(i, end) - mix->count_before(i, first);
        if (at_capacity) ++result_.endpoints[mix->endpoint_at(end)].missed_capacity;
    }
    result_.scheduling.missed_late += expired;
    if (at_capacity) ++result_.scheduling.missed_capacity;
}

void Measurements::Samples::reserve(std::size_t count) {
    successful.reserve(count);
    completed.reserve(count);
}

void Measurements::Samples::record(Counters& c, Outcome outcome, double duration_ms) {
    switch (outcome) {
    case Outcome::success: ++c.successes; successful.push_back(duration_ms); break;
    case Outcome::http_error: ++c.http_errors; break;
    case Outcome::connection_error: ++c.connection_errors; break;
    case Outcome::network_error: ++c.network_errors; break;
    case Outcome::tls_error: ++c.tls_errors; break;
    case Outcome::timeout: ++c.timeouts; break;
    case Outcome::response_limit: ++c.response_limit_errors; break;
    case Outcome::cancelled: ++c.cancelled; return;
    }
    ++c.completed;
    completed.push_back(duration_ms);
}

void Measurements::record(Outcome outcome, double duration_ms, long http_status, int transport_error, std::size_t endpoint) {
    if (!result_.endpoints.empty()) {
        const auto& local = result_.endpoints.at(endpoint).counters;
        if (local.completed + local.cancelled >= local.started) throw std::logic_error("no active request for endpoint");
    }
    auto& c = result_.counters;
    if (c.completed + c.cancelled >= c.started) throw std::logic_error("no started request to complete");
    if (!std::isfinite(duration_ms) || duration_ms < 0) throw std::invalid_argument("invalid request duration");
    if (transport_error != 0) ++result_.transport_error_counts[transport_error];
    global_samples_.record(c, outcome, duration_ms);
    if (!result_.endpoints.empty())
        endpoint_samples_[endpoint].record(result_.endpoints[endpoint].counters, outcome, duration_ms);
    if (http_status >= 100 && http_status < 600 &&
        (outcome == Outcome::success || outcome == Outcome::http_error)) {
        ++result_.http_status_counts[static_cast<std::size_t>(http_status)];
    }
}

RunResult Measurements::finish(double elapsed_seconds, std::size_t peak_in_flight,
                               bool interrupted, std::string engine_error, std::optional<Verification> verification) {
    result_.verification = std::move(verification);
    auto& c = result_.counters;
    c.not_started = c.requested - c.started;
    if (!c.valid()) throw std::logic_error("request accounting invariant violated");
    if (!std::isfinite(elapsed_seconds) || elapsed_seconds < 0) throw std::invalid_argument("invalid elapsed time");
    if (const auto rate = result_.config.fixed_rate) {
        auto& s = result_.scheduling;
        s.offered = c.started + s.missed_late + s.missed_capacity;
        const bool probe_failed = result_.verification && !result_.verification->failure.empty();
        if (s.offered > c.requested || (!interrupted && engine_error.empty() && !probe_failed && s.offered != c.requested))
            throw std::logic_error("arrival accounting invariant violated");
        s.window_seconds = std::min(elapsed_seconds, static_cast<double>(rate->duration_ms) / 1000);
        if (s.window_seconds > 0) {
            s.offered_rps = static_cast<double>(s.offered) / s.window_seconds;
            s.dispatched_rps = static_cast<double>(c.started) / s.window_seconds;
        }
        s.dispatch_delay_ms = summarize_latencies(std::move(dispatch_delays_));
    }
    result_.elapsed_seconds = elapsed_seconds;
    if (elapsed_seconds > 0) {
        result_.completed_throughput_rps = static_cast<double>(c.completed) / elapsed_seconds;
        result_.successful_throughput_rps = static_cast<double>(c.successes) / elapsed_seconds;
    }
    result_.peak_in_flight = peak_in_flight;
    result_.interrupted = interrupted;
    result_.engine_error = std::move(engine_error);
    for (std::size_t i = 0; i < result_.endpoints.size(); ++i) {
        auto& e = result_.endpoints[i];
        e.counters.not_started = e.counters.requested - e.counters.started;
        const auto offered = e.counters.started + e.missed_late + e.missed_capacity;
        if (!e.counters.valid() || offered != result_.config.mix->count_before(i, result_.scheduling.offered))
            throw std::logic_error("endpoint arrival accounting invariant violated");
        e.success_latency_ms = summarize_latencies(std::move(endpoint_samples_[i].successful));
        e.completed_latency_ms = summarize_latencies(std::move(endpoint_samples_[i].completed));
    }
    result_.success_latency_ms = summarize_latencies(std::move(global_samples_.successful));
    result_.completed_latency_ms = summarize_latencies(std::move(global_samples_.completed));
    return std::move(result_);
}

std::string_view version() { return build_info::version; }

void write_json(std::ostream& out, const RunResult& r) {
    out.imbue(std::locale::classic());
    out << std::defaultfloat << std::setprecision(17);
    out << "{\n  \"schema_version\":" << (r.verification ? 7 : 6) << ",\n  \"engine\":{\"version\":";
    quoted(out, version());
    out << ",\"source_revision\":"; quoted(out, build_info::revision);
    out << ",\"compiler\":"; quoted(out, build_info::compiler);
    out << ",\"build_type\":"; quoted(out, build_info::build_type);
    out << ",\"build_flags\":"; quoted(out, build_info::flags);
    out << ",\"sanitizers\":"; quoted(out, build_info::sanitizers);
    out << ",\"platform\":"; quoted(out, build_info::platform);
    out << ",\"libcurl\":"; quoted(out, curl_version());
    out << "},\n  \"started_at_utc\":"; quoted(out, r.started_at_utc);
    if (r.verification) {
        const auto& v = *r.verification;
        out << ",\n  \"verification\":{\"scope\":\"untimed_preflight\",\"policy_id\":";
        quoted(out, v.policy_id);
        out << ",\"status\":"; quoted(out, v.failure.empty() ? "passed" : "failed");
        out << ",\"attempted\":" << v.attempted << ",\"passed\":" << v.passed << ",\"failure\":";
        if (v.failure.empty()) out << "null"; else quoted(out, v.failure);
        out << '}';
    }
    out << ",\n  \"workload\":{\"url\":"; quoted(out, r.config.url);
    out << ",\"method\":"; quoted(out, r.config.method);
    out << ",\"request_body_bytes\":" << r.request_body_bytes << ",\"http_version\":\"1.1\",\"model\":";
    quoted(out, r.config.fixed_rate ? "fixed_arrival_rate" : "closed_loop");
    out << ",\"arrival_schedule\":";
    if (const auto rate = r.config.fixed_rate) {
        out << "{\"target_rps\":" << rate->per_second << ",\"duration_ms\":" << rate->duration_ms
            << ",\"overload_policy\":\"skip_expired_drop_at_capacity\"}";
    } else out << "null";
    out << ",\"endpoint_mix\":";
    if (const auto& mix = r.config.mix) {
        out << "{\"assignment\":"; quoted(out, EndpointMix::assignment);
        out << ",\"endpoints\":[";
        for (std::size_t i = 0; i < mix->size; ++i) {
            if (i) out << ',';
            const auto& e = mix->endpoints()[i];
            out << "{\"path\":"; quoted(out, e.path);
            out << ",\"weight\":" << e.weight << '}';
        }
        out << "]}";
    } else out << "null";
    out << ",\"redirects\":false,\"proxy\":false,\"connection_reuse\":"
        << (method_reuses_connections(r.config.method) ? "true" : "false") << ",\"warmup_requests\":0,"
           "\"application_retries\":0,\"success_policy\":\"complete_2xx\",\"requests\":" << r.counters.requested
        << ",\"concurrency\":" << r.config.concurrency << ",\"timeout_ms\":" << r.config.timeout_ms
        << ",\"max_body_bytes\":" << r.config.max_body_bytes << ",\"tls\":";
    if (is_https_url(r.config.url)) {
        out << "{\"verify_peer\":true,\"verify_host\":true,\"minimum_version\":\"1.2\",\"ca_source\":";
        quoted(out, r.config.ca_file.empty() ? "libcurl_default" : "custom_bundle");
        out << ",\"ca_bundle_sha256\":";
        if (r.ca_bundle_sha256.empty()) out << "null";
        else quoted(out, r.ca_bundle_sha256);
        out << '}';
    } else out << "null";
    out << ",\"headers\":{\"names\":[";
    for (std::size_t i = 0; i < r.header_names.size(); ++i) {
        if (i) out << ',';
        quoted(out, r.header_names[i]);
    }
    out << "],\"values\":\"redacted\"}},\n";
    const auto& c = r.counters;
    out << "  \"scheduling\":";
    if (r.config.fixed_rate) {
        const auto& s = r.scheduling;
        out << "{\"offered\":" << s.offered << ",\"missed_capacity\":" << s.missed_capacity
            << ",\"missed_late\":" << s.missed_late << ",\"not_evaluated\":" << c.requested - s.offered
            << ",\"window_seconds\":" << s.window_seconds << ",\"offered_rps\":" << s.offered_rps
            << ",\"dispatched_rps\":" << s.dispatched_rps << ",\"dispatch_delay_ms\":";
        latency_json(out, s.dispatch_delay_ms);
        out << '}';
    } else out << "null";
    out << ",\n";
    out << "  \"endpoint_results\":";
    if (r.config.mix) {
        out << '[';
        for (std::size_t i = 0; i < r.endpoints.size(); ++i) {
            const auto& e = r.endpoints[i];
            const auto& counts = e.counters;
            if (i) out << ',';
            out << "{\"path\":"; quoted(out, r.config.mix->endpoints()[i].path);
            out << ",\"counts\":"; counts_json(out, counts);
            out << ",\"failed\":" << counts.completed - counts.successes
                << ",\"missed_capacity\":" << e.missed_capacity << ",\"missed_late\":" << e.missed_late
                << ",\"not_evaluated\":" << counts.not_started - e.missed_capacity - e.missed_late
                << ",\"completed_throughput_rps\":" << throughput(counts.completed, r.elapsed_seconds)
                << ",\"successful_throughput_rps\":" << throughput(counts.successes, r.elapsed_seconds)
                << ",\"success_latency_ms\":"; latency_json(out, e.success_latency_ms);
            out << ",\"completed_latency_ms\":"; latency_json(out, e.completed_latency_ms);
            out << '}';
        }
        out << ']';
    } else out << "null";
    out << ",\n  \"counts\":"; counts_json(out, c);
    out << ",\n  \"http_status_counts\":{";
    bool first = true;
    for (std::size_t status = 100; status < r.http_status_counts.size(); ++status) {
        if (r.http_status_counts[status] == 0) continue;
        if (!first) out << ',';
        first = false;
        quoted(out, std::to_string(status)); out << ':' << r.http_status_counts[status];
    }
    out << "},\n  \"transport_error_counts\":{";
    first = true;
    for (const auto& [code, count] : r.transport_error_counts) {
        if (!first) out << ',';
        first = false;
        quoted(out, std::to_string(code));
        out << ":{\"count\":" << count << ",\"description\":";
        quoted(out, curl_easy_strerror(static_cast<CURLcode>(code))); out << '}';
    }
    out << "},\n  \"elapsed_seconds\":" << r.elapsed_seconds
        << ",\n  \"completed_throughput_rps\":" << r.completed_throughput_rps
        << ",\n  \"successful_throughput_rps\":" << r.successful_throughput_rps
        << ",\n  \"peak_in_flight\":" << r.peak_in_flight
        << ",\n  \"interrupted\":" << (r.interrupted ? "true" : "false")
        << ",\n  \"engine_error\":";
    if (r.engine_error.empty()) out << "null";
    else quoted(out, r.engine_error);
    out << ",\n  \"success_latency_ms\":"; latency_json(out, r.success_latency_ms);
    out << ",\n  \"completed_latency_ms\":"; latency_json(out, r.completed_latency_ms);
    out << "\n}\n";
}

void write_summary(std::ostream& out, const RunResult& r) {
    const auto& c = r.counters;
    if (r.verification) {
        const auto& v = *r.verification;
        out << "Untimed response verification " << (v.failure.empty() ? "passed" : v.failure)
            << " (" << v.passed << '/' << v.attempted << " probes passed)\n";
    } else out << "Response correctness unchecked\n";
    out << std::fixed << std::setprecision(3)
        << "Completed " << c.completed << '/' << c.requested << " requests; " << c.successes << " successful\n"
        << "Elapsed: " << r.elapsed_seconds << " s; completed: " << r.completed_throughput_rps
        << " req/s; successful: " << r.successful_throughput_rps << " req/s\n"
        << "Peak in flight: " << r.peak_in_flight << " (limit " << r.config.concurrency << ")\n"
        << "Errors: HTTP=" << c.http_errors << " connection=" << c.connection_errors
        << " network=" << c.network_errors << " TLS=" << c.tls_errors << " timeout=" << c.timeouts
        << " response_limit=" << c.response_limit_errors << '\n'
        << "Incomplete: cancelled=" << c.cancelled << " not_started=" << c.not_started << '\n';
    if (const auto rate = r.config.fixed_rate) {
        const auto& s = r.scheduling;
        out << "Arrivals: planned=" << c.requested << " offered=" << s.offered << " dispatched=" << c.started
            << " missed_capacity=" << s.missed_capacity << " missed_late=" << s.missed_late
            << " not_evaluated=" << c.requested - s.offered << '\n'
            << "Arrival window: " << s.window_seconds << " s; target=" << rate->per_second
            << " offered=" << s.offered_rps << " dispatched=" << s.dispatched_rps << " req/s\n";
        if (s.dispatch_delay_ms.samples)
            out << "Dispatch delay (ms): p50=" << s.dispatch_delay_ms.p50 << " p95=" << s.dispatch_delay_ms.p95
                << " p99=" << s.dispatch_delay_ms.p99 << " max=" << s.dispatch_delay_ms.max << '\n';
    }
    if (c.tls_errors) for (const auto& [code, count] : r.transport_error_counts)
        if (is_tls_error(code)) out << "TLS error " << code << ": " << curl_easy_strerror(static_cast<CURLcode>(code)) << " (" << count << ")\n";
    const auto& s = r.success_latency_ms;
    if (s.samples) {
        out << "Successful request latency (ms, " << s.samples << " samples): p50=" << s.p50
            << " p95=" << s.p95 << " p99=" << s.p99 << " mean=" << s.mean << '\n';
    } else {
        out << "Successful request latency: no samples\n";
    }
    for (std::size_t i = 0; i < r.endpoints.size(); ++i) {
        const auto& e = r.endpoints[i];
        const auto& counts = e.counters;
        out << r.config.mix->endpoints()[i].path << " planned=" << counts.requested << " started=" << counts.started
            << " completed=" << counts.completed << " successful=" << counts.successes << " failed=" << counts.completed - counts.successes
            << " cancelled=" << counts.cancelled << " missed_capacity=" << e.missed_capacity << " missed_late=" << e.missed_late
            << " not_evaluated=" << counts.not_started - e.missed_capacity - e.missed_late
            << " successful/s=" << throughput(counts.successes, r.elapsed_seconds);
        if (e.success_latency_ms.samples) out << " successful_p95_ms=" << e.success_latency_ms.p95;
        if (e.completed_latency_ms.samples) out << " completed_p95_ms=" << e.completed_latency_ms.p95;
        out << '\n';
    }
    if (r.interrupted) out << "Run interrupted; partial results retained.\n";
    if (!r.engine_error.empty()) out << "Engine error: " << r.engine_error << '\n';
}
}
