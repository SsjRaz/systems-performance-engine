#include "spe/workload.hpp"
#include "spe/config.hpp"
#include "config_json.hpp"

#include <numeric>
#include <stdexcept>
#include <utility>

namespace spe {
namespace {
bool valid_path(std::string_view path) {
    if (path.empty() || path.size() > 2048 || path.front() != '/' || path.find("//") != path.npos)
        return false;
    // Literal path segments only. Never resolve a URL reference against the origin.
    for (const char c : path) {
        if (!((c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') || (c >= '0' && c <= '9') ||
              std::string_view("/-._~").find(c) != path.npos)) return false;
    }
    return path.find("/./") == path.npos && path.find("/../") == path.npos &&
           !path.ends_with("/.") && !path.ends_with("/..");
}
}

EndpointMix::EndpointMix(std::array<WeightedEndpoint, size> endpoints) : endpoints_(std::move(endpoints)) {
    for (const auto& endpoint : endpoints_) {
        if (!valid_path(endpoint.path)) throw std::invalid_argument("invalid workload endpoint path");
        if (endpoint.weight == 0 || endpoint.weight > max_requests)
            throw std::invalid_argument("endpoint weights must be integers in [1, 1000000]");
    }
    if (endpoints_[0].path == endpoints_[1].path) throw std::invalid_argument("endpoint paths must be distinct");
    const auto divisor = std::gcd(endpoints_[0].weight, endpoints_[1].weight);
    for (auto& endpoint : endpoints_) endpoint.weight /= divisor;
    total_weight_ = endpoints_[0].weight + endpoints_[1].weight;
}

std::uint64_t EndpointMix::count_before(std::size_t endpoint, std::uint64_t arrivals) const {
    if (endpoint >= size || arrivals > max_requests) throw std::out_of_range("invalid endpoint prefix");
    // Round the first endpoint's cumulative quota to nearest, with ties going to it.
    const auto first = (arrivals * endpoints_[0].weight + total_weight_ / 2) / total_weight_;
    return endpoint == 0 ? first : arrivals - first;
}

std::size_t EndpointMix::endpoint_at(std::uint64_t ordinal) const {
    if (ordinal >= max_requests) throw std::out_of_range("invalid arrival ordinal");
    return count_before(0, ordinal + 1) != count_before(0, ordinal) ? 0 : 1;
}

EndpointMix load_workload_file(const std::string& path) {
    const auto doc = read_config_json(path, ConfigFile::workload);
    if (!doc.is_object() || doc.size() != 1 || !doc.contains("endpoints"))
        throw std::invalid_argument("workload requires an endpoints array");
    return parse_endpoints(doc.at("endpoints"));
}

EndpointMix parse_endpoints(const nlohmann::json& definition) {
    if (!definition.is_array() || definition.size() != EndpointMix::size)
        throw std::invalid_argument("workload requires exactly two ordered endpoints");
    std::array<WeightedEndpoint, EndpointMix::size> endpoints{};
    for (std::size_t i = 0; i < endpoints.size(); ++i) {
        const auto& e = definition.at(i);
        if (!e.is_object() || e.size() != 2 || !e.contains("path") || !e.at("path").is_string() ||
            !e.contains("weight") || !e.at("weight").is_number_unsigned())
            throw std::invalid_argument("each endpoint requires a path and a positive integer weight");
        endpoints[i] = {e.at("path").get<std::string>(), e.at("weight").get<std::uint64_t>()};
    }
    return EndpointMix(std::move(endpoints));
}
}
