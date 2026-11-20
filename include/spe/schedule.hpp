#pragma once

#include "spe/config.hpp"

#include <chrono>
#include <optional>

namespace spe {
// Single-owner schedule. Times are monotonic offsets from the start of the run.
class ArrivalSchedule {
public:
    using Offset = std::chrono::nanoseconds;
    struct Decision {
        std::uint64_t first = 0; // First ordinal evaluated in this batch.
        std::uint64_t expired = 0;
        bool at_capacity = false;
        std::optional<Offset> dispatch;
    };

    explicit ArrivalSchedule(FixedRate rate);
    Decision advance(Offset elapsed, bool slot_available);
    Offset next_wake() const; // Window end after the last arrival.
    Offset window() const { return window_; }

private:
    Offset deadline(std::uint64_t index) const;
    std::uint64_t rate_;
    std::uint64_t total_;
    Offset window_;
    std::uint64_t next_ = 0;
};
}
