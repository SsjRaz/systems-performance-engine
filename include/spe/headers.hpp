#pragma once

#include <memory>
#include <string>
#include <string_view>
#include <vector>

struct curl_slist;

namespace spe {
struct HeaderListDeleter { void operator()(curl_slist* list) const noexcept; };
struct PreparedHeaders {
    std::unique_ptr<curl_slist, HeaderListDeleter> lines;
    std::vector<std::string> names;
};

// Private values live only in the immutable list, which must outlive easy handles.
PreparedHeaders load_headers_file(const std::string& path);
bool is_normalized_header_name(std::string_view name);
}
