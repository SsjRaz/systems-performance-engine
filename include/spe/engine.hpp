#pragma once

#include "spe/config.hpp"
#include "spe/result.hpp"

#include <functional>

namespace spe {
// Serializes all HTTP handle access and measurement updates on the calling thread.
// The optional predicate is polled on that thread; it must not block or throw.
RunResult run(const Config& config, const std::function<bool()>& stop_requested = {});
}
