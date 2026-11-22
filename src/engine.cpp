#include "spe/engine.hpp"
#include "spe/schedule.hpp"
#include "spe/tls.hpp"
#include "spe/headers.hpp"
#include "spe/body.hpp"

#include <curl/curl.h>

#include <algorithm>
#include <chrono>
#include <limits>
#include <memory>
#include <stdexcept>
#include <string>
#include <thread>
#include <vector>

namespace spe {
namespace {
using Clock = std::chrono::steady_clock;
static_assert(Clock::is_steady);

class CurlRuntime {
public:
    CurlRuntime() {
        if (curl_global_init(CURL_GLOBAL_DEFAULT) != CURLE_OK) throw std::runtime_error("libcurl initialization failed");
    }
    ~CurlRuntime() { curl_global_cleanup(); }
    CurlRuntime(const CurlRuntime&) = delete;
    CurlRuntime& operator=(const CurlRuntime&) = delete;
};

void check(CURLcode code) {
    if (code != CURLE_OK) throw std::runtime_error(std::string("libcurl: ") + curl_easy_strerror(code));
}
void check(CURLMcode code) {
    if (code != CURLM_OK) throw std::runtime_error(std::string("libcurl multi: ") + curl_multi_strerror(code));
}

struct Slot {
    std::unique_ptr<CURL, decltype(&curl_easy_cleanup)> easy{curl_easy_init(), curl_easy_cleanup};
    Clock::time_point started;
    std::size_t body_bytes = 0;
    std::size_t body_limit = 0;
    std::size_t endpoint = 0;
    bool body_limit_hit = false;
    bool active = false;
};

std::size_t discard_body(char*, std::size_t size, std::size_t count, void* context) noexcept {
    auto& slot = *static_cast<Slot*>(context);
    if (size != 0 && count > std::numeric_limits<std::size_t>::max() / size) {
        slot.body_limit_hit = true;
        return 0;
    }
    const auto bytes = size * count;
    if (bytes > slot.body_limit - slot.body_bytes) {
        slot.body_limit_hit = true;
        return 0;
    }
    slot.body_bytes += bytes;
    return bytes;
}

// Removes every attached easy handle before cleaning up the multi handle, including
// stack unwinding. Slots outlive this owner so callback context stays valid.
class Multi {
public:
    explicit Multi(std::vector<std::unique_ptr<Slot>>& slots) : slots_(slots) {
        if (!handle_) throw std::runtime_error("cannot allocate libcurl multi handle");
    }
    ~Multi() {
        for (const auto& slot : slots_) {
            if (slot->active) curl_multi_remove_handle(handle_, slot->easy.get());
        }
        curl_multi_cleanup(handle_);
    }
    Multi(const Multi&) = delete;
    Multi& operator=(const Multi&) = delete;
    CURLM* get() const { return handle_; }

private:
    std::vector<std::unique_ptr<Slot>>& slots_;
    CURLM* handle_ = curl_multi_init();
};

Outcome classify(CURLcode code, long status, bool body_limit_hit) {
    if (body_limit_hit || code == CURLE_FILESIZE_EXCEEDED) return Outcome::response_limit;
    if (code == CURLE_OPERATION_TIMEDOUT) return Outcome::timeout;
    if (code == CURLE_COULDNT_CONNECT || code == CURLE_COULDNT_RESOLVE_HOST) return Outcome::connection_error;
    if (is_tls_error(static_cast<int>(code))) return Outcome::tls_error;
    if (code != CURLE_OK || status < 100 || status >= 600) return Outcome::network_error;
    return status >= 200 && status < 300 ? Outcome::success : Outcome::http_error;
}

void configure_method(CURL* easy, const std::string& method, const std::string& body) {
    if (method == "GET") {
        check(curl_easy_setopt(easy, CURLOPT_HTTPGET, 1L));
    } else if (method == "HEAD") {
        check(curl_easy_setopt(easy, CURLOPT_NOBODY, 1L));
    } else {
        // libcurl may replay a transfer on a stale reused connection. A write
        // gets a fresh HTTP/1.1 connection even when its easy handle is recycled.
        check(curl_easy_setopt(easy, CURLOPT_FRESH_CONNECT, 1L));
        check(curl_easy_setopt(easy, CURLOPT_FORBID_REUSE, 1L));
        if (method_accepts_body(method)) {
            check(curl_easy_setopt(easy, CURLOPT_POSTFIELDSIZE_LARGE, static_cast<curl_off_t>(body.size())));
            check(curl_easy_setopt(easy, CURLOPT_POSTFIELDS, body.c_str()));
        }
        check(curl_easy_setopt(easy, CURLOPT_CUSTOMREQUEST, method.c_str()));
    }
}
}

RunResult run(const Config& config, const std::function<bool()>& stop_requested) {
    validate_config(config);
    // One process-wide initialization, before creating any easy/multi handles.
    static CurlRuntime runtime;
    const auto* curl_info = curl_version_info(CURLVERSION_NOW);
    if (curl_info->version_num < 0x075500 || !(curl_info->features & CURL_VERSION_ASYNCHDNS)) {
        throw std::runtime_error("libcurl >= 7.85 with asynchronous DNS is required for bounded event-loop waits");
    }
    const bool https = is_https_url(config.url);
    if (https && !(curl_info->features & CURL_VERSION_SSL)) throw std::runtime_error("HTTPS requires a TLS-enabled libcurl");
    if (https) {
        bool available = false;
        for (auto protocol = curl_info->protocols; protocol && *protocol; ++protocol)
            available = available || std::string_view(*protocol) == "https";
        if (!available) throw std::runtime_error("libcurl was built without HTTPS support");
    }
    // Immutable bundle storage outlives all easy handles. No trust material/path is exported.
    auto ca_bundle = config.ca_file.empty() ? CaBundle{} : load_ca_bundle(config.ca_file);
    auto headers = config.headers_file.empty() ? PreparedHeaders{} : load_headers_file(config.headers_file);
    const auto request_body = config.body_file.empty() ? std::string{} : load_request_body(config.body_file);
    if (method_accepts_body(config.method)) {
        // Suppress automatic 100-continue negotiation and its 417 retry path.
        headers.append_line("Expect:");
        if (!std::binary_search(headers.names.begin(), headers.names.end(), "content-type"))
            headers.append_line("Content-Type: application/octet-stream");
    }
    curl_blob ca_blob{ca_bundle.bytes.data(), ca_bundle.bytes.size(), CURL_BLOB_NOCOPY};
    const auto user_agent = std::string("systems-performance-engine/") + std::string(version());
    const auto budget = config.request_budget();
    const auto limit = std::min(config.concurrency, static_cast<std::size_t>(budget));
    std::vector<std::string> endpoint_urls;
    if (config.mix) {
        auto origin = config.url;
        if (origin.ends_with('/')) origin.pop_back();
        for (const auto& endpoint : config.mix->endpoints()) endpoint_urls.push_back(origin + endpoint.path);
    }
    std::vector<std::unique_ptr<Slot>> slots;
    slots.reserve(limit);
    for (std::size_t i = 0; i < limit; ++i) {
        auto slot = std::make_unique<Slot>();
        if (!slot->easy) throw std::runtime_error("cannot allocate libcurl request handle");
        auto* easy = slot->easy.get();
        slot->body_limit = config.max_body_bytes;
        check(curl_easy_setopt(easy, CURLOPT_URL, endpoint_urls.empty() ? config.url.c_str() : endpoint_urls.front().c_str()));
        check(curl_easy_setopt(easy, CURLOPT_PROTOCOLS_STR, https ? "https" : "http"));
        check(curl_easy_setopt(easy, CURLOPT_HTTP_VERSION, static_cast<long>(CURL_HTTP_VERSION_1_1)));
        configure_method(easy, config.method, request_body);
        check(curl_easy_setopt(easy, CURLOPT_FOLLOWLOCATION, 0L));
        check(curl_easy_setopt(easy, CURLOPT_PROXY, ""));
        check(curl_easy_setopt(easy, CURLOPT_NETRC, static_cast<long>(CURL_NETRC_IGNORED)));
        check(curl_easy_setopt(easy, CURLOPT_NOSIGNAL, 1L));
        check(curl_easy_setopt(easy, CURLOPT_TIMEOUT_MS, config.timeout_ms));
        check(curl_easy_setopt(easy, CURLOPT_CONNECTTIMEOUT_MS, config.timeout_ms));
        check(curl_easy_setopt(easy, CURLOPT_ACCEPT_ENCODING, "identity"));
        check(curl_easy_setopt(easy, CURLOPT_HTTP_CONTENT_DECODING, 0L));
        check(curl_easy_setopt(easy, CURLOPT_USERAGENT, user_agent.c_str()));
        check(curl_easy_setopt(easy, CURLOPT_HTTPHEADER, headers.lines.get()));
        check(curl_easy_setopt(easy, CURLOPT_HEADEROPT, static_cast<long>(CURLHEADER_SEPARATE)));
        if (https) {
            check(curl_easy_setopt(easy, CURLOPT_SSL_VERIFYPEER, 1L));
            check(curl_easy_setopt(easy, CURLOPT_SSL_VERIFYHOST, 2L));
            check(curl_easy_setopt(easy, CURLOPT_SSLVERSION, static_cast<long>(CURL_SSLVERSION_TLSv1_2)));
            if (!config.ca_file.empty()) {
                check(curl_easy_setopt(easy, CURLOPT_CAINFO, static_cast<char*>(nullptr)));
                const auto clear_path = curl_easy_setopt(easy, CURLOPT_CAPATH, static_cast<char*>(nullptr));
                if (clear_path != CURLE_NOT_BUILT_IN) check(clear_path);
                // Unsupported custom-CA backends fail before dispatch; there is no fallback.
                check(curl_easy_setopt(easy, CURLOPT_CAINFO_BLOB, &ca_blob));
            }
        }
        check(curl_easy_setopt(easy, CURLOPT_WRITEFUNCTION, &discard_body));
        check(curl_easy_setopt(easy, CURLOPT_WRITEDATA, slot.get()));
        check(curl_easy_setopt(easy, CURLOPT_PRIVATE, slot.get()));
        slots.push_back(std::move(slot));
    }
    Multi multi(slots);
    check(curl_multi_setopt(multi.get(), CURLMOPT_MAX_TOTAL_CONNECTIONS, static_cast<long>(limit)));
    check(curl_multi_setopt(multi.get(), CURLMOPT_MAX_HOST_CONNECTIONS, static_cast<long>(limit)));
    check(curl_multi_setopt(multi.get(), CURLMOPT_MAXCONNECTS, static_cast<long>(limit)));
    check(curl_multi_setopt(multi.get(), CURLMOPT_PIPELINING, CURLPIPE_NOTHING));

    Measurements measurements(config, ca_bundle.sha256, headers.names, request_body.size());
    std::size_t in_flight = 0;
    std::size_t peak = 0;
    std::vector<Slot*> ready;
    ready.reserve(limit);
    bool interrupted = false;
    std::string engine_error;
    const auto stopped = [&] { return stop_requested && stop_requested(); };
    std::optional<ArrivalSchedule> schedule;
    if (config.fixed_rate) {
        schedule.emplace(*config.fixed_rate);
        for (const auto& slot : slots) ready.push_back(slot.get());
    }
    const auto begin = Clock::now();
    const auto start = [&](Slot& slot, std::optional<ArrivalSchedule::Offset> deadline = {}, std::size_t endpoint = 0) {
        if (slot.endpoint != endpoint) {
            check(curl_easy_setopt(slot.easy.get(), CURLOPT_URL, endpoint_urls.at(endpoint).c_str()));
            slot.endpoint = endpoint;
        }
        slot.body_bytes = 0;
        slot.body_limit_hit = false;
        slot.started = Clock::now();
        check(curl_multi_add_handle(multi.get(), slot.easy.get()));
        slot.active = true;
        std::optional<double> delay_ms;
        if (deadline) {
            delay_ms = std::chrono::duration<double, std::milli>((slot.started - begin) - *deadline).count();
        }
        measurements.start(delay_ms, endpoint);
        ++in_flight;
        peak = std::max(peak, in_flight);
    };
    const auto perform = [&] {
        int running = 0;
        check(curl_multi_perform(multi.get(), &running));
        int remaining = 0;
        // Drain the entire completion batch before reusing any handle.
        while (auto* message = curl_multi_info_read(multi.get(), &remaining)) {
            if (message->msg != CURLMSG_DONE) continue;
            char* context = nullptr;
            check(curl_easy_getinfo(message->easy_handle, CURLINFO_PRIVATE, &context));
            auto& slot = *reinterpret_cast<Slot*>(context);
            const auto duration = std::chrono::duration<double, std::milli>(Clock::now() - slot.started).count();
            const auto code = message->data.result;
            long status = 0;
            check(curl_easy_getinfo(message->easy_handle, CURLINFO_RESPONSE_CODE, &status));
            check(curl_multi_remove_handle(multi.get(), message->easy_handle));
            measurements.record(classify(code, status, slot.body_limit_hit), duration, status, static_cast<int>(code), slot.endpoint);
            slot.active = false;
            --in_flight;
            ready.push_back(&slot);
        }
    };

    try {
        if (schedule) {
            for (;;) {
                if (in_flight != 0) perform();
                if (stopped()) { interrupted = true; break; }

                const auto now = Clock::now();
                const auto elapsed = std::chrono::duration_cast<ArrivalSchedule::Offset>(now - begin);
                const auto due = schedule->advance(elapsed, !ready.empty());
                measurements.miss_arrivals(due.expired, due.at_capacity, due.first);
                if (due.dispatch) {
                    const auto endpoint = config.mix ? config.mix->endpoint_at(due.first + due.expired) : 0;
                    start(*ready.back(), due.dispatch, endpoint);
                    ready.pop_back();
                    continue; // Drive newly registered work before blocking on sockets.
                }

                int wait_ms = 50;
                if (elapsed < schedule->window()) {
                    const auto wake = std::min(schedule->next_wake(), elapsed + std::chrono::milliseconds(50));
                    if (in_flight == 0) {
                        // No transfer needs socket progress. Preserve sub-millisecond
                        // arrival deadlines and the normal cancellation check interval.
                        std::this_thread::sleep_until(begin + wake);
                        continue;
                    }
                    wait_ms = static_cast<int>(std::chrono::ceil<std::chrono::milliseconds>(wake - elapsed).count());
                } else if (in_flight == 0) {
                    break; // All arrivals accounted for; all dispatched transfers drained.
                }
                check(curl_multi_poll(multi.get(), nullptr, 0, wait_ms, nullptr));
            }
        } else {
            for (const auto& slot : slots) {
                if (stopped()) break;
                start(*slot);
            }
            while (in_flight != 0) {
                ready.clear();
                perform();
                if (stopped()) { interrupted = true; break; }
                for (auto* slot : ready) {
                    if (measurements.counters().started == budget || stopped()) break;
                    start(*slot);
                }
                if (stopped()) { interrupted = true; break; }
                if (in_flight != 0) {
                    // Also bounds signal observation latency when all sockets are idle.
                    check(curl_multi_poll(multi.get(), nullptr, 0, 50, nullptr));
                }
            }
            interrupted = interrupted || (stopped() && measurements.counters().started < budget);
        }
    } catch (const std::exception& error) {
        engine_error = error.what();
    }
    for (const auto& slot : slots) {
        if (!slot->active) continue;
        const auto code = curl_multi_remove_handle(multi.get(), slot->easy.get());
        if (code != CURLM_OK && engine_error.empty()) engine_error = curl_multi_strerror(code);
        slot->active = false;
        measurements.record(Outcome::cancelled,
            std::chrono::duration<double, std::milli>(Clock::now() - slot->started).count(), 0, 0, slot->endpoint);
    }
    const auto elapsed = std::chrono::duration<double>(Clock::now() - begin).count();
    return measurements.finish(elapsed, peak, interrupted, std::move(engine_error));
}
}
