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

void validate_config(const Config& config) {
    if (config.requests == 0 || config.requests > max_requests) {
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
    for (std::size_t i = 0; i < arguments.size(); ++i) {
        const auto arg = arguments[i];
        if (arg == "--help" || arg == "-h") {
            options.help = true;
        } else if (arg == "--version") {
            options.version = true;
        } else if (arg.starts_with('-')) {
            if (arg != "--requests" && arg != "--concurrency" && arg != "--timeout-ms" &&
                arg != "--max-body-bytes" && arg != "--json" && arg != "--ca-file" && arg != "--headers-file") {
                throw std::invalid_argument("unknown option: " + std::string(arg));
            }
            if (!seen.insert(arg).second) {
                throw std::invalid_argument("duplicate option: " + std::string(arg));
            }
            if (++i == arguments.size()) {
                throw std::invalid_argument("missing value for " + std::string(arg));
            }
            const auto value = arguments[i];
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
    if (!options.help && !options.version) validate_config(options.config);
    return options;
}

std::string_view usage() {
    return "Usage: spe <http[s]://host[:port]/path> [options]\n"
           "  --requests N        Total logical requests, 1..1000000 (default 1000)\n"
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
