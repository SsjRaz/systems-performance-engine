#include "spe/headers.hpp"

#include <curl/curl.h>
#include <nlohmann/json.hpp>

#include <algorithm>
#include <array>
#include <cerrno>
#include <cstdint>
#include <fcntl.h>
#include <set>
#include <stdexcept>
#include <sys/stat.h>
#include <system_error>
#include <unistd.h>

namespace spe {
namespace {
std::string read_file(const std::string& path) {
    constexpr std::size_t limit = 64 * 1024;
    const auto fd = ::open(path.c_str(), O_RDONLY | O_CLOEXEC | O_NONBLOCK);
    if (fd < 0) throw std::system_error(errno, std::generic_category(), "cannot open headers file");
    struct Descriptor { int value; ~Descriptor() { ::close(value); } } owner{fd};
    struct stat info{};
    if (::fstat(fd, &info) != 0) throw std::system_error(errno, std::generic_category(), "cannot inspect headers file");
    if (!S_ISREG(info.st_mode) || info.st_size <= 0 || static_cast<std::uintmax_t>(info.st_size) > limit)
        throw std::invalid_argument("headers file must be a nonempty regular file of at most 64 KiB");
    if (info.st_mode & (S_IRWXG | S_IRWXO))
        throw std::invalid_argument("headers file must have owner-only permissions (for example chmod 600)");
    std::string bytes;
    std::array<char, 4096> buffer{};
    while (true) {
        const auto n = ::read(fd, buffer.data(), buffer.size());
        if (n < 0 && errno == EINTR) continue;
        if (n < 0) throw std::system_error(errno, std::generic_category(), "cannot read headers file");
        if (n == 0) break;
        bytes.append(buffer.data(), static_cast<std::size_t>(n));
        if (bytes.size() > limit) throw std::invalid_argument("headers file exceeds 64 KiB");
    }
    return bytes;
}

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
    using Json = nlohmann::json;
    const auto bytes = read_file(path);
    Json document;
    try {
        std::vector<std::set<std::string>> keys;
        const auto callback = [&](int depth, Json::parse_event_t event, Json& value) {
            if (depth > 8) throw std::invalid_argument("headers JSON nesting exceeds 8 levels");
            if (event == Json::parse_event_t::object_start) keys.emplace_back();
            if (event == Json::parse_event_t::object_end) keys.pop_back();
            if (event == Json::parse_event_t::key && !keys.back().insert(value.get<std::string>()).second)
                throw std::invalid_argument("duplicate headers JSON key");
            return true;
        };
        document = Json::parse(bytes, callback);
    } catch (const std::exception&) {
        // Parser diagnostics can quote the private input. Never forward them.
        throw std::invalid_argument("invalid headers JSON (syntax, duplicate keys, encoding or nesting)");
    }
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
