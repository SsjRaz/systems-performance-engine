#include "spe/config.hpp"
#include "spe/schedule.hpp"
#include "spe/engine.hpp"
#include "spe/result.hpp"
#include "spe/tls.hpp"
#include "spe/headers.hpp"

#include <cmath>
#include <iostream>
#include <limits>
#include <numeric>
#include <sstream>
#include <stdexcept>
#include <string>
#include <vector>
#include <unistd.h>

namespace {
void require(bool condition, const std::string& message) {
    if (!condition) throw std::runtime_error(message);
}
template<class F> void invalid(F function) {
    bool rejected = false;
    try { function(); } catch (const std::exception&) { rejected = true; }
    require(rejected, "invalid input was accepted");
}
spe::Config config() {
    spe::Config c;
    c.url = "http://127.0.0.1:8080/test?key=value";
    return c;
}

void cli_and_validation() {
    auto write = config();
    write.method = "POST";
    invalid([&] { spe::validate_config(write); });  // No implicit thousand-write run via the library.
    write.requests = 1;
    spe::validate_config(write);
    write.fixed_rate = spe::FixedRate{10, 500};
    spe::validate_config(write);
    const std::vector<std::string_view> args = {"http://localhost:80/", "--requests", "37", "--concurrency", "4",
        "--timeout-ms", "250", "--json", "-", "--max-body-bytes", "2048"};
    const auto c = spe::parse_cli(args).config;
    require(c.requests == 37 && c.concurrency == 4 && c.timeout_ms == 250 &&
        c.json_path == "-" && c.max_body_bytes == 2048, "CLI values");
    for (const auto url : {"http://localhost/", "http://[::1]:1234/a", "HTTP://127.0.0.1/?x=%20", "https://localhost/", "HTTPS://[::1]:8443/a"}) {
        auto valid = config(); valid.url = url; spe::validate_config(valid);
    }
    for (const auto url : {"", "localhost/path", "ftp://localhost/", "file:///tmp/test", "http://",
            "http://user:secret@localhost/", "http://localhost/a#fragment", "http://localhost/#",
            "http://localhost:0/", "http://localhost:65536/", "http://localhost/a b", "http://localhost/\n"}) {
        invalid([&] { auto bad = config(); bad.url = url; spe::validate_config(bad); });
    }
    for (auto value : {0ULL, 1'000'001ULL}) {
        invalid([&] { auto bad = config(); bad.requests = value; spe::validate_config(bad); });
    }
    for (const auto value : {0U, 1025U}) {
        invalid([&] { auto bad = config(); bad.concurrency = value; spe::validate_config(bad); });
    }
    for (const auto value : {-1L, 0L, 3'600'001L}) {
        invalid([&] { auto bad = config(); bad.timeout_ms = value; spe::validate_config(bad); });
    }
    invalid([] { auto bad = config(); bad.max_body_bytes = 0; spe::validate_config(bad); });
    invalid([] { auto bad = config(); bad.max_body_bytes = 1'073'741'825; spe::validate_config(bad); });
    for (const auto value : {"", "-1", "0", "1.2", "1x", "18446744073709551616", "999999999999999999"}) {
        invalid([&] { const std::vector<std::string_view> bad = {"http://localhost/", "--requests", value}; spe::parse_cli(bad); });
    }
    for (const std::vector<std::string_view>& bad : {
            std::vector<std::string_view>{}, {"http://localhost/", "--unknown"}, {"http://localhost/", "--requests"},
            {"http://localhost/", "--requests", "1", "--requests", "2"},
            {"http://localhost/", "http://other/"}, {"http://localhost/", "--json", "--requests"}}) {
        invalid([&] { spe::parse_cli(bad); });
    }
    const std::vector<std::string_view> help = {"--help"};
    require(spe::parse_cli(help).help, "help without URL");
    const std::vector<std::string_view> secure = {"HTTPS://localhost/", "--ca-file", "/private/test-root.pem"};
    require(spe::parse_cli(secure).config.ca_file == "/private/test-root.pem", "custom HTTPS CA option");
    const std::vector<std::string_view> headers = {"https://localhost/", "--headers-file", "private.json"};
    require(spe::parse_cli(headers).config.headers_file == "private.json", "private headers file option");
    require(spe::is_https_url("HTTPS:/localhost/"), "TLS scheme detection follows the explicit scheme");
    for (const std::vector<std::string_view>& bad : {
            std::vector<std::string_view>{"http://localhost/", "--ca-file", "roots.pem"},
            {"https://localhost/", "--ca-file"}, {"https://localhost/", "--ca-file", ""},
            {"https://localhost/", "--ca-file", "a.pem", "--ca-file", "b.pem"},
            {"https://localhost/", "--insecure"}, {"https://localhost/", "-k"},
            {"http://localhost/", "--headers-file"}, {"http://localhost/", "--headers-file", ""},
            {"http://localhost/", "--headers-file", "a", "--headers-file", "b"}}) {
        invalid([&] { spe::parse_cli(bad); });
    }
}

void fixed_rate_schedule() {
    using namespace std::chrono_literals;
    const auto parse = [](std::initializer_list<std::string_view> options) {
        std::vector<std::string_view> args{"http://localhost/"};
        args.insert(args.end(), options);
        return spe::parse_cli(args).config;
    };
    require(parse({"--rate", "3", "--duration-ms", "1001"}).request_budget() == 4, "fractional final period");
    require(parse({"--duration-ms", "1", "--rate", "1"}).request_budget() == 1, "sub-period window");
    require(parse({"--rate", "1000000", "--duration-ms", "1000"}).request_budget() == spe::max_requests, "bounded maximum");
    for (std::initializer_list<std::string_view> args : {
            std::initializer_list<std::string_view>{"--rate", "1"}, {"--duration-ms", "1"},
            {"--rate", "0", "--duration-ms", "1"}, {"--rate", "1", "--duration-ms", "0"},
            {"--rate", "1", "--duration-ms", "1", "--requests", "1"},
            {"--rate", "1000000", "--duration-ms", "1001"}, {"--rate", "1", "--duration-ms", "3600001"},
            {"--rate", "18446744073709551615", "--duration-ms", "2"}}) {
        invalid([&] { parse(args); });
    }
    spe::ArrivalSchedule s({3, 1001});
    require(s.next_wake() == 0ns && s.advance(10ns, true).dispatch == 0ns, "immediate first arrival");
    require(s.next_wake() == 333333334ns && !s.advance(333333333ns, true).dispatch, "never before fractional deadline");
    auto due = s.advance(666666667ns, true);
    require(due.expired == 1 && due.dispatch == 666666667ns && !due.at_capacity, "only newest due arrival eligible");
    require(!s.advance(666666667ns, true).dispatch && s.next_wake() == 1s, "no replay at same instant");
    due = s.advance(1s, false);
    require(due.at_capacity && !due.dispatch && due.expired == 0, "full capacity drops current arrival");
    require(s.next_wake() == 1001ms && !s.advance(1001ms, true).dispatch, "exclusive end, no backlog");
    spe::ArrivalSchedule stalled({1000, 1000});
    due = stalled.advance(1s, true);
    require(due.expired == 1000 && !due.dispatch && !due.at_capacity, "window expired while scheduler asleep");
    require(stalled.advance(2s, true).expired == 0, "each missed tick counted once");

    auto c = parse({"--rate", "3", "--duration-ms", "1001"});
    spe::Measurements measured(c);
    invalid([&] { measured.miss_arrivals(std::numeric_limits<std::uint64_t>::max(), true); });
    invalid([&] { measured.miss_arrivals(4, true); });
    measured.start(0.0);
    measured.record(spe::Outcome::success, 1.0, 200);
    measured.miss_arrivals(2, true);
    invalid([&] { measured.start(0.0); });
    const auto full = measured.finish(1.001, 1, false);
    require(full.scheduling.offered == 4 && full.scheduling.missed_late == 2 &&
            full.scheduling.missed_capacity == 1, "started and missed arrivals share one bounded budget");

    auto result = spe::run(c, [] { return true; });
    require(result.interrupted && result.counters.not_started == 4 && result.scheduling.offered == 0 &&
            result.scheduling.dispatch_delay_ms.samples == 0, "stop before schedule starts");
}

void weighted_arrivals() {
    const auto mix = [](std::uint64_t a, std::uint64_t b) {
        return spe::EndpointMix(std::array{spe::WeightedEndpoint{"/a", a}, spe::WeightedEndpoint{"/b", b}});
    };
    for (const auto& [a, b] : {std::pair{4u, 1u}, {1u, 4u}, {1u, 1u}, {7u, 13u}, {999999u, 1u}}) {
        const auto m = mix(a, b);
        std::uint64_t first = 0;
        for (std::uint64_t n = 1; n <= 1000; ++n) {
            if (m.endpoint_at(n - 1) == 0) ++first;
            require(first == m.count_before(0, n) && n-first == m.count_before(1, n), "prefix and routing agree");
            const auto error = static_cast<std::int64_t>(first*(a+b)) - static_cast<std::int64_t>(n*a);
            require(2*std::abs(error) <= a+b, "every prefix stays within half an arrival of quota");
        }
    }
    auto c = config();
    c.fixed_rate = spe::FixedRate{100, 10000};
    c.mix = mix(80, 20);
    c.url = "http://localhost";
    require(c.mix->endpoints()[0].weight == 4 && c.mix->endpoints()[1].weight == 1, "canonical weights");
    require(mix(1, 1).endpoint_at(0) == 0 && mix(1, 1).endpoint_at(1) == 1, "ties favor first endpoint");
    require(c.mix->count_before(0, spe::max_requests) == 800000, "maximum bounded prefix");
    invalid([&] { c.mix->endpoint_at(spe::max_requests); });
    invalid([&] { c.mix->count_before(2, 3); });
    spe::ArrivalSchedule schedule(*c.fixed_rate);
    spe::Measurements m(c);
    const auto jump = schedule.advance(std::chrono::milliseconds(55), false);
    require(jump.first == 0 && jump.expired == 5 && jump.at_capacity, "jump contains expired range and one current arrival");
    m.miss_arrivals(jump.expired, jump.at_capacity, jump.first);
    const auto next = schedule.advance(std::chrono::milliseconds(60), true);
    require(next.first == 6 && next.dispatch.has_value(), "never replay expired ordinals");
    m.start(0, c.mix->endpoint_at(next.first));
    m.record(spe::Outcome::cancelled, 0, 0, 0, 0);
    const auto r = m.finish(0.061, 1, true);
    require(r.endpoints[0].missed_late == 4 && r.endpoints[1].missed_late == 1 &&
            r.endpoints[0].missed_capacity == 1 && r.endpoints[0].counters.cancelled == 1, "per-endpoint interrupted prefix");
    spe::Measurements stalled(c);
    stalled.miss_arrivals(1000, false, 0);
    const auto expired = stalled.finish(10.001, 0, false);
    require(expired.endpoints[0].missed_late == 800 && expired.endpoints[1].missed_late == 200, "whole window expiry");
}

void percentiles() {
    require(spe::summarize_latencies({}).samples == 0, "empty latency");
    const auto one = spe::summarize_latencies({7.5});
    require(one.samples == 1 && one.p50 == 7.5 && one.p99 == 7.5 && one.mean == 7.5, "singleton latency");
    const auto three = spe::summarize_latencies({20, 0, 10});
    require(three.min == 0 && three.max == 20 && three.mean == 10 && three.p50 == 10 && three.p95 == 20, "unsorted latency");
    std::vector<double> hundred(100);
    std::iota(hundred.begin(), hundred.end(), 1.0);
    const auto many = spe::summarize_latencies(hundred);
    require(many.p50 == 50 && many.p95 == 95 && many.p99 == 99 && many.mean == 50.5, "nearest ranks");
    require(spe::summarize_latencies({2, 1}).p50 == 1, "nearest rank for two samples");
    invalid([] { spe::summarize_latencies({-1}); });
    invalid([] { spe::summarize_latencies({std::numeric_limits<double>::quiet_NaN()}); });
}

void accounting() {
    auto c = config(); c.requests = 10;
    c.url = "https://127.0.0.1:8080/test";
    spe::Measurements m(c);
    for (int i = 0; i < 9; ++i) m.start();
    m.record(spe::Outcome::success, 10, 200);
    m.record(spe::Outcome::success, 30, 204);
    m.record(spe::Outcome::http_error, 1000, 503);
    m.record(spe::Outcome::connection_error, 1);
    m.record(spe::Outcome::network_error, 2);
    m.record(spe::Outcome::tls_error, 5, 0, 60);
    m.record(spe::Outcome::timeout, 500);
    m.record(spe::Outcome::response_limit, 3);
    m.record(spe::Outcome::cancelled, 100);
    const auto r = m.finish(2, 4, true);
    require(r.counters.valid() && r.counters.completed == 8 && r.counters.cancelled == 1 && r.counters.not_started == 1 && r.counters.tls_errors == 1, "all outcomes accounted");
    require(r.success_latency_ms.samples == 2 && r.success_latency_ms.mean == 20 &&
        r.completed_latency_ms.samples == 8, "failure exclusion");
    require(r.completed_throughput_rps == 4 && r.successful_throughput_rps == 1, "throughput denominator");
    require(r.http_status_counts[200] == 1 && r.http_status_counts[204] == 1 && r.http_status_counts[503] == 1, "status counters");
    spe::Measurements invalid_m(c);
    invalid([&] { invalid_m.record(spe::Outcome::success, 1); });
    invalid_m.start();
    invalid([&] { invalid_m.finish(1, 1, false); });
    invalid([&] { invalid_m.record(spe::Outcome::success, -1); });
}

void json_and_cancellation() {
    auto c = config(); c.requests = 7;
    auto result = spe::run(c, [] { return true; });
    require(result.counters.valid() && result.counters.started == 0 && result.counters.not_started == 7 &&
        result.interrupted && result.peak_in_flight == 0, "cancellation before first dispatch");
    result.engine_error = "quoted \"text\"\\\n\t\r\x01";
    std::ostringstream out;
    spe::write_json(out, result);
    const auto json = out.str();
    require(json.find("\\\"text\\\"\\\\\\n\\t\\r\\u0001") != std::string::npos, "JSON escaping");
    require(json.find("\"p50\":null") != std::string::npos, "empty distribution is null");
}

void ca_identity_and_privacy() {
    char path[] = "/tmp/spe-ca-unit-XXXXXX";
    const auto fd = ::mkstemp(path);
    require(fd >= 0, "create CA fingerprint fixture");
    struct File { int fd; const char* path; ~File() { ::close(fd); ::unlink(path); } } file{fd, path};
    require(::write(fd, "abc", 3) == 3, "write fingerprint fixture");
    const auto bundle = spe::load_ca_bundle(path);
    require(bundle.bytes == "abc" && bundle.sha256 == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
            "standard SHA-256 test vector");
    auto c = config(); c.url = "https://localhost/"; c.requests = 1; c.ca_file = path;
    invalid([&] { spe::Measurements missing_identity(c); });
    spe::Measurements measurements(c, bundle.sha256);
    measurements.start();
    measurements.record(spe::Outcome::tls_error, 5, 0, 60);
    auto result = measurements.finish(0.1, 1, false);
    std::ostringstream out;
    spe::write_json(out, result);
    require(out.str().find(path) == std::string::npos && out.str().find(bundle.sha256) != std::string::npos,
            "result exports CA identity without private file path");
    require(spe::is_tls_error(35) && spe::is_tls_error(60) && spe::is_tls_error(77) &&
            !spe::is_tls_error(28) && !spe::is_tls_error(18), "TLS classification preserves timeout and body error categories");
}

void header_policy() {
    for (auto name : {"authorization", "x-api-key", "accept", "cookie", "x-custom_123", "x-empty"})
        require(spe::is_normalized_header_name(name), "allowed application header");
    for (auto name : {"", "Authorization", "x invalid", "x:invalid", "x\r\ninjected", "host", "content-length",
                     "transfer-encoding", "connection", "accept-encoding", "user-agent", "proxy-authorization"})
        require(!spe::is_normalized_header_name(name), "unsafe or transport-controlled header rejected");
}
}

int main() {
    int failures = 0;
    const auto test = [&](const char* name, auto function) {
        try { function(); std::cout << "PASS " << name << '\n'; }
        catch (const std::exception& error) { ++failures; std::cerr << "FAIL " << name << ": " << error.what() << '\n'; }
    };
    test("CLI and validation", cli_and_validation);
    test("fixed-rate schedule", fixed_rate_schedule);
    test("weighted arrivals", weighted_arrivals);
    test("percentiles", percentiles);
    test("accounting", accounting);
    test("JSON and pre-dispatch cancellation", json_and_cancellation);
    test("CA fingerprint and result privacy", ca_identity_and_privacy);
    test("request header policy", header_policy);
    return failures == 0 ? 0 : 1;
}
