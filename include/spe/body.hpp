#pragma once

#include <string>

namespace spe {
// Read a bounded owner-only file once, before registering any transfers.
std::string load_request_body(const std::string& path);
}
