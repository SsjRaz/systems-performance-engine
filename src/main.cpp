#include "spe/config.hpp"
#include "spe/compare.hpp"
#include "spe/engine.hpp"
#include "spe/result.hpp"

#include <csignal>
#include <cerrno>
#include <fcntl.h>
#include <iostream>
#include <sstream>
#include <stdexcept>
#include <string_view>
#include <system_error>
#include <unistd.h>
#include <vector>

namespace {
volatile std::sig_atomic_t received_signal = 0;
extern "C" void on_signal(int signal) { received_signal = signal; }

class ResultFile {
public:
    explicit ResultFile(const std::string& path) {
        if (path.empty() || path == "-") return;
        descriptor_ = ::open(path.c_str(), O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC, 0666);
        if (descriptor_ < 0) throw std::system_error(errno, std::generic_category(), "cannot create JSON output (use a new path): " + path);
    }
    ~ResultFile() { if (descriptor_ >= 0) ::close(descriptor_); }
    ResultFile(const ResultFile&) = delete;
    ResultFile& operator=(const ResultFile&) = delete;
    bool enabled() const { return descriptor_ >= 0; }
    void write(std::string_view data) {
        std::size_t offset = 0;
        while (offset < data.size()) {
            const auto written = ::write(descriptor_, data.data() + offset, data.size() - offset);
            if (written < 0 && errno == EINTR) continue;
            if (written <= 0) throw std::system_error(written < 0 ? errno : EIO, std::generic_category(), "cannot write JSON output");
            offset += static_cast<std::size_t>(written);
        }
        const auto descriptor = descriptor_;
        descriptor_ = -1;
        if (::close(descriptor) != 0) throw std::system_error(errno, std::generic_category(), "cannot close JSON output");
    }
private:
    int descriptor_ = -1;
};
}

int main(int argc, char** argv) {
    try {
        std::vector<std::string_view> arguments;
        for (int i = 1; i < argc; ++i) arguments.emplace_back(argv[i]);
        if (!arguments.empty() && arguments.front() == "compare") {
            const auto options = spe::parse_compare_cli(std::span<const std::string_view>(arguments).subspan(1));
            if (options.help) { std::cout << spe::compare_usage(); return 0; }
            ResultFile file(options.json_path);
            if (std::signal(SIGPIPE, SIG_IGN) == SIG_ERR) throw std::runtime_error("cannot install output signal handler");
            const auto report = spe::compare_saved_results(options);
            const bool json_stdout = options.json_path == "-";
            auto& summary = json_stdout ? std::cerr : std::cout;
            summary << report.summary;
            if (json_stdout) {
                std::cout << report.json;
                std::cout.flush();
                if (!std::cout) throw std::runtime_error("cannot write comparison JSON");
            } else if (file.enabled()) file.write(report.json);
            summary.flush();
            if (!summary) throw std::runtime_error("cannot write comparison summary");
            return spe::comparison_exit_code(report.status);
        }
        const auto options = spe::parse_cli(arguments);
        if (options.help) { std::cout << spe::usage(); return 0; }
        if (options.version) { std::cout << "Systems Performance Engine " << spe::version() << '\n'; return 0; }

        ResultFile file(options.config.json_path);
        if (std::signal(SIGINT, on_signal) == SIG_ERR || std::signal(SIGTERM, on_signal) == SIG_ERR ||
            std::signal(SIGPIPE, SIG_IGN) == SIG_ERR) {
            throw std::runtime_error("cannot install signal handlers");
        }
        const auto result = spe::run(options.config, [] { return received_signal != 0; });
        const bool json_stdout = options.config.json_path == "-";
        auto& summary_output = json_stdout ? std::cerr : std::cout;
        spe::write_summary(summary_output, result);
        if (json_stdout) {
            spe::write_json(std::cout, result);
            std::cout.flush();
            if (!std::cout) throw std::runtime_error("cannot write JSON to stdout");
        } else if (file.enabled()) {
            std::ostringstream json;
            spe::write_json(json, result);
            file.write(json.str());
        }
        summary_output.flush();
        if (!summary_output) throw std::runtime_error("cannot write summary");
        if (!result.engine_error.empty()) return 1;
        if (received_signal) return 128 + received_signal;
        return result.counters.successes == result.counters.requested ? 0 : 3;
    } catch (const std::invalid_argument& error) {
        std::cerr << "Configuration error: " << error.what() << "\nUse --help for usage.\n";
        return 2;
    } catch (const std::exception& error) {
        std::cerr << "Error: " << error.what() << '\n';
        return 1;
    }
}
