#pragma once

#include "spe/workload.hpp"
#include <nlohmann/json.hpp>
#include <string>

namespace spe {
enum class ConfigFile { private_headers, workload };
EndpointMix parse_endpoints(const nlohmann::json& endpoints);
nlohmann::json read_config_json(const std::string& path, ConfigFile kind);
}
