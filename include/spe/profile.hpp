#pragma once

#include "spe/analyze.hpp"

namespace spe {
ComparisonReport compare_cpu_profiles(const AnalyzeOptions& options);
std::string_view profile_usage();
}
