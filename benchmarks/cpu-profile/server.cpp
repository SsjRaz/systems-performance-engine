#include "catalog.hpp"

#include <arpa/inet.h>
#include <dlfcn.h>
#include <sys/resource.h>
#include <sys/socket.h>
#include <unistd.h>

#include <algorithm>
#include <array>
#include <charconv>
#include <chrono>
#include <cstdlib>
#include <fstream>
#include <iostream>
#include <stdexcept>
#include <string>
#include <thread>

namespace {
using Clock = std::chrono::steady_clock;
void require(bool ok, const char* message) {
    if (!ok) throw std::runtime_error(message);
}

unsigned number(std::string_view s, unsigned lo, unsigned hi) {
    unsigned value = 0;
    const auto [end, error] = std::from_chars(s.data(), s.data() + s.size(), value);
    require(error == std::errc{} && end == s.data() + s.size() && value >= lo && value <= hi, "invalid fixture argument");
    return value;
}

struct Socket {
    int fd;
    explicit Socket(int value) : fd(value) { require(fd >= 0, "socket unavailable"); }
    ~Socket() { close(fd); }
    Socket(const Socket&) = delete;
    Socket& operator=(const Socket&) = delete;
};

class Collector {
public:
    Collector() {
        path_ = std::getenv("SPE_FIXTURE_GPERF_OUT");
        const auto* library = std::getenv("SPE_FIXTURE_GPERF_LIBRARY");
        external_ = std::getenv("SPE_FIXTURE_PERF_CONTROL") != nullptr;
        require((path_ == nullptr) == (library == nullptr) && !(path_ && external_), "inconsistent collector");
        if (path_) {
            handle_ = dlopen(library, RTLD_NOW | RTLD_LOCAL);
            require(handle_ != nullptr, "cannot load libprofiler");
            begin_ = reinterpret_cast<int(*)(const char*)>(dlsym(handle_, "ProfilerStart"));
            end_ = reinterpret_cast<void(*)()>(dlsym(handle_, "ProfilerStop"));
            require(begin_ && end_, "incomplete libprofiler API");
        }
    }
    void start() {
        if (external_) barrier("WINDOW");
        if (path_) require(begin_(path_) != 0, "ProfilerStart failed");
    }
    void stop() {
        if (path_) end_();
        if (external_) barrier("FINISHED");
    }
    const char* name() const { return external_ ? "perf" : path_ ? "gperftools" : "none"; }
private:
    static void barrier(const char* marker) {
        std::cout << marker << std::endl;
        std::string response;
        require(std::getline(std::cin, response) && response == "continue", "profiler controller disconnected");
    }
    // libprofiler installs process-level handlers. Leave it loaded until exit.
    void* handle_ = nullptr;
    const char* path_ = nullptr;
    int (*begin_)(const char*) = nullptr;
    void (*end_)() = nullptr;
    bool external_ = false;
};

long long micros(timeval t) { return t.tv_sec * 1000000LL + t.tv_usec; }
rusage usage() {
    rusage r{};
    require(getrusage(RUSAGE_SELF, &r) == 0, "getrusage failed");
    return r;
}

void serve_one(int listener, const std::vector<freight::Parcel>& parcels, const freight::Rates& rates, unsigned delay) {
    int descriptor;
    do { descriptor = accept4(listener, nullptr, nullptr, SOCK_CLOEXEC); } while (descriptor < 0 && errno == EINTR);
    Socket client(descriptor);
    std::string request;
    std::array<char, 1024> chunk{};
    while (request.find("\r\n\r\n") == request.npos) {
        auto n = recv(client.fd, chunk.data(), chunk.size(), 0);
        if (n < 0 && errno == EINTR) continue;
        require(n > 0 && request.size() + static_cast<std::size_t>(n) <= 8192, "invalid bounded HTTP request");
        request.append(chunk.data(), static_cast<std::size_t>(n));
    }
    require(request.starts_with("GET /v1/quote HTTP/1.1\r\n"), "unexpected request");
    const auto cents = freight::quote(parcels, rates);
    if (delay) std::this_thread::sleep_for(std::chrono::microseconds(delay));
    const auto body = "{\"parcels\":" + std::to_string(parcels.size()) + ",\"cents\":" + std::to_string(cents) + "}\n";
    const auto reply = "HTTP/1.1 200 OK\r\nConnection: close\r\nContent-Type: application/json\r\nContent-Length: " +
                       std::to_string(body.size()) + "\r\n\r\n" + body;
    for (std::size_t sent = 0; sent < reply.size();) {
        auto n = send(client.fd, reply.data() + sent, reply.size() - sent, MSG_NOSIGNAL);
        if (n < 0 && errno == EINTR) continue;
        require(n > 0, "incomplete HTTP reply");
        sent += static_cast<std::size_t>(n);
    }
}
}

int main(int argc, char** argv) {
    try {
        require(argc == 5, "usage: quote-server DATA PORT REQUESTS DELAY_US");
        alarm(60);
        const auto port = number(argv[2], 1, 65535);
        const auto count = number(argv[3], 1, 10000);
        const auto delay = number(argv[4], 0, 20000);
        freight::Rates rates{};
        for (std::uint32_t i = 0; i < rates.size(); ++i) rates[i] = {i, 70U + (i * 103U) % 911U};
        std::vector<freight::Parcel> parcels;
        std::ifstream input(argv[1]);
        require(input.good(), "dataset unavailable");
        while (input >> std::ws && !input.eof()) {
            freight::Parcel p{};
            require((input >> p.zone >> p.weight >> p.units) && p.zone < rates.size() &&
                    p.weight < 20000 && p.units >= 1 && p.units <= 9 && parcels.size() < 65536, "invalid parcel");
            parcels.push_back(p);
        }
        require(!parcels.empty(), "empty catalog");
        Collector collector;
        Socket server(socket(AF_INET, SOCK_STREAM | SOCK_CLOEXEC, 0));
        int reuse = 1;
        require(setsockopt(server.fd, SOL_SOCKET, SO_REUSEADDR, &reuse, sizeof(reuse)) == 0, "socket setup failed");
        sockaddr_in addr{};
        addr.sin_family = AF_INET;
        addr.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
        addr.sin_port = htons(static_cast<std::uint16_t>(port));
        require(bind(server.fd, reinterpret_cast<sockaddr*>(&addr), sizeof(addr)) == 0 && listen(server.fd, 4) == 0, "listen failed");
        std::cout << "READY" << std::endl;
        serve_one(server.fd, parcels, rates, delay);  // SPE's separate, content-verified preflight.
        collector.start();
        const auto before = usage();
        const auto start = Clock::now();
        for (unsigned n = 0; n < count; ++n) serve_one(server.fd, parcels, rates, delay);
        const auto elapsed = std::chrono::duration_cast<std::chrono::microseconds>(Clock::now() - start).count();
        const auto after = usage();
        collector.stop();
        std::cout << "{\"collector\":\"" << collector.name() << "\",\"preflights\":1,\"successes\":" << count
                  << ",\"user_us\":" << micros(after.ru_utime) - micros(before.ru_utime)
                  << ",\"system_us\":" << micros(after.ru_stime) - micros(before.ru_stime)
                  << ",\"wall_us\":" << elapsed << ",\"peak_rss_kib\":" << after.ru_maxrss
                  << ",\"voluntary_switches\":" << after.ru_nvcsw - before.ru_nvcsw
                  << ",\"involuntary_switches\":" << after.ru_nivcsw - before.ru_nivcsw << "}\n";
    } catch (const std::exception& error) {
        std::cerr << error.what() << '\n';
        return 1;
    }
}
