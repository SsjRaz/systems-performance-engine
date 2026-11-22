#pragma once

#include "spe/workload.hpp"
#include <nlohmann/json.hpp>
#include <string>

namespace spe {
enum class ConfigFile { private_headers, workload, verification, experiment };
EndpointMix parse_endpoints(const nlohmann::json& endpoints);
nlohmann::json parse_bounded_json(std::string_view bytes, std::string_view label, int max_depth, std::size_t max_events);
nlohmann::json read_config_json(const std::string& path, ConfigFile kind);
}
