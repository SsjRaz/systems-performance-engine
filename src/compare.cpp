#include "spe/compare.hpp"
#include "spe/config.hpp"
#include "spe/tls.hpp"
#include "spe/headers.hpp"

#include <curl/curl.h>
#include <nlohmann/json.hpp>

#include <algorithm>
#include <array>
#include <cerrno>
#include <charconv>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <fcntl.h>
#include <iomanip>
#include <limits>
#include <locale>
#include <map>
#include <memory>
#include <optional>
#include <set>
#include <sstream>
#include <stdexcept>
#include <sys/stat.h>
#include <system_error>
#include <unistd.h>
#include <utility>

namespace spe {
namespace {
using Json = nlohmann::json;
constexpr std::size_t max_file_bytes = 1024 * 1024;
constexpr std::size_t max_total_bytes = 32 * 1024 * 1024;
constexpr std::size_t max_group_runs = 100;
constexpr std::array count_names = {"requested", "started", "completed", "successes", "http_errors",
    "connection_errors", "network_errors", "tls_errors", "timeouts", "response_limit_errors", "cancelled", "not_started"};
constexpr std::array latency_names = {"min", "mean", "p50", "p95", "p99", "max"};

void require(bool condition, const std::string& message) {
    if (!condition) throw std::invalid_argument(message);
}

void keys(const Json& object, std::initializer_list<std::string_view> names, std::string_view location) {
    require(object.is_object(), std::string(location) + " must be an object");
    require(object.size() == names.size(), std::string(location) + " has missing or unsupported fields");
    for (auto name : names) require(object.contains(name), std::string(location) + " missing " + std::string(name));
}

std::uint64_t integer(const Json& value, std::string_view name, std::uint64_t maximum) {
    require(value.is_number_unsigned(), std::string(name) + " must be an unsigned integer");
    const auto result = value.get<std::uint64_t>();
    require(result <= maximum, std::string(name) + " exceeds its supported bound");
    return result;
}

double number(const Json& value, std::string_view name) {
    require(value.is_number(), std::string(name) + " must be a number");
    const auto result = value.get<double>();
    require(std::isfinite(result) && result >= 0, std::string(name) + " must be finite and nonnegative");
    return result;
}

std::string string(const Json& value, std::string_view name, std::size_t maximum, bool empty = false) {
    require(value.is_string(), std::string(name) + " must be a string");
    auto result = value.get<std::string>();
    require((empty || !result.empty()) && result.size() <= maximum, std::string(name) + " has invalid length");
    return result;
}

bool close(double a, double b) {
    return std::isfinite(a) && std::isfinite(b) &&
        std::abs(a - b) <= 1e-9 * std::max(std::abs(a), std::abs(b));
}

void timestamp(const Json& value) {
    const auto text = string(value, "started_at_utc", 20);
    require(text.size() == 20 && text[4] == '-' && text[7] == '-' && text[10] == 'T' &&
            text[13] == ':' && text[16] == ':' && text[19] == 'Z', "invalid native UTC timestamp");
    const auto part = [&](std::size_t begin, std::size_t length) {
        int n = 0;
        const auto [end, error] = std::from_chars(text.data() + begin, text.data() + begin + length, n);
        require(std::all_of(text.begin() + static_cast<std::ptrdiff_t>(begin), text.begin() + static_cast<std::ptrdiff_t>(begin + length),
                    [](char c) { return c >= '0' && c <= '9'; }) &&
                    error == std::errc{} && end == text.data() + begin + length, "invalid UTC timestamp digits");
        return n;
    };
    const std::chrono::year_month_day day{std::chrono::year{part(0, 4)},
        std::chrono::month{static_cast<unsigned>(part(5, 2))}, std::chrono::day{static_cast<unsigned>(part(8, 2))}};
    require(day.ok() && part(11, 2) < 24 && part(14, 2) < 60 && part(17, 2) <= 60, "invalid UTC timestamp value");
}

struct Target { std::string origin, endpoint; };
Target target(const std::string& text) {
    const std::unique_ptr<CURLU, decltype(&curl_url_cleanup)> url(curl_url(), curl_url_cleanup);
    if (!url || curl_url_set(url.get(), CURLUPART_URL, text.c_str(), CURLU_DISALLOW_USER) != CURLUE_OK)
        throw std::invalid_argument("cannot parse target URL");
    const auto part = [&](CURLUPart kind, unsigned flags = 0) {
        char* raw = nullptr;
        const auto code = curl_url_get(url.get(), kind, &raw, flags);
        const std::unique_ptr<char, decltype(&curl_free)> owned(raw, curl_free);
        return code == CURLUE_OK ? std::string(owned.get()) : std::string{};
    };
    auto host = part(CURLUPART_HOST);
    for (auto& c : host) if (c >= 'A' && c <= 'Z') c = static_cast<char>(c + ('a' - 'A'));
    const auto zone = part(CURLUPART_ZONEID);
    if (!zone.empty()) host.insert(host.size() - 1, "%25" + zone);
    const auto query = part(CURLUPART_QUERY);
    return {part(CURLUPART_SCHEME) + "://" + host + ":" + part(CURLUPART_PORT, CURLU_DEFAULT_PORT),
            part(CURLUPART_PATH) + (text.find('?') == std::string::npos ? "" : "?" + query)};
}

struct Run {
    std::string path;
    Json document;
    Target target;
    bool clean = false;
    std::string state;
};

using FileIdentity = std::pair<std::uintmax_t, std::uintmax_t>;
Run read_run(const std::string& path, std::size_t& total_bytes, std::set<FileIdentity>& identities) {
    const auto fd = ::open(path.c_str(), O_RDONLY | O_CLOEXEC | O_NONBLOCK);
    if (fd < 0) throw std::system_error(errno, std::generic_category(), "cannot open input");
    struct Descriptor { int value; ~Descriptor() { ::close(value); } } owner{fd};
    struct stat info{};
    if (::fstat(fd, &info) != 0) throw std::system_error(errno, std::generic_category(), "cannot inspect input");
    require(S_ISREG(info.st_mode), "input must be a regular file");
    require(info.st_size >= 0 && static_cast<std::uintmax_t>(info.st_size) <= max_file_bytes, "input exceeds 1 MiB");
    require(identities.emplace(static_cast<std::uintmax_t>(info.st_dev), static_cast<std::uintmax_t>(info.st_ino)).second,
            "duplicate input file; each run artifact may be listed only once across both groups");
    std::string bytes;
    std::array<char, 4096> buffer{};
    while (true) {
        const auto n = ::read(fd, buffer.data(), buffer.size());
        if (n < 0 && errno == EINTR) continue;
        if (n < 0) throw std::system_error(errno, std::generic_category(), "cannot read input");
        if (n == 0) break;
        bytes.append(buffer.data(), static_cast<std::size_t>(n));
        require(bytes.size() <= max_file_bytes, "input exceeds 1 MiB");
    }
    total_bytes += bytes.size();
    require(total_bytes <= max_total_bytes, "combined input exceeds 32 MiB");
    std::vector<std::set<std::string>> object_keys;
    auto callback = [&](int depth, Json::parse_event_t event, Json& parsed) {
        require(depth <= 32, "JSON nesting exceeds 32 levels");
        if (event == Json::parse_event_t::object_start) object_keys.emplace_back();
        if (event == Json::parse_event_t::object_end) object_keys.pop_back();
        if (event == Json::parse_event_t::key)
            require(object_keys.back().insert(parsed.get<std::string>()).second, "duplicate JSON object key");
        return true;
    };
    Json document;
    try {
        document = Json::parse(bytes, callback);
    } catch (const Json::exception&) {
        // Malformed input may be a private configuration selected by mistake.
        // Library parser messages can quote its contents; keep diagnostics generic.
        throw std::invalid_argument("invalid result JSON (syntax, encoding or numeric range)");
    }
    return {path, std::move(document), {}, false, {}};
}

void latency(const Json& stats, std::uint64_t samples, double elapsed) {
    keys(stats, {"samples", "min", "mean", "p50", "p95", "p99", "max"}, "latency summary");
    require(integer(stats.at("samples"), "latency.samples", max_requests) == samples, "latency sample count disagrees with accounting");
    if (samples == 0) {
        for (auto name : latency_names) require(stats.at(name).is_null(), "empty latency distribution must contain null values");
        return;
    }
    for (auto name : latency_names) number(stats.at(name), name);
    const auto get = [&](const char* name) { return stats.at(name).get<double>(); };
    require(get("min") <= get("p50") && get("p50") <= get("p95") && get("p95") <= get("p99") &&
            get("p99") <= get("max") && get("min") <= get("mean") && get("mean") <= get("max"), "latency summary is not ordered");
    // Both extrema must occur in the recorded samples, which constrains the mean.
    const auto range_per_sample = (get("max") - get("min")) / static_cast<double>(samples);
    const auto lowest_mean = get("min") + range_per_sample;
    const auto highest_mean = get("max") - range_per_sample;
    require((get("mean") >= lowest_mean || close(get("mean"), lowest_mean)) &&
            (get("mean") <= highest_mean || close(get("mean"), highest_mean)), "latency mean is inconsistent with extrema and sample count");
    require(get("max") <= elapsed * 1000 + 1e-6 || close(get("max"), elapsed * 1000), "request latency exceeds the measured run window");
    if (samples == 1) for (auto name : latency_names) require(close(get(name), get("min")), "singleton latency values disagree");
}

void validate(Run& run) {
    auto& d = run.document;
    keys(d, {"schema_version", "engine", "started_at_utc", "workload", "counts", "http_status_counts",
        "transport_error_counts", "elapsed_seconds", "completed_throughput_rps", "successful_throughput_rps",
        "peak_in_flight", "interrupted", "engine_error", "success_latency_ms", "completed_latency_ms"}, "native result");
    const auto schema = integer(d.at("schema_version"), "schema_version", 3);
    require(schema >= 1 && schema <= 3, "only native schema versions 1, 2 and 3 are supported");
    const auto& engine = d.at("engine");
    keys(engine, {"version", "source_revision", "compiler", "build_type", "build_flags", "sanitizers", "platform", "libcurl"}, "native engine metadata");
    require(engine.at("version") == (schema == 1 ? "0.1.0" : schema == 2 ? "0.2.0" : "0.3.0"), "unsupported native engine/schema measurement contract");
    for (auto it = engine.begin(); it != engine.end(); ++it)
        string(it.value(), it.key(), 8192, it.key() == "source_revision" || it.key() == "build_flags");
    require(engine.at("sanitizers") == "ON" || engine.at("sanitizers") == "OFF", "unknown sanitizer setting");
    timestamp(d.at("started_at_utc"));
    auto& w = d.at("workload");
    auto& c = d.at("counts");
    if (schema == 1) {
        // Validate the original contract before adding explicit no-TLS defaults.
        keys(w, {"url", "method", "http_version", "model", "redirects", "proxy", "connection_reuse", "warmup_requests",
            "application_retries", "success_policy", "requests", "concurrency", "timeout_ms", "max_body_bytes"}, "legacy workload");
        keys(c, {"requested", "started", "completed", "successes", "http_errors", "connection_errors", "network_errors",
            "timeouts", "response_limit_errors", "cancelled", "not_started"}, "legacy counts");
        w["tls"] = nullptr;
        c["tls_errors"] = std::uint64_t{0};
    }
    if (schema < 3) {
        keys(w, {"url", "method", "http_version", "model", "redirects", "proxy", "connection_reuse", "warmup_requests",
            "application_retries", "success_policy", "requests", "concurrency", "timeout_ms", "max_body_bytes", "tls"}, "legacy workload");
        w["headers"] = Json{{"names", Json::array()}, {"values", "redacted"}};
    }
    keys(w, {"url", "method", "http_version", "model", "redirects", "proxy", "connection_reuse", "warmup_requests",
        "application_retries", "success_policy", "requests", "concurrency", "timeout_ms", "max_body_bytes", "tls", "headers"}, "workload");
    const auto& headers = w.at("headers");
    keys(headers, {"names", "values"}, "header metadata");
    require(headers.at("values") == "redacted", "unsupported header-value export policy");
    const auto& names = headers.at("names");
    require(names.is_array() && names.size() <= 32, "invalid header name list");
    std::string previous;
    for (const auto& item : names) {
        const auto name = string(item, "header name", 128);
        require(is_normalized_header_name(name) && previous < name, "header names must be allowed, lowercase, unique and sorted");
        previous = name;
    }
    require(w.at("method") == "GET" && w.at("http_version") == "1.1" && w.at("model") == "closed_loop" &&
        w.at("redirects").is_boolean() && w.at("redirects") == false && w.at("proxy").is_boolean() && w.at("proxy") == false &&
        w.at("connection_reuse").is_boolean() && w.at("connection_reuse") == true && w.at("success_policy") == "complete_2xx",
        "unsupported execution or success semantics");
    require(integer(w.at("warmup_requests"), "warmup_requests", max_requests) == 0 &&
        integer(w.at("application_retries"), "application_retries", max_requests) == 0, "unsupported warm-up/retry semantics");
    Config config;
    config.url = string(w.at("url"), "workload.url", 8192);
    config.requests = integer(w.at("requests"), "workload.requests", max_requests);
    config.concurrency = static_cast<std::size_t>(integer(w.at("concurrency"), "concurrency", max_concurrency));
    config.timeout_ms = static_cast<long>(integer(w.at("timeout_ms"), "timeout_ms", 3'600'000));
    config.max_body_bytes = static_cast<std::size_t>(integer(w.at("max_body_bytes"), "max_body_bytes", 1'073'741'824));
    validate_config(config);
    const bool https = is_https_url(config.url);
    require(schema != 1 || !https, "legacy SPE results cannot describe HTTPS measurements");
    if (https) {
        const auto& tls = w.at("tls");
        keys(tls, {"verify_peer", "verify_host", "minimum_version", "ca_source", "ca_bundle_sha256"}, "TLS policy");
        require(tls.at("verify_peer").is_boolean() && tls.at("verify_peer") == true &&
            tls.at("verify_host").is_boolean() && tls.at("verify_host") == true && tls.at("minimum_version") == "1.2",
            "unsupported or unverified TLS policy");
        if (tls.at("ca_source") == "libcurl_default") require(tls.at("ca_bundle_sha256").is_null(), "default trust has no custom bundle fingerprint");
        else {
            require(tls.at("ca_source") == "custom_bundle", "unknown CA source");
            const auto hash = string(tls.at("ca_bundle_sha256"), "CA bundle SHA-256", 64);
            require(hash.size() == 64 && hash.find_first_not_of("0123456789abcdef") == std::string::npos, "invalid CA bundle SHA-256");
        }
    } else require(w.at("tls").is_null(), "HTTP workloads cannot carry a TLS policy");
    run.target = target(config.url);
    keys(c, {"requested", "started", "completed", "successes", "http_errors", "connection_errors", "network_errors",
        "tls_errors", "timeouts", "response_limit_errors", "cancelled", "not_started"}, "counts");
    for (auto name : count_names) integer(c.at(name), name, config.requests);
    const auto count = [&](const char* name) { return c.at(name).get<std::uint64_t>(); };
    const auto failures = count("http_errors") + count("connection_errors") + count("network_errors") + count("tls_errors") + count("timeouts") + count("response_limit_errors");
    require(https || count("tls_errors") == 0, "HTTP workloads cannot have TLS outcomes");
    require(count("requested") == config.requests && count("requested") == count("started") + count("not_started") &&
        count("started") == count("completed") + count("cancelled") && count("completed") == count("successes") + failures,
        "request accounting invariants failed");
    const auto elapsed = number(d.at("elapsed_seconds"), "elapsed_seconds");
    require(elapsed <= 1e12 && (elapsed > 0 || count("started") == 0), "invalid measured elapsed time");
    for (auto pair : {std::pair{"successful_throughput_rps", "successes"}, std::pair{"completed_throughput_rps", "completed"}}) {
        const auto expected = elapsed > 0 ? static_cast<double>(count(pair.second)) / elapsed : 0;
        const auto recorded = number(d.at(pair.first), pair.first);
        require(expected == 0 ? recorded == 0 : close(recorded, expected), "throughput disagrees with counts / elapsed time");
    }
    const auto peak = integer(d.at("peak_in_flight"), "peak_in_flight", config.concurrency);
    require(peak <= count("started") && (count("started") == 0 ? peak == 0 : peak > 0), "invalid peak concurrency");
    require(d.at("interrupted").is_boolean(), "interrupted must be boolean");
    const auto& error = d.at("engine_error");
    if (!error.is_null()) string(error, "engine_error", 65536);
    const bool incomplete = count("cancelled") != 0 || count("not_started") != 0;
    require(!incomplete || d.at("interrupted") == true || !error.is_null(), "incomplete run has no interruption or engine error");
    latency(d.at("success_latency_ms"), count("successes"), elapsed);
    latency(d.at("completed_latency_ms"), count("completed"), elapsed);
    if (count("completed")) {
        const auto sum = d.at("completed_latency_ms").at("mean").get<double>() * static_cast<double>(count("completed"));
        const auto capacity = static_cast<double>(peak) * elapsed * 1000;
        require(sum <= capacity + 0.001 || close(sum, capacity), "summed request duration exceeds concurrency window");
    }
    if (count("successes")) {
        const auto& good = d.at("success_latency_ms"); const auto& all = d.at("completed_latency_ms");
        const auto good_sum = good.at("mean").get<double>() * static_cast<double>(count("successes"));
        const auto all_sum = all.at("mean").get<double>() * static_cast<double>(count("completed"));
        require(good.at("min").get<double>() >= all.at("min").get<double>() && good.at("max").get<double>() <= all.at("max").get<double>() &&
            (good_sum <= all_sum + 1e-6 || close(good_sum, all_sum)), "successful latencies are not a subset of completed observations");
        if (count("successes") == count("completed"))
            for (auto name : latency_names) require(close(good.at(name).get<double>(), all.at(name).get<double>()), "all-success latency summaries disagree");
    }
    const auto& statuses = d.at("http_status_counts");
    require(statuses.is_object() && statuses.size() <= 500, "invalid HTTP status map");
    std::uint64_t good_statuses = 0, bad_statuses = 0;
    for (auto it = statuses.begin(); it != statuses.end(); ++it) {
        unsigned status = 0;
        const auto [end, err] = std::from_chars(it.key().data(), it.key().data() + it.key().size(), status);
        require(err == std::errc{} && end == it.key().data() + it.key().size() && status >= 100 && status < 600 &&
                it.key() == std::to_string(status), "invalid HTTP status key");
        const auto n = integer(it.value(), "HTTP status count", config.requests);
        require(n > 0, "HTTP status map contains an empty count");
        (status >= 200 && status < 300 ? good_statuses : bad_statuses) += n;
    }
    require(good_statuses == count("successes") && bad_statuses == count("http_errors"), "HTTP statuses disagree with outcomes");
    const auto& transport = d.at("transport_error_counts");
    require(transport.is_object() && transport.size() <= 1024, "invalid transport error map");
    std::uint64_t transport_total = 0, tls_total = 0;
    for (auto it = transport.begin(); it != transport.end(); ++it) {
        int code = 0;
        const auto [end, err] = std::from_chars(it.key().data(), it.key().data() + it.key().size(), code);
        require(err == std::errc{} && end == it.key().data() + it.key().size() && code > 0 && it.key() == std::to_string(code), "invalid transport error key");
        keys(it.value(), {"count", "description"}, "transport error");
        const auto n = integer(it.value().at("count"), "transport count", config.requests);
        require(n > 0, "transport map contains an empty count"); transport_total += n;
        if (is_tls_error(code)) tls_total += n;
        string(it.value().at("description"), "transport description", 8192);
    }
    require(transport_total <= failures - count("http_errors"), "transport error counts exceed failed transfers");
    require(tls_total == count("tls_errors"), "TLS error codes disagree with TLS outcomes");
    run.clean = !incomplete && d.at("interrupted") == false && error.is_null() && count("successes") == config.requests;
    run.state = run.clean ? "complete_success" : incomplete || d.at("interrupted") == true || !error.is_null() ? "incomplete_or_interrupted" : "completed_with_failures";
}

double median(std::vector<double> values) {
    std::sort(values.begin(), values.end());
    const auto i = values.size() / 2;
    return values.size() % 2 ? values[i] : values[i - 1] / 2 + values[i] / 2;
}

Json spread(const std::vector<double>& values) {
    if (values.empty()) return nullptr;
    const auto [lo, hi] = std::minmax_element(values.begin(), values.end());
    Json sd = nullptr;
    if (values.size() > 1) {
        double shifted_mean = 0, squares = 0;
        if (*hi > 0) {
            const auto center = values.front() / *hi;
            for (auto v : values) shifted_mean += (v / *hi - center) / static_cast<double>(values.size());
            for (auto v : values) squares += std::pow((v / *hi - center) - shifted_mean, 2);
        }
        sd = *hi * std::sqrt(squares / static_cast<double>(values.size() - 1));
    }
    return Json{{"runs", values.size()}, {"minimum", *lo}, {"maximum", *hi}, {"median", median(values)}, {"sample_stddev", sd}};
}

Json group(const std::vector<Run>& runs) {
    Json counts = Json::object(); for (auto name : count_names) counts[name] = std::uint64_t{0};
    Json inputs = Json::array();
    double elapsed = 0, latency_sum = 0;
    bool clean = true;
    std::vector<double> throughput, means, p50, p95, p99;
    for (const auto& run : runs) {
        const auto& d = run.document; clean = clean && run.clean;
        for (auto name : count_names) counts[name] = counts.at(name).get<std::uint64_t>() + d.at("counts").at(name).get<std::uint64_t>();
        elapsed += d.at("elapsed_seconds").get<double>();
        throughput.push_back(d.at("successful_throughput_rps").get<double>());
        const auto samples = d.at("success_latency_ms").at("samples").get<std::uint64_t>();
        if (samples) {
            const auto& s = d.at("success_latency_ms");
            means.push_back(s.at("mean").get<double>()); latency_sum += means.back() * static_cast<double>(samples);
            p50.push_back(s.at("p50").get<double>()); p95.push_back(s.at("p95").get<double>()); p99.push_back(s.at("p99").get<double>());
        }
        inputs.push_back(Json{{"path", run.path}, {"started_at_utc", d.at("started_at_utc")}, {"state", run.state},
            {"url", d.at("workload").at("url")}, {"counts", d.at("counts")}, {"engine_error", d.at("engine_error")},
            {"transport_error_counts", d.at("transport_error_counts")},
            {"elapsed_seconds", d.at("elapsed_seconds")}, {"success_latency_ms", d.at("success_latency_ms")}});
    }
    const auto successes = counts.at("successes").get<std::uint64_t>();
    const auto completed = counts.at("completed").get<std::uint64_t>();
    Json metrics{{"successful_throughput_rps", elapsed > 0 ? static_cast<double>(successes) / elapsed : 0},
        {"mean_request_latency_ms", successes ? Json(latency_sum / static_cast<double>(successes)) : Json(nullptr)},
        {"median_run_p50_ms", p50.empty() ? Json(nullptr) : Json(median(p50))},
        {"median_run_p95_ms", p95.empty() ? Json(nullptr) : Json(median(p95))},
        {"median_run_p99_ms", p99.empty() ? Json(nullptr) : Json(median(p99))}};
    return Json{{"runs", runs.size()}, {"eligible_for_performance_deltas", clean}, {"counts", counts},
        {"completed_errors", completed - successes},
        {"completed_error_rate_percent", completed ? Json(100 * static_cast<double>(completed - successes) / static_cast<double>(completed)) : Json(nullptr)},
        {"elapsed_seconds", elapsed}, {"metrics", metrics},
        {"run_variability", {{"successful_throughput_rps", spread(throughput)}, {"mean_request_latency_ms", spread(means)},
            {"p50_ms", spread(p50)}, {"p95_ms", spread(p95)}, {"p99_ms", spread(p99)}}}, {"inputs", inputs}};
}

std::string display(const Json& value, unsigned precision = 3) {
    if (value.is_null()) return "n/a";
    const auto n = value.get<double>();
    std::ostringstream text; text.imbue(std::locale::classic());
    if (n != 0 && (std::abs(n) < std::pow(10.0, -static_cast<int>(precision)) || std::abs(n) >= 1e9)) text << std::scientific;
    else text << std::fixed;
    text << std::setprecision(static_cast<int>(precision)) << n;
    return text.str();
}

ComparisonReport render(Json report, ComparisonStatus status) {
    std::ostringstream text; text.imbue(std::locale::classic());
    text << "PERFORMANCE COMPARISON\n";
    if (status == ComparisonStatus::rejected) {
        text << "Comparison rejected: " << Json(report.at("error")).dump(-1, ' ', true, Json::error_handler_t::replace) << '\n';
    } else {
        const auto& a = report.at("baseline"); const auto& b = report.at("candidate");
        text << "Input checks: PASS (schema, accounting, measurements, workload and engine build).\n";
        text << "Runs: baseline=" << a.at("runs") << " candidate=" << b.at("runs") << '\n';
        text << std::left << std::setw(32) << "Metric" << std::right << std::setw(16) << "Baseline" << std::setw(16) << "Candidate" << std::setw(14) << "Change" << '\n';
        for (const auto& [name, label] : {std::pair{"successful_throughput_rps", "Successful throughput (req/s)"},
                 std::pair{"mean_request_latency_ms", "Mean request latency (ms)"}, std::pair{"median_run_p50_ms", "Median run p50 (ms)"},
                 std::pair{"median_run_p95_ms", "Median run p95 (ms)"}, std::pair{"median_run_p99_ms", "Median run p99 (ms)"}}) {
            const auto& metric = report.at("metrics").at(name);
            const auto delta = metric.at("change_percent");
            text << std::left << std::setw(32) << label << std::right << std::setw(16) << display(metric.at("baseline"))
                 << std::setw(16) << display(metric.at("candidate")) << std::setw(14) << (delta.is_null() ? "n/a" :
                    (delta.get<double>() >= 0 ? "+" : "") + display(delta, 2) + "%") << '\n';
        }
        text << "Completed errors: baseline=" << a.at("completed_errors") << " candidate=" << b.at("completed_errors") << '\n';
        for (const auto& [label, g] : {std::pair{"baseline", a}, std::pair{"candidate", b}}) {
            const auto& c = g.at("counts");
            text << label << " requests (totals): requested=" << c.at("requested") << " completed=" << c.at("completed")
                 << " successful=" << c.at("successes") << " completed_error_rate=" << display(g.at("completed_error_rate_percent"), 2)
                 << (g.at("completed_error_rate_percent").is_null() ? "" : "%") << '\n';
            text << label << " outcomes (totals across runs): HTTP=" << c.at("http_errors") << " connection=" << c.at("connection_errors")
                << " network=" << c.at("network_errors") << " TLS=" << c.at("tls_errors") << " timeout=" << c.at("timeouts") << " response_limit=" << c.at("response_limit_errors")
                << " cancelled=" << c.at("cancelled") << " not_started=" << c.at("not_started") << '\n';
            const auto& variability = g.at("run_variability");
            for (auto metric : {"successful_throughput_rps", "mean_request_latency_ms"}) {
                const auto& s = variability.at(metric);
                if (!s.is_null() && s.at("runs") > 1) text << label << " run " << metric << " range=" << display(s.at("minimum")) << ".." << display(s.at("maximum"))
                    << " sample_stddev=" << display(s.at("sample_stddev")) << '\n';
            }
            for (const auto& input : g.at("inputs")) if (input.at("state") != "complete_success")
                text << label << " " << input.at("path").dump(-1, ' ', true, Json::error_handler_t::replace) << ": " << input.at("state").get<std::string>() << '\n';
        }
        if (status == ComparisonStatus::insufficient_evidence)
            text << "Insufficient evidence: failed, incomplete, or interrupted runs; performance deltas suppressed. Absolute values are diagnostics.\n";
        text << "Direction: higher successful throughput; lower successful-request latency (under comparable conditions).\n"
             << "Throughput = total successes / total measured seconds; mean latency is weighted by successful samples.\n"
             << "p50/p95/p99 summaries are medians of per-run percentiles, not pooled request percentiles.\n";
        text << "Evidence: " << report.at("evidence").at("level").get<std::string>() << ". Individual requests are not replicates; independence of runs is not established.\n";
        if (report.at("origin_change_acknowledged") == true)
            text << "Different origins explicitly allowed; network/environment equivalence remains unverified.\n";
        if (report.at("redacted_headers_acknowledged") == true)
            text << "Header values are redacted. Equality and authorization context are unverified; acknowledged explicitly.\n";
    }
    text << "Statistical verdict: NOT ASSESSED.\n";
    return {status, text.str(), report.dump(2, ' ', true, Json::error_handler_t::replace) + '\n'};
}
}

CompareOptions parse_compare_cli(std::span<const std::string_view> arguments) {
    CompareOptions options;
    std::vector<std::string> positional;
    bool named = false, output_seen = false;
    for (std::size_t i = 0; i < arguments.size(); ++i) {
        const auto arg = arguments[i];
        if (arg == "--help" || arg == "-h") { options.help = true; continue; }
        if (arg == "--allow-origin-change") {
            require(!options.allow_origin_change, "duplicate --allow-origin-change"); options.allow_origin_change = true; continue;
        }
        if (arg == "--allow-redacted-headers") {
            require(!options.allow_redacted_headers, "duplicate --allow-redacted-headers"); options.allow_redacted_headers = true; continue;
        }
        if (arg == "--baseline" || arg == "--candidate" || arg == "--json") {
            require(++i < arguments.size(), "missing value for " + std::string(arg));
            const auto value = arguments[i];
            require(!value.empty() && (!value.starts_with('-') || (arg == "--json" && value == "-")), "invalid value for " + std::string(arg));
            if (arg == "--json") { require(!output_seen, "duplicate --json"); output_seen = true; options.json_path = value; }
            else { named = true; (arg == "--baseline" ? options.baseline : options.candidate).emplace_back(value); }
        } else {
            require(!arg.starts_with('-'), "unknown comparison option: " + std::string(arg)); positional.emplace_back(arg);
        }
    }
    if (options.help) return options;
    require(!named || positional.empty(), "use either two positional files or repeated --baseline/--candidate options");
    if (!named) {
        require(positional.size() == 2, "compare requires one baseline and one candidate file, or named run groups");
        options.baseline.push_back(positional[0]); options.candidate.push_back(positional[1]);
    }
    require(!options.baseline.empty() && !options.candidate.empty(), "both baseline and candidate groups are required");
    require(options.baseline.size() <= max_group_runs && options.candidate.size() <= max_group_runs, "at most 100 files per group are supported");
    return options;
}

std::string_view compare_usage() {
    return "Usage: spe compare BASELINE.json CANDIDATE.json [options]\n"
           "       spe compare --baseline FILE [--baseline FILE ...] --candidate FILE [--candidate FILE ...] [options]\n"
           "  --json PATH|-          New comparison JSON file, or JSON stdout with summary on stderr\n"
           "  --allow-origin-change  Acknowledge different hosts/ports; encoded path/query must still match\n"
           "  --allow-redacted-headers  Acknowledge unverifiable header values; recorded names must still match\n"
           "  --help, -h             Show comparison help\n"
           "Native SPE 0.1.0/schema 1, 0.2.0/schema 2 and 0.3.0/schema 3 inputs; contracts cannot be mixed. No traffic or statistical verdicts.\n"
           "Limits: 100 files/group, 1 MiB/file, 32 MiB total, 32 JSON nesting levels.\n"
           "Exit: 0 descriptive comparison; 3 insufficient complete-success evidence; 2 rejected input; 1 output/system error.\n";
}

int comparison_exit_code(ComparisonStatus status) {
    return status == ComparisonStatus::descriptive ? 0 : status == ComparisonStatus::insufficient_evidence ? 3 : 2;
}

ComparisonReport compare_saved_results(const CompareOptions& options) {
    Json report{{"schema_version", 3}, {"kind", "spe_comparison"}, {"statistical_verdict", "not_assessed"},
        {"status", "rejected"}, {"checks", Json::array()}, {"metrics", Json::object()}};
    std::string context = "inputs";
    try {
        require(!options.baseline.empty() && !options.candidate.empty() && options.baseline.size() <= max_group_runs &&
                options.candidate.size() <= max_group_runs, "each group needs 1..100 run files");
        // Reject oversized collections before parsing any file. Actual reads still enforce
        // the limits independently because files may change after this metadata check.
        std::size_t expected_bytes = 0;
        for (auto paths : {&options.baseline, &options.candidate}) for (const auto& path : *paths) {
            context = path;
            struct stat info{};
            if (::stat(path.c_str(), &info) != 0) throw std::system_error(errno, std::generic_category(), "cannot inspect input");
            require(S_ISREG(info.st_mode), "input must be a regular file");
            require(info.st_size >= 0 && static_cast<std::uintmax_t>(info.st_size) <= max_file_bytes, "input exceeds 1 MiB");
            expected_bytes += static_cast<std::size_t>(info.st_size);
            require(expected_bytes <= max_total_bytes, "combined input exceeds 32 MiB");
        }
        std::set<FileIdentity> identities;
        std::size_t bytes = 0;
        std::vector<Run> baseline, candidate;
        for (auto pair : {std::pair{&options.baseline, &baseline}, std::pair{&options.candidate, &candidate}}) {
            for (const auto& path : *pair.first) {
                context = path;
                auto run = read_run(path, bytes, identities); validate(run); pair.second->push_back(std::move(run));
                report["checks"].push_back(Json{{"input", path}, {"schema_accounting_measurements", "pass"}});
            }
        }
        context = "compatibility";
        const auto& reference = baseline.front();
        auto expected = reference.document.at("workload"); expected.erase("url");
        bool origins_differ = false;
        for (auto runs : {&baseline, &candidate}) for (const auto& run : *runs) {
            require(run.document.at("schema_version") == reference.document.at("schema_version"), "result measurement contracts differ");
            require(run.document.at("engine") == reference.document.at("engine"), "recorded engine builds differ; use one engine build for all runs");
            auto settings = run.document.at("workload"); settings.erase("url");
            require(settings == expected, "workload settings differ (request budget, concurrency, timeout, body limit or execution policy)");
            require(run.target.endpoint == reference.target.endpoint, "encoded endpoint path/query differ");
            if (run.target.origin != reference.target.origin) origins_differ = true;
        }
        require(!origins_differ || options.allow_origin_change, "target origins differ; --allow-origin-change explicitly acknowledges this difference");
        const bool redacted_headers = !expected.at("headers").at("names").empty();
        require(!redacted_headers || options.allow_redacted_headers,
            "header values are redacted and cannot be checked; --allow-redacted-headers explicitly acknowledges this limitation");
        report["checks"].push_back(Json{{"compatible_workload_engine_and_measurement_contract", "pass"}});
        report["engine"] = reference.document.at("engine"); report["workload"] = expected;
        report["input_result_schema_version"] = reference.document.at("schema_version");
        report["endpoint"] = reference.target.endpoint; report["origin_change_acknowledged"] = origins_differ;
        report["redacted_headers_acknowledged"] = redacted_headers;
        report["baseline"] = group(baseline); report["candidate"] = group(candidate);
        const bool eligible = report.at("baseline").at("eligible_for_performance_deltas") == true &&
                              report.at("candidate").at("eligible_for_performance_deltas") == true;
        for (const auto& [name, a] : report.at("baseline").at("metrics").items()) {
            const auto b = report.at("candidate").at("metrics").at(name);
            Json delta = nullptr, percent = nullptr;
            std::string reason = "none";
            if (!eligible) reason = "failed_or_incomplete_runs";
            else if (a.is_null() || b.is_null()) reason = "missing_samples";
            else {
                const auto av = a.get<double>(), bv = b.get<double>(); delta = bv - av;
                if (av == 0) reason = "baseline_zero";
                else {
                    const auto change = ((bv - av) / av) * 100;
                    if (std::isfinite(change)) percent = change;
                    else reason = "percentage_outside_numeric_range";
                }
            }
            report["metrics"][name] = Json{{"baseline", a}, {"candidate", b}, {"absolute_change", delta},
                {"change_percent", percent}, {"change_note", reason}, {"preferred_direction", name == "successful_throughput_rps" ? "higher" : "lower"}};
        }
        report["evidence"] = Json{{"level", baseline.size() == 1 && candidate.size() == 1 ? "single run per version; repeatability unavailable" : "multiple input runs; run-to-run variation is descriptive"},
            {"independence", "experimental assumption, not established by file count"}, {"replication_unit", "input run, not individual request"}};
        report["notes"] = Json::array({"Successful throughput and request-weighted mean latency use total measured successes and seconds.",
            "Percentile summaries are medians of per-run percentiles, not pooled request percentiles.",
            "Matching recorded settings does not prove identical hardware, background load, target state or artifact authenticity.",
            "Native request duration includes connection work, TLS negotiation for HTTPS, and completion-observation overhead; no significance test is performed.",
            "Default trust-store contents are not fingerprinted; custom CA bundles are compared by their recorded SHA-256.",
            "Configured header values are never exported or hashed; matching names do not establish matching values or authorization context."});
        report["status"] = eligible ? "descriptive_comparison" : "insufficient_evidence";
        return render(std::move(report), eligible ? ComparisonStatus::descriptive : ComparisonStatus::insufficient_evidence);
    } catch (const std::exception& error) {
        report["error"] = context + ": " + error.what();
        report["metrics"] = Json::object(); report.erase("baseline"); report.erase("candidate");
        report["checks"].push_back(Json{{"failed", context}});
        return render(std::move(report), ComparisonStatus::rejected);
    }
}
}
