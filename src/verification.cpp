#include "verification.hpp"
#include "config_json.hpp"
#include "spe/tls.hpp"

#include <algorithm>
#include <cstring>
#include <memory>
#include <stdexcept>

namespace spe {
namespace {
using Json = nlohmann::json;
constexpr std::size_t max_probe_bytes = 256 * 1024;

struct Rule {
    std::string digest;
    std::vector<std::pair<Json::json_pointer, Json>> fields;
};

Rule read_rule(const Json& item) {
    Rule rule;
    if (!item.is_object() || item.size() != 1) throw std::invalid_argument("each probe requires one assertion kind");
    if (item.contains("sha256") && item.at("sha256").is_string()) {
        rule.digest = item.at("sha256").get<std::string>();
        if (rule.digest.size() != 64 || rule.digest.find_first_not_of("0123456789abcdef") != std::string::npos)
            throw std::invalid_argument("expected a lowercase SHA-256 digest");
        return rule;
    }
    if (!item.contains("fields") || !item.at("fields").is_object() ||
        item.at("fields").empty() || item.at("fields").size() > 16)
        throw std::invalid_argument("each JSON probe requires 1..16 pointer/scalar assertions");
    for (const auto& [key, expected] : item.at("fields").items()) {
        if (key.size() > 256 || !expected.is_primitive())
            throw std::invalid_argument("invalid verification pointer or expected scalar");
        try {
            rule.fields.emplace_back(Json::json_pointer(key), expected);
        } catch (const Json::exception&) {
            throw std::invalid_argument("invalid verification JSON pointer");
        }
    }
    return rule;
}

struct Capture {
    std::vector<char> buffer;
    std::size_t used = 0;
    bool overflow = false;
};

std::size_t capture(char* data, std::size_t size, std::size_t count, void* context) noexcept {
    auto& c = *static_cast<Capture*>(context);
    if (size && count > (c.buffer.size() - c.used) / size) {
        c.overflow = true;
        return 0;
    }
    const auto bytes = size * count;
    if (bytes) std::memcpy(c.buffer.data() + c.used, data, bytes);
    c.used += bytes;
    return bytes;
}

int progress(void* context, curl_off_t, curl_off_t, curl_off_t, curl_off_t) noexcept {
    const auto& stop = *static_cast<const std::function<bool()>*>(context);
    return stop && stop() ? 1 : 0;
}

void checked(CURLcode code) {
    if (code != CURLE_OK) throw std::runtime_error("cannot configure response probe");
}

std::string verify_body(const Rule& rule, std::string_view bytes) {
    if (!rule.digest.empty()) return sha256_hex(bytes) == rule.digest ? "" : "content_mismatch";
    Json doc;
    try {
        doc = parse_bounded_json(bytes, "response", 32, 16384);
    } catch (const std::invalid_argument&) {
        return "invalid_json";
    }
    try {
        for (const auto& [pointer, expected] : rule.fields) {
            const auto& actual = doc.at(pointer);
            if (actual.type() != expected.type() || actual != expected) return "content_mismatch";
        }
    } catch (const Json::exception&) {
        return "content_mismatch";
    }
    return {};
}
}

bool valid_public_id(std::string_view id) {
    return !id.empty() && id.size() <= 80 &&
        id.find_first_not_of("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-") == id.npos;
}

Verification preflight(CURL* configured, const Config& config, const std::vector<std::string>& mixed_urls,
                       const std::function<bool()>& stop_requested) {
    const auto spec = read_config_json(config.verification_file, ConfigFile::verification);
    const auto endpoints = mixed_urls.empty() ? 1U : mixed_urls.size();
    if (!spec.is_object() || spec.size() != 2 || !spec.contains("policy_id") || !spec.at("policy_id").is_string() ||
        !spec.contains("endpoints") || !spec.at("endpoints").is_array() || spec.at("endpoints").size() != endpoints)
        throw std::invalid_argument("verification requires a public policy_id and one rule per configured endpoint");
    Verification report;
    report.policy_id = spec.at("policy_id").get<std::string>();
    if (!valid_public_id(report.policy_id)) throw std::invalid_argument("invalid public verification policy_id");
    std::vector<Rule> rules;
    for (const auto& item : spec.at("endpoints")) rules.push_back(read_rule(item));
    Capture response{std::vector<char>(std::min(config.max_body_bytes, max_probe_bytes))};
    // A duplicate inherits the exact wire policy and immutable request inputs. Its
    // connection cache is private and dies before the timed multi handle starts.
    std::unique_ptr<CURL, decltype(&curl_easy_cleanup)> easy(curl_easy_duphandle(configured), curl_easy_cleanup);
    if (!easy) throw std::runtime_error("cannot allocate response probe");
    checked(curl_easy_setopt(easy.get(), CURLOPT_WRITEFUNCTION, &capture));
    checked(curl_easy_setopt(easy.get(), CURLOPT_WRITEDATA, &response));
    checked(curl_easy_setopt(easy.get(), CURLOPT_NOPROGRESS, 0L));
    checked(curl_easy_setopt(easy.get(), CURLOPT_XFERINFOFUNCTION, &progress));
    checked(curl_easy_setopt(easy.get(), CURLOPT_XFERINFODATA, &stop_requested));
    for (std::size_t i = 0; i < rules.size(); ++i) {
        if (stop_requested && stop_requested()) { report.failure = "cancelled"; break; }
        const auto& url = mixed_urls.empty() ? config.url : mixed_urls[i];
        checked(curl_easy_setopt(easy.get(), CURLOPT_URL, url.c_str()));
        response.used = 0;
        ++report.attempted;
        const auto code = curl_easy_perform(easy.get());
        long status = 0;
        checked(curl_easy_getinfo(easy.get(), CURLINFO_RESPONSE_CODE, &status));
        if (response.overflow) report.failure = "response_limit";
        else if (code == CURLE_ABORTED_BY_CALLBACK) report.failure = "cancelled";
        else if (code == CURLE_OPERATION_TIMEDOUT) report.failure = "timeout";
        else if (is_tls_error(static_cast<int>(code))) report.failure = "tls_error";
        else if (code != CURLE_OK) report.failure = "transport_error";
        else if (status < 200 || status >= 300) report.failure = "http_error";
        else report.failure = verify_body(rules[i], {response.buffer.data(), response.used});
        if (!report.failure.empty()) break;
        ++report.passed;
    }
    return report;
}
}
