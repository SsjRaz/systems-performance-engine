#pragma once

#include "spe/result.hpp"
#include <curl/curl.h>
#include <functional>
#include <vector>

namespace spe {
Verification preflight(CURL* configured, const Config& config, const std::vector<std::string>& mixed_urls,
                       const std::function<bool()>& stop_requested);
}
