#include "spe/headers.hpp"

#include <curl/curl.h>
#include "config_json.hpp"

#include <algorithm>
#include <array>
#include <set>
#include <stdexcept>

namespace spe {
namespace {
std::string lower(std::string name) {
    for (auto& c : name) if (c >= 'A' && c <= 'Z') c = static_cast<char>(c + ('a' - 'A'));
    return name;
}
}

bool is_normalized_header_name(std::string_view name) {
    if (name.empty() || name.size() > 128) return false;
    for (const char c : name)
        if (!((c >= 'a' && c <= 'z') || (c >= '0' && c <= '9') ||
              std::string_view("!#$%&'*+-.^_`|~").find(c) != std::string_view::npos)) return false;
    constexpr std::array controlled = {"host", "connection", "proxy-connection", "keep-alive", "transfer-encoding",
        "content-length", "trailer", "te", "upgrade", "expect", "accept-encoding", "user-agent",
        "proxy-authorization", "proxy-authenticate"};
    return std::find(controlled.begin(), controlled.end(), name) == controlled.end();
}

void HeaderListDeleter::operator()(curl_slist* list) const noexcept { curl_slist_free_all(list); }

PreparedHeaders load_headers_file(const std::string& path) {
    const auto document = read_config_json(path, ConfigFile::private_headers);
    if (!document.is_array() || document.size() > 32)
        throw std::invalid_argument("headers JSON must be an array with at most 32 entries");
    PreparedHeaders result;
    std::set<std::string> names;
    std::size_t wire_bytes = 0;
    for (const auto& header : document) {
        if (!header.is_object() || header.size() != 2 || !header.contains("name") || !header.contains("value") ||
            !header.at("name").is_string() || !header.at("value").is_string())
            throw std::invalid_argument("each header requires exactly string name and value fields");
        const auto name = lower(header.at("name").get<std::string>());
        const auto& value = header.at("value").get_ref<const std::string&>();
        if (!is_normalized_header_name(name)) throw std::invalid_argument("invalid or transport-controlled header name");
        if (!names.insert(name).second) throw std::invalid_argument("duplicate header name (case-insensitive)");
        if (value.size() > 4096 || std::any_of(value.begin(), value.end(), [](unsigned char c) { return c < 32 || c >= 127; }) ||
            (!value.empty() && (value.front() == ' ' || value.back() == ' ')))
            throw std::invalid_argument("header values require visible ASCII, no control characters or surrounding spaces, and at most 4096 bytes");
        wire_bytes += name.size() + value.size() + 4;
        if (wire_bytes > 16 * 1024) throw std::invalid_argument("configured headers exceed 16 KiB total wire size");
        // A semicolon requests a literal empty field, rather than libcurl suppression.
        const auto line = value.empty() ? name + ';' : name + ": " + value;
        auto* appended = curl_slist_append(result.lines.get(), line.c_str());
        if (!appended) throw std::runtime_error("cannot allocate request headers");
        result.lines.release();
        result.lines.reset(appended);
    }
    result.names.assign(names.begin(), names.end());
    return result;
}
}
