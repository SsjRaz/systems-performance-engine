#include "spe/config.hpp"

#include <curl/curl.h>

#include <algorithm>
#include <charconv>
#include <limits>
#include <memory>
#include <set>
#include <stdexcept>

namespace spe {
namespace {
std::uint64_t number(std::string_view value, std::string_view option) {
    if (value.empty()) {
        throw std::invalid_argument(std::string(option) + " requires a positive decimal integer");
    }
    std::uint64_t result = 0;
    const auto [end, error] = std::from_chars(value.data(), value.data() + value.size(), result);
    if (error != std::errc{} || end != value.data() + value.size()) {
        throw std::invalid_argument(std::string(option) + " requires a positive decimal integer");
    }
    return result;
}

std::string url_part(CURLU* url, CURLUPart part) {
    char* raw = nullptr;
    const auto code = curl_url_get(url, part, &raw, 0);
    const std::unique_ptr<char, decltype(&curl_free)> value(raw, curl_free);
    if (code != CURLUE_OK) {
        return {};
    }
    return value.get();
}
}

bool is_https_url(std::string_view url) {
    constexpr std::string_view prefix = "https:";
    return url.size() >= prefix.size() && std::equal(prefix.begin(), prefix.end(), url.begin(), [](char a, char b) {
        if (b >= 'A' && b <= 'Z') b = static_cast<char>(b + ('a' - 'A'));
        return a == b;
    });
}

std::uint64_t FixedRate::planned_arrivals() const {
    if (per_second == 0 || per_second > max_requests)
        throw std::invalid_argument("--rate must be in [1, 1000000] requests per second");
    if (duration_ms == 0 || duration_ms > 3'600'000)
        throw std::invalid_argument("--duration-ms must be in [1, 3600000]");
    // First arrival at t=0, last strictly before the end of the window.
    const auto count = (per_second * duration_ms + 999) / 1000;
    if (count > max_requests)
        throw std::invalid_argument("fixed-rate schedule exceeds 1000000 arrivals");
    return count;
}

bool method_accepts_body(std::string_view method) {
    return method == "POST" || method == "PUT" || method == "PATCH";
}

bool method_reuses_connections(std::string_view method) {
    return method == "GET" || method == "HEAD";
}

void validate_config(const Config& config) {
    const auto budget = config.request_budget();
    if (!method_reuses_connections(config.method) && !method_accepts_body(config.method) && config.method != "DELETE")
        throw std::invalid_argument("method must be GET, HEAD, POST, PUT, PATCH or DELETE");
    if (!method_reuses_connections(config.method) && budget > 1 && !config.repeat_writes && !config.fixed_rate)
        throw std::invalid_argument("repeated closed-loop writes require an explicit request budget");
    if (!config.body_file.empty() && !method_accepts_body(config.method))
        throw std::invalid_argument("--body-file requires POST, PUT or PATCH");
    if (config.body_file.size() > 4096 || config.body_file.find('\0') != std::string::npos)
        throw std::invalid_argument("invalid request body file path");
    if (budget == 0 || budget > max_requests) {
        throw std::invalid_argument("--requests must be in [1, 1000000]");
    }
    if (config.concurrency == 0 || config.concurrency > max_concurrency) {
        throw std::invalid_argument("--concurrency must be in [1, 1024]");
    }
    if (config.timeout_ms < 1 || config.timeout_ms > 3'600'000) {
        throw std::invalid_argument("--timeout-ms must be in [1, 3600000]");
    }
    if (config.max_body_bytes == 0 || config.max_body_bytes > 1'073'741'824) {
        throw std::invalid_argument("--max-body-bytes must be in [1, 1073741824]");
    }
    if (config.url.empty() || config.url.size() > 8192 ||
        std::any_of(config.url.begin(), config.url.end(), [](unsigned char c) { return c <= 32 || c >= 127; })) {
        throw std::invalid_argument("target must be an ASCII HTTP URL of at most 8192 bytes; percent-encode other characters");
    }
    const std::unique_ptr<CURLU, decltype(&curl_url_cleanup)> url(curl_url(), curl_url_cleanup);
    if (!url) {
        throw std::runtime_error("cannot allocate URL parser");
    }
    const auto code = curl_url_set(url.get(), CURLUPART_URL, config.url.c_str(), CURLU_DISALLOW_USER);
    if (code != CURLUE_OK) {
        throw std::invalid_argument(std::string("invalid target URL: ") + curl_url_strerror(code));
    }
    const auto scheme = url_part(url.get(), CURLUPART_SCHEME);
    if ((scheme != "http" && scheme != "https") || url_part(url.get(), CURLUPART_HOST).empty()) {
        throw std::invalid_argument("target must use http:// or https:// with a host");
    }
    if (config.url.find('#') != std::string::npos || url_part(url.get(), CURLUPART_PORT) == "0") {
        throw std::invalid_argument("URL fragments and port zero are not supported");
    }
    if (config.mix) {
        if (config.method != "GET") throw std::invalid_argument("endpoint mixes require GET");
        if (!config.fixed_rate) throw std::invalid_argument("endpoint mixes require --rate and --duration-ms");
        std::string_view origin = config.url;
        if (origin.ends_with('/')) origin.remove_suffix(1);
        const auto scheme_end = origin.find("://");
        if (scheme_end == origin.npos || scheme_end + 3 >= origin.size() ||
            origin.substr(scheme_end + 3).find_first_of("/\\?#@") != origin.npos)
            throw std::invalid_argument("an endpoint mix requires an HTTP(S) origin with no path, query or credentials");
        for (const auto& endpoint : config.mix->endpoints())
            if (origin.size() + endpoint.path.size() > 8192) throw std::invalid_argument("endpoint URL exceeds 8192 bytes");
    }
    if (config.ca_file.size() > 4096 || config.ca_file.find('\0') != std::string::npos)
        throw std::invalid_argument("invalid CA certificate file path");
    if (!config.ca_file.empty() && scheme != "https")
        throw std::invalid_argument("--ca-file requires an HTTPS target");
    if (config.headers_file.size() > 4096 || config.headers_file.find('\0') != std::string::npos)
        throw std::invalid_argument("invalid headers file path");
}

CliOptions parse_cli(std::span<const std::string_view> arguments) {
    CliOptions options;
    std::set<std::string_view> seen;
    FixedRate rate{};
    std::string workload_file;
    for (std::size_t i = 0; i < arguments.size(); ++i) {
        const auto arg = arguments[i];
        if (arg == "--help" || arg == "-h") {
            options.help = true;
        } else if (arg == "--version") {
            options.version = true;
        } else if (arg.starts_with('-')) {
            if (arg != "--requests" && arg != "--rate" && arg != "--duration-ms" && arg != "--concurrency" && arg != "--timeout-ms" &&
                arg != "--max-body-bytes" && arg != "--json" && arg != "--ca-file" && arg != "--headers-file" &&
                arg != "--workload-file" && arg != "--method" && arg != "--body-file") {
                throw std::invalid_argument("unknown option: " + std::string(arg));
            }
            if (!seen.insert(arg).second) {
                throw std::invalid_argument("duplicate option: " + std::string(arg));
            }
            if (++i == arguments.size()) {
                throw std::invalid_argument("missing value for " + std::string(arg));
            }
            const auto value = arguments[i];
            if (arg == "--method") {
                options.config.method = value;
                continue;
            }
            if (arg == "--body-file") {
                if (value.empty() || value.starts_with('-')) throw std::invalid_argument("--body-file requires a file path");
                options.config.body_file = value;
                continue;
            }
            if (arg == "--workload-file") {
                if (value.empty() || value.starts_with('-') || value.size() > 4096 || value.find('\0') != value.npos)
                    throw std::invalid_argument("--workload-file requires a configuration file path");
                workload_file = value;
                continue;
            }
            if (arg == "--headers-file") {
                if (value.empty() || value.starts_with('-')) throw std::invalid_argument("--headers-file requires a configuration file path");
                options.config.headers_file = value;
                continue;
            }
            if (arg == "--ca-file") {
                if (value.empty() || value.starts_with('-')) throw std::invalid_argument("--ca-file requires a certificate file path");
                options.config.ca_file = value;
                continue;
            }
            if (arg == "--json") {
                if (value.empty() || (value.starts_with('-') && value != "-")) {
                    throw std::invalid_argument("--json requires a file path or - for stdout");
                }
                options.config.json_path = value;
                continue;
            }
            const auto n = number(value, arg);
            if (n > static_cast<std::uint64_t>(std::numeric_limits<long>::max()) ||
                n > std::numeric_limits<std::size_t>::max()) {
                throw std::invalid_argument("value too large for " + std::string(arg));
            }
            if (arg == "--requests") options.config.requests = n;
            if (arg == "--rate") rate.per_second = n;
            if (arg == "--duration-ms") rate.duration_ms = n;
            if (arg == "--concurrency") options.config.concurrency = static_cast<std::size_t>(n);
            if (arg == "--timeout-ms") options.config.timeout_ms = static_cast<long>(n);
            if (arg == "--max-body-bytes") options.config.max_body_bytes = static_cast<std::size_t>(n);
        } else {
            if (!options.config.url.empty()) {
                throw std::invalid_argument("exactly one target URL is required");
            }
            options.config.url = arg;
        }
    }
    if (seen.contains("--rate") || seen.contains("--duration-ms")) {
        if (!seen.contains("--rate") || !seen.contains("--duration-ms") || seen.contains("--requests"))
            throw std::invalid_argument("use --rate and --duration-ms together, without --requests");
        options.config.fixed_rate = rate;
    }
    if (!options.help && !options.version) {
        if (!method_reuses_connections(options.config.method)) {
            options.config.repeat_writes = seen.contains("--requests") || options.config.fixed_rate.has_value();
            if (!options.config.repeat_writes) options.config.requests = 1;
        }
        if (!workload_file.empty()) options.config.mix = load_workload_file(workload_file);
        validate_config(options.config);
    }
    return options;
}

std::string_view usage() {
    return "Usage: spe <http[s]://host[:port]/path> [options]\n"
           "  --method VERB       GET (default), HEAD, POST, PUT, PATCH or DELETE\n"
           "  --body-file PATH    Private binary payload for POST/PUT/PATCH, up to 64 KiB\n"
           "  --requests N        Total logical requests, 1..1000000 (default 1000 for GET/HEAD, 1 for writes)\n"
           "  --rate N            Fixed arrivals/second (positive integer); requires --duration-ms instead of --requests\n"
           "  --duration-ms N     Arrival window, 1..3600000 ms; at most 1000000 planned arrivals\n"
           "  --workload-file PATH  Two weighted endpoint paths (JSON); requires a target origin and fixed rate\n"
           "  --concurrency N     Maximum in-flight requests, 1..1024 (default 10)\n"
           "  --timeout-ms N      Total per-request timeout, 1..3600000 (default 1000)\n"
           "  --max-body-bytes N  Response body limit, 1..1073741824 (default 16777216)\n"
           "  --json PATH        Write JSON results; - writes JSON to stdout\n"
           "  --ca-file PATH     Custom HTTPS CA bundle (PEM, at most 1 MiB); verification stays enabled\n"
           "  --headers-file PATH  Private JSON array of name/value pairs (owner-only permissions)\n"
           "  --help, -h         Show help\n"
           "  --version          Show version\n"
           "Saved results: spe compare BASELINE.json CANDIDATE.json [options]\n"
           "  spe compare --help shows comparison options.\n";
}
}
