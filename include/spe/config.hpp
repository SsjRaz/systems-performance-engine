#pragma once

#include <cstddef>
#include <cstdint>
#include <optional>
#include <span>
#include <string>
#include <string_view>

namespace spe {
inline constexpr std::uint64_t max_requests = 1'000'000;
inline constexpr std::size_t max_concurrency = 1024;

struct FixedRate {
    std::uint64_t per_second;
    std::uint64_t duration_ms;
    std::uint64_t planned_arrivals() const;
};

struct Config {
    std::string url;
    std::uint64_t requests = 1000;
    std::size_t concurrency = 10;
    long timeout_ms = 1000;
    std::size_t max_body_bytes = 16 * 1024 * 1024;
    std::string json_path;
    std::string ca_file;
    std::string headers_file;
    std::optional<FixedRate> fixed_rate;

    std::uint64_t request_budget() const {
        return fixed_rate ? fixed_rate->planned_arrivals() : requests;
    }
};

struct CliOptions {
    Config config;
    bool help = false;
    bool version = false;
};

// Arguments exclude argv[0]. Invalid input throws std::invalid_argument.
CliOptions parse_cli(std::span<const std::string_view> arguments);
void validate_config(const Config& config);
bool is_https_url(std::string_view url);
std::string_view usage();
}
