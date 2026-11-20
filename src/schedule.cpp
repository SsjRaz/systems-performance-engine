#include "spe/schedule.hpp"

namespace spe {
namespace {
constexpr std::uint64_t ns_per_second = 1'000'000'000;
}

ArrivalSchedule::ArrivalSchedule(FixedRate rate)
    : rate_(rate.per_second), total_(rate.planned_arrivals()), window_(std::chrono::milliseconds(rate.duration_ms)) {}

ArrivalSchedule::Offset ArrivalSchedule::deadline(std::uint64_t index) const {
    // Round up to the first eligible nanosecond, always relative to the same epoch.
    return Offset((index * ns_per_second + rate_ - 1) / rate_);
}

ArrivalSchedule::Offset ArrivalSchedule::next_wake() const {
    return next_ == total_ ? window_ : deadline(next_);
}

ArrivalSchedule::Decision ArrivalSchedule::advance(Offset elapsed, bool slot_available) {
    Decision decision;
    if (elapsed >= window_) {
        decision.expired = total_ - next_;
        next_ = total_;
    } else if (elapsed >= next_wake()) {
        // Before window end, latest is always in [next_, total_). No catch-up queue.
        const auto latest = static_cast<std::uint64_t>(elapsed.count()) * rate_ / ns_per_second;
        decision.expired = latest - next_;
        if (slot_available) decision.dispatch = deadline(latest);
        else decision.at_capacity = true;
        next_ = latest + 1;
    }
    return decision;
}
}
