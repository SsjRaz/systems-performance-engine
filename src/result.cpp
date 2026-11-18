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

Measurements::Measurements(const Config& config, std::string ca_bundle_sha256, std::vector<std::string> header_names) {
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
    result_.counters.requested = config.requests;
    result_.started_at_utc = utc_timestamp();
    successful_.reserve(static_cast<std::size_t>(config.requests));
    completed_.reserve(static_cast<std::size_t>(config.requests));
}

void Measurements::start() {
    if (result_.counters.started == result_.counters.requested) {
        throw std::logic_error("request count exceeded");
    }
    ++result_.counters.started;
}

void Measurements::record(Outcome outcome, double duration_ms, long http_status, int transport_error) {
    auto& c = result_.counters;
    if (c.completed + c.cancelled >= c.started) throw std::logic_error("no started request to complete");
    if (!std::isfinite(duration_ms) || duration_ms < 0) throw std::invalid_argument("invalid request duration");
    if (transport_error != 0) ++result_.transport_error_counts[transport_error];
    switch (outcome) {
    case Outcome::success: ++c.successes; successful_.push_back(duration_ms); break;
    case Outcome::http_error: ++c.http_errors; break;
    case Outcome::connection_error: ++c.connection_errors; break;
    case Outcome::network_error: ++c.network_errors; break;
    case Outcome::tls_error: ++c.tls_errors; break;
    case Outcome::timeout: ++c.timeouts; break;
    case Outcome::response_limit: ++c.response_limit_errors; break;
    case Outcome::cancelled: ++c.cancelled; return;
    }
    ++c.completed;
    completed_.push_back(duration_ms);
    if (http_status >= 100 && http_status < 600 &&
        (outcome == Outcome::success || outcome == Outcome::http_error)) {
        ++result_.http_status_counts[static_cast<std::size_t>(http_status)];
    }
}

RunResult Measurements::finish(double elapsed_seconds, std::size_t peak_in_flight,
                               bool interrupted, std::string engine_error) {
    auto& c = result_.counters;
    c.not_started = c.requested - c.started;
    if (!c.valid()) throw std::logic_error("request accounting invariant violated");
    if (!std::isfinite(elapsed_seconds) || elapsed_seconds < 0) throw std::invalid_argument("invalid elapsed time");
    result_.elapsed_seconds = elapsed_seconds;
    if (elapsed_seconds > 0) {
        result_.completed_throughput_rps = static_cast<double>(c.completed) / elapsed_seconds;
        result_.successful_throughput_rps = static_cast<double>(c.successes) / elapsed_seconds;
    }
    result_.peak_in_flight = peak_in_flight;
    result_.interrupted = interrupted;
    result_.engine_error = std::move(engine_error);
    result_.success_latency_ms = summarize_latencies(std::move(successful_));
    result_.completed_latency_ms = summarize_latencies(std::move(completed_));
    return std::move(result_);
}

std::string_view version() { return build_info::version; }

void write_json(std::ostream& out, const RunResult& r) {
    out.imbue(std::locale::classic());
    out << std::defaultfloat << std::setprecision(17);
    out << "{\n  \"schema_version\":3,\n  \"engine\":{\"version\":";
    quoted(out, version());
    out << ",\"source_revision\":"; quoted(out, build_info::revision);
    out << ",\"compiler\":"; quoted(out, build_info::compiler);
    out << ",\"build_type\":"; quoted(out, build_info::build_type);
    out << ",\"build_flags\":"; quoted(out, build_info::flags);
    out << ",\"sanitizers\":"; quoted(out, build_info::sanitizers);
    out << ",\"platform\":"; quoted(out, build_info::platform);
    out << ",\"libcurl\":"; quoted(out, curl_version());
    out << "},\n  \"started_at_utc\":"; quoted(out, r.started_at_utc);
    out << ",\n  \"workload\":{\"url\":"; quoted(out, r.config.url);
    out << ",\"method\":\"GET\",\"http_version\":\"1.1\",\"model\":\"closed_loop\","
           "\"redirects\":false,\"proxy\":false,\"connection_reuse\":true,\"warmup_requests\":0,"
           "\"application_retries\":0,\"success_policy\":\"complete_2xx\",\"requests\":" << r.config.requests
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
    out << "  \"counts\":{\"requested\":" << c.requested << ",\"started\":" << c.started
        << ",\"completed\":" << c.completed << ",\"successes\":" << c.successes
        << ",\"http_errors\":" << c.http_errors << ",\"connection_errors\":" << c.connection_errors
        << ",\"network_errors\":" << c.network_errors << ",\"tls_errors\":" << c.tls_errors << ",\"timeouts\":" << c.timeouts
        << ",\"response_limit_errors\":" << c.response_limit_errors << ",\"cancelled\":" << c.cancelled
        << ",\"not_started\":" << c.not_started << "},\n  \"http_status_counts\":{";
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
    out << std::fixed << std::setprecision(3)
        << "Completed " << c.completed << '/' << c.requested << " requests; " << c.successes << " successful\n"
        << "Elapsed: " << r.elapsed_seconds << " s; completed: " << r.completed_throughput_rps
        << " req/s; successful: " << r.successful_throughput_rps << " req/s\n"
        << "Peak in flight: " << r.peak_in_flight << " (limit " << r.config.concurrency << ")\n"
        << "Errors: HTTP=" << c.http_errors << " connection=" << c.connection_errors
        << " network=" << c.network_errors << " TLS=" << c.tls_errors << " timeout=" << c.timeouts
        << " response_limit=" << c.response_limit_errors << '\n'
        << "Incomplete: cancelled=" << c.cancelled << " not_started=" << c.not_started << '\n';
    if (c.tls_errors) for (const auto& [code, count] : r.transport_error_counts)
        if (is_tls_error(code)) out << "TLS error " << code << ": " << curl_easy_strerror(static_cast<CURLcode>(code)) << " (" << count << ")\n";
    const auto& s = r.success_latency_ms;
    if (s.samples) {
        out << "Successful request latency (ms, " << s.samples << " samples): p50=" << s.p50
            << " p95=" << s.p95 << " p99=" << s.p99 << " mean=" << s.mean << '\n';
    } else {
        out << "Successful request latency: no samples\n";
    }
    if (r.interrupted) out << "Run interrupted; partial results retained.\n";
    if (!r.engine_error.empty()) out << "Engine error: " << r.engine_error << '\n';
}
}
