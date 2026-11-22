#pragma once

#include <array>
#include <cstdint>
#include <vector>

namespace freight {
struct Rate { std::uint32_t zone, cents; };
struct Parcel { std::uint32_t zone, weight, units; };
using Rates = std::array<Rate, 256>;
std::uint64_t quote(const std::vector<Parcel>& parcels, const Rates& rates);
}
