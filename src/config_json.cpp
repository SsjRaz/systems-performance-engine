#include "config_json.hpp"

#include <array>
#include <cerrno>
#include <cstdint>
#include <fcntl.h>
#include <set>
#include <stdexcept>
#include <sys/stat.h>
#include <system_error>
#include <unistd.h>
#include <vector>

namespace spe {
namespace {
std::string read_file(const std::string& path, ConfigFile kind, const std::string& label) {
    constexpr std::size_t limit = 64 * 1024;
    const auto fd = ::open(path.c_str(), O_RDONLY | O_CLOEXEC | O_NONBLOCK);
    if (fd < 0) throw std::system_error(errno, std::generic_category(), "cannot open " + label + " file");
    struct Descriptor { int value; ~Descriptor() { ::close(value); } } owner{fd};
    struct stat info{};
    if (::fstat(fd, &info) != 0) throw std::system_error(errno, std::generic_category(), "cannot inspect " + label + " file");
    if (!S_ISREG(info.st_mode) || info.st_size <= 0 || static_cast<std::uintmax_t>(info.st_size) > limit)
        throw std::invalid_argument(label + " file must be a nonempty regular file of at most 64 KiB");
    if (kind == ConfigFile::private_headers && (info.st_mode & (S_IRWXG | S_IRWXO)))
        throw std::invalid_argument("headers file must have owner-only permissions (for example chmod 600)");
    std::string bytes;
    std::array<char, 4096> buffer{};
    while (true) {
        const auto n = ::read(fd, buffer.data(), buffer.size());
        if (n < 0 && errno == EINTR) continue;
        if (n < 0) throw std::system_error(errno, std::generic_category(), "cannot read " + label + " file");
        if (n == 0) break;
        bytes.append(buffer.data(), static_cast<std::size_t>(n));
        if (bytes.size() > limit) throw std::invalid_argument(label + " file exceeds 64 KiB");
    }
    return bytes;
}
}

nlohmann::json read_config_json(const std::string& path, ConfigFile kind) {
    using Json = nlohmann::json;
    const std::string label = kind == ConfigFile::private_headers ? "headers" : "workload";
    const auto bytes = read_file(path, kind, label);
    // The JSON lexer treats a raw NUL as end-of-input.
    if (bytes.find('\0') != std::string::npos) throw std::invalid_argument("invalid " + label + " JSON");
    Json document;
    try {
        std::vector<std::set<std::string>> keys;
        const auto callback = [&](int depth, Json::parse_event_t event, Json& value) {
            if (depth > 8) throw std::invalid_argument(label + " JSON nesting exceeds 8 levels");
            if (event == Json::parse_event_t::object_start) keys.emplace_back();
            if (event == Json::parse_event_t::object_end) keys.pop_back();
            if (event == Json::parse_event_t::key && !keys.back().insert(value.get<std::string>()).second)
                throw std::invalid_argument("duplicate configuration key");
            return true;
        };
        document = Json::parse(bytes, callback);
    } catch (const std::exception&) {
        // Parser diagnostics can quote the private input. Never forward them.
        throw std::invalid_argument("invalid " + label + " JSON (syntax, duplicate keys, encoding or nesting)");
    }
    return document;
}
}
