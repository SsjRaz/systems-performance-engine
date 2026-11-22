#pragma once

#include "spe/compare.hpp"

namespace spe {
struct AnalyzeOptions {
    std::string manifest;
    std::string json_path;
    bool help = false;
};
AnalyzeOptions parse_analyze_cli(std::span<const std::string_view> arguments);
std::string_view analyze_usage();
ComparisonReport analyze_experiment(const AnalyzeOptions& options);
}
