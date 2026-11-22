#pragma once

#include <span>
#include <string>
#include <string_view>
#include <vector>

namespace spe {
struct CompareOptions {
    std::vector<std::string> baseline;
    std::vector<std::string> candidate;
    std::string json_path;
    bool allow_origin_change = false;
    bool allow_redacted_headers = false;
    bool help = false;
    bool allow_redacted_body = false;
};

enum class ComparisonStatus { descriptive, insufficient_evidence, rejected };

// Rendering is encapsulated here so the CLI does not depend on a JSON library.
struct ComparisonReport {
    ComparisonStatus status = ComparisonStatus::rejected;
    std::string summary;
    std::string json;
};

CompareOptions parse_compare_cli(std::span<const std::string_view> arguments);
std::string_view compare_usage();
// Reads bounded local files only; does not execute workloads or send HTTP traffic.
ComparisonReport compare_saved_results(const CompareOptions& options, bool for_paired_analysis = false);
int comparison_exit_code(ComparisonStatus status);
}
