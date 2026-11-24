#include "spe/config.hpp"
#include "spe/compare.hpp"
#include "spe/analyze.hpp"
#include "spe/profile.hpp"
#include "spe/engine.hpp"
#include "spe/result.hpp"
#include "spe/study_script.hpp"

#include <csignal>
#include <cerrno>
#include <fcntl.h>
#include <iostream>
#include <sstream>
#include <stdexcept>
#include <string_view>
#include <system_error>
#ifdef __linux__
#include <sys/prctl.h>
#endif
#include <unistd.h>
#include <vector>

namespace {
volatile std::sig_atomic_t received_signal = 0;
extern "C" void on_signal(int signal) { received_signal = signal; }

[[noreturn]] void start_study(int argc, char** argv) {
#ifdef __linux__
    // Adopt orphaned build/server workers so the driver can reap its own groups
    // even when the container's PID 1 does not run a child reaper.
    if (::prctl(PR_SET_CHILD_SUBREAPER, 1L, 0L, 0L, 0L) != 0)
        throw std::system_error(errno, std::generic_category(), "cannot supervise study descendants");
#endif
    std::vector<const char*> command{"python3", "-I", "-c", spe::study_script, argv[0]};
    command.insert(command.end(), argv + 2, argv + argc);
    command.push_back(nullptr);
    // POSIX exec leaves the argument strings untouched.
    ::execvp(command.front(), const_cast<char* const*>(command.data()));
    throw std::system_error(errno, std::generic_category(), "spe study requires python3 on PATH");
}

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

template<class Options, class Evaluate>
int report_command(const Options& options, std::string_view usage, Evaluate evaluate) {
    if (options.help) {
        std::cout << usage;
        return 0;
    }
    const auto& json_path = options.json_path;
    ResultFile file(json_path);
    if (std::signal(SIGPIPE, SIG_IGN) == SIG_ERR) throw std::runtime_error("cannot install output signal handler");
    const auto report = evaluate(options);
    const bool json_stdout = json_path == "-";
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
}

int main(int argc, char** argv) {
    try {
        std::vector<std::string_view> arguments;
        for (int i = 1; i < argc; ++i) arguments.emplace_back(argv[i]);
        if (!arguments.empty()) {
            const auto command = arguments.front();
            const auto args = std::span<const std::string_view>(arguments).subspan(1);
            if (command == "study") start_study(argc, argv);
            if (command == "compare") {
                return report_command(spe::parse_compare_cli(args), spe::compare_usage(),
                    [](const auto& options) { return spe::compare_saved_results(options); });
            }
            if (command == "analyze")
                return report_command(spe::parse_analyze_cli(args), spe::analyze_usage(), spe::analyze_experiment);
            if (command == "profile-diff")
                return report_command(spe::parse_analyze_cli(args), spe::profile_usage(), spe::compare_cpu_profiles);
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
