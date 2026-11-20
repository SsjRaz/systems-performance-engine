#pragma once

#include <array>
#include <cstddef>
#include <cstdint>
#include <string>
#include <string_view>

namespace spe {
struct WeightedEndpoint {
    std::string path;
    std::uint64_t weight;
};

class EndpointMix {
public:
    static constexpr std::string_view assignment = "nearest_prefix_v1";
    static constexpr std::size_t size = 2;
    explicit EndpointMix(std::array<WeightedEndpoint, size> endpoints);
    const auto& endpoints() const { return endpoints_; }

    // Counts in [0, arrivals). Indices and arrival counts come from the bounded scheduler.
    std::uint64_t count_before(std::size_t endpoint, std::uint64_t arrivals) const;
    std::size_t endpoint_at(std::uint64_t ordinal) const;

private:
    std::array<WeightedEndpoint, size> endpoints_;
    std::uint64_t total_weight_;
};

EndpointMix load_workload_file(const std::string& path);
}
