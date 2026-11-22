#include "catalog.hpp"

#include <stdexcept>

namespace freight {
// Rate cards are indexed by zone. Keep these three business operations visible in sampled stacks.
[[gnu::noinline]] std::uint32_t lookup_rate(const Rates& rates, std::uint32_t zone) {
    return rates[zone].cents;
}

[[gnu::noinline]] std::uint32_t handling_class(const Parcel& parcel) {
    auto key = parcel.weight * 257U + parcel.zone;
    for (unsigned band = 0; band < 16; ++band) {
        key ^= key >> 15;
        key *= 2246822519U;
        key ^= key >> 13;
    }
    return key % 97;
}

[[gnu::noinline]] std::uint64_t quote(const std::vector<Parcel>& parcels, const Rates& rates) {
    std::uint64_t total = 0;
    for (const auto& parcel : parcels) {
        const auto rate = lookup_rate(rates, parcel.zone);
        total += (rate + handling_class(parcel)) * parcel.units;
    }
    return total;
}
}
