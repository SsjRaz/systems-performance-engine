#include "spe/profile.hpp"
#include "config_json.hpp"
#include "spe/result.hpp"
#include "spe/tls.hpp"

#include <algorithm>
#include <array>
#include <cmath>
#include <filesystem>
#include <map>
#include <numeric>
#include <set>
#include <stdexcept>
#include <tuple>

namespace spe {
namespace {
using Json = nlohmann::json;
namespace fs = std::filesystem;
using Symbol = std::pair<std::string, std::string>; // public symbol and source file
using Frame = std::tuple<Symbol, unsigned>;
constexpr std::array arms{"baseline", "candidate"};

void require(bool condition, const char* reason) {
    if (!condition) throw std::invalid_argument(reason);
}

void fields(const Json& value, std::initializer_list<const char*> names) {
    require(value.is_object() && value.size() == names.size(), "missing or unknown profile fields");
    for (auto key : names) require(value.contains(key), "missing profile field");
}

unsigned number(const Json& value, unsigned lo, unsigned hi) {
    require(value.is_number_unsigned() && value >= lo && value <= hi, "unsigned profile field outside bounds");
    return value.get<unsigned>();
}

std::string id(const Json& value) {
    auto s = value.get<std::string>();
    require(valid_public_id(s), "invalid profile identifier");
    return s;
}

std::string digest(const Json& value, std::size_t length) {
    auto s = value.get<std::string>();
    require(s.size() == length && s.find_first_not_of("0123456789abcdef") == s.npos, "invalid artifact digest");
    return s;
}

fs::path relative_to(const fs::path& directory, const Json& value) {
    auto s = value.get<std::string>();
    fs::path path(s);
    require(!s.empty() && s.size() <= 1024 && s.find('\0') == s.npos && path.is_relative() &&
            std::find(path.begin(), path.end(), "..") == path.end(), "expected a bounded relative evidence path");
    return directory / path;
}

Json read(const fs::path& path) { return read_config_json(path.string(), ConfigFile::profile); }

Frame frame(const Json& entry, bool allow_symbol_only) {
    require(entry.is_array() && entry.size() == 3, "invalid symbolized frame");
    auto name = entry.at(0).get<std::string>();
    auto file = entry.at(1).get<std::string>();
    auto line = number(entry.at(2), 0, 10000000);
    if (name == "[other]") {
        require(file.empty() && line == 0, "invalid unresolved frame");
    } else {
        require(!name.empty() && name.size() <= 512 && std::all_of(name.begin(), name.end(),
                [](unsigned char c) { return c >= 32 && c < 127; }), "invalid public symbol");
        if (allow_symbol_only && file.empty() && line == 0) return {{name, file}, line};
        require(line > 0 && !file.empty() && file.size() <= 256 &&
                file.find_first_not_of("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_./-") == file.npos,
                "invalid source location");
        relative_to({}, file);
    }
    return {{name, file}, line};
}

struct Self {
    unsigned samples = 0;
    unsigned heaviest_stack = 0;
    Json example;
};
struct Window {
    std::string instance, native, native_sha, elf, build, policy;
    unsigned samples, successes, user, system, wall, peak_rss, voluntary, involuntary, preflights;
    unsigned other = 0, receipts = 0;
    std::map<Symbol, Self> leaves;

    double per_request(unsigned micros) const { return static_cast<double>(micros) / (receipts ? receipts : successes); }
    double estimated_us(const Symbol& key) const {
        const auto it = leaves.find(key);
        return it == leaves.end() ? 0 : per_request(user) * it->second.samples / samples;
    }
};
using Pair = std::array<Window, 2>;

Window window(const fs::path& path, const Json& plan, const Json& timing, std::size_t arm) {
    const auto data = read(path);
    fields(data, {"schema_version","instance","binary_sha256","build_id","runtime_id","dataset_sha256","policy_id",
                  "native_result","native_sha256","collector","frequency_hz","samples","frames","stacks","resource"});
    require(data.at("schema_version") == plan.at("schema_version"), "capture schema differs from plan");
    const bool process_window = number(data.at("schema_version"), 1, 2) == 2;
    require(data.at("dataset_sha256") == plan.at("dataset_sha256") && data.at("collector") == plan.at("collector") &&
            data.at("frequency_hz") == plan.at("frequency_hz"), "capture dataset or collector differs");
    Window w{};
    w.instance = id(data.at("instance"));
    w.elf = digest(data.at("binary_sha256"), 64);
    w.build = digest(data.at("build_id"), 40);
    w.policy = id(data.at("policy_id"));
    const auto version = sha256_hex(w.elf + "/" + id(data.at("runtime_id")));
    require(version == timing.at(arm == 0 ? "baseline_version" : "candidate_version").get<std::string>(), "profiled target differs from timing arm");
    w.native_sha = digest(data.at("native_sha256"), 64);
    w.native = relative_to(path.parent_path(), data.at("native_result")).string();
    const auto& r = data.at("resource");
    if (process_window) {
        fields(r, {"collector","preflights","successes","receipts","user_us","system_us","wall_us","peak_rss_kib",
                   "cpu_capacity","clock_tick_us"});
    } else {
        fields(r, {"collector","preflights","successes","user_us","system_us","wall_us","peak_rss_kib",
                   "voluntary_switches","involuntary_switches"});
    }
    require(r.at("collector") == plan.at("collector"), "resource collector differs");
    w.preflights = number(r.at("preflights"), 1, 1024);
    w.successes = number(r.at("successes"), 1, 1000000);
    w.user = number(r.at("user_us"), 0, 120000000);
    w.system = number(r.at("system_us"), 0, 120000000);
    w.wall = number(r.at("wall_us"), 1, 120000000);
    w.peak_rss = number(r.at("peak_rss_kib"), 1, 1000000000);
    if (process_window) {
        const auto capacity = number(r.at("cpu_capacity"), 1, 1024);
        const auto tick = number(r.at("clock_tick_us"), 1, 100000);
        require(plan.at("cpu_capacity") == capacity && plan.at("clock_tick_us") == tick, "process capacity or clock changed");
        w.receipts = number(r.at("receipts"), 2, 1000001);
        require(w.preflights == 1 && w.receipts == w.successes + w.preflights, "verified search receipts disagree");
        require(w.user % tick == 0 && w.system % tick == 0, "invalid process tick accounting");
        require(w.user + w.system <= std::uint64_t{w.wall} * capacity + 2 * tick, "process CPU exceeds assigned capacity");
    } else {
        require(w.user + w.system <= w.wall + 5000, "single-thread process CPU exceeds wall time");
        w.voluntary = number(r.at("voluntary_switches"), 0, 1000000000);
        w.involuntary = number(r.at("involuntary_switches"), 0, 1000000000);
    }
    w.samples = number(data.at("samples"), 0, 100000);
    std::vector<Frame> frames;
    const auto& table = data.at("frames");
    require(table.is_array() && table.size() <= 512, "invalid frame table");
    for (const auto& entry : table) frames.push_back(frame(entry, process_window));
    const auto& stacks = data.at("stacks");
    require(stacks.is_array() && stacks.size() <= 1024, "invalid stack table");
    unsigned total = 0;
    for (const auto& row : stacks) {
        require(row.is_array() && row.size() == 2, "invalid weighted call stack");
        const auto weight = number(row.at(0), 1, 100000);
        const auto& trace = row.at(1);
        require(trace.is_array() && !trace.empty() && trace.size() <= 32 && !frames.empty(), "invalid call chain");
        for (const auto& index : trace) number(index, 0, static_cast<unsigned>(frames.size() - 1));
        const auto& key = std::get<0>(frames.at(trace.front().get<unsigned>()));
        total += weight;
        require(total <= w.samples, "stack weights exceed declared samples");
        if (key.first == "[other]") w.other += weight;
        auto& self = w.leaves[key];
        self.samples += weight;
        if (weight > self.heaviest_stack) {
            self.heaviest_stack = weight;
            self.example = Json::array();
            for (const auto& index : trace) self.example.push_back(table.at(index.get<unsigned>()));
        }
    }
    require(total == w.samples, "stack weights disagree with declared samples");
    return w;
}

struct Study {
    std::vector<Pair> pairs;
    std::vector<std::pair<std::size_t, std::size_t>> order; // arm, native index in that arm
    CompareOptions inputs;
};

Study collect(const Json& plan, const fs::path& directory, const Json& timing, const fs::path& timing_dir) {
    Study study;
    std::set<std::string> instances, blocks;
    study.inputs.allow_redacted_headers = study.inputs.allow_redacted_body = timing.at("private_inputs_confirmed").get<bool>();
    auto append = [&](std::size_t arm, const std::string& instance, const std::string& path) {
        require(instances.insert(instance).second, "timing and profiling require independent instances");
        auto& paths = arm == 0 ? study.inputs.baseline : study.inputs.candidate;
        study.order.emplace_back(arm, paths.size());
        paths.push_back(path);
    };
    for (const auto& block : timing.at("blocks")) {
        for (const auto& row : block.at("runs"))
            append(row.at("arm") == "baseline" ? 0 : 1, id(row.at("instance")), relative_to(timing_dir,row.at("result")).string());
    }
    const auto& pairs = plan.at("pairs");
    require(pairs.is_array() && pairs.size() >= 2 && pairs.size() <= 8, "expected 2..8 independent profile pairs");
    int balance = 0;
    for (const auto& pair : pairs) {
        fields(pair, {"id","runs"});
        require(blocks.insert(id(pair.at("id"))).second, "duplicate profile pair");
        const auto& runs = pair.at("runs");
        require(runs.is_array() && runs.size() == 2 && runs[0].at("arm") != runs[1].at("arm"), "profile pair requires both arms");
        balance += runs[0].at("arm") == "baseline" ? 1 : -1;
        Pair both{};
        for (const auto& run : runs) {
            fields(run, {"arm","capture"});
            require(run.at("arm") == "baseline" || run.at("arm") == "candidate", "unknown profile arm");
            const std::size_t arm = run.at("arm") == "baseline" ? 0 : 1;
            auto& w = both[arm] = window(relative_to(directory,run.at("capture")), plan, timing, arm);
            append(arm, w.instance, w.native);
            if (!study.pairs.empty())
                require(w.build == study.pairs.front()[arm].build, "ELF build ID changed within arm");
        }
        study.pairs.push_back(std::move(both));
    }
    require(std::abs(balance) <= 1, "unbalanced profile run order");
    return study;
}

void check_native(const Study& study, const Json& comparison, const Json& timing, const Json& decision) {
    require(comparison.at("input_result_schema_version") == 7, "verified native results required");
    const bool process_window = study.pairs.front()[0].receipts != 0;
    if (process_window) require(comparison.at("workload").at("endpoint_mix").is_null(), "process window requires one endpoint");
    std::int64_t previous = 0;
    double elapsed = 0;
    auto primary = timing.at("blocks").size();
    for (auto [arm,index] : study.order) {
        const auto& input = comparison.at(arms[arm]).at("inputs").at(index);
        const auto start = input.at("started_second_utc").get<std::int64_t>();
        require(start >= previous && elapsed <= static_cast<double>(start - previous + 1) + 1e-9, "windows overlap or contradict order");
        previous = start;
        elapsed = input.at("elapsed_seconds").get<double>();
        const auto& verify = input.at("verification");
        require(verify.at("status") == "passed", "native preflight failed");
        if (process_window) require(verify.at("passed") == 1, "process window requires one verified preflight");
        if (index < primary) {
            // Tie the combined native snapshot back to the exact observations used for the timing verdict.
            auto metric = timing.at("metric");
            const double value = metric == "successful_rps" ? input.at("counts").at("successes").get<double>() / elapsed :
                input.at("success_latency_ms").at(metric == "mean_latency_ms" ? "mean" : "p95").get<double>();
            require(value == decision.at("pairs").at(index).at(arms[arm]), "timing evidence changed during analysis");
        } else {
            const auto& w = study.pairs.at(index - primary)[arm];
            require(input.at("file_sha256").get<std::string>() == w.native_sha, "profile refers to different native result bytes");
            require(verify.at("policy_id").get<std::string>() == w.policy && verify.at("passed") == w.preflights &&
                    input.at("counts").at("successes") == w.successes, "target and native accounting differ");
        }
    }
}

Json resources(const std::vector<Pair>& pairs, std::size_t arm) {
    double user = 0, system = 0, wall = 0;
    unsigned samples = 0, other = 0, successes = 0, peak_rss = 0, receipts = 0;
    std::uint64_t voluntary = 0, involuntary = 0;
    for (const auto& pair : pairs) {
        const auto& w = pair[arm];
        user += w.per_request(w.user);
        system += w.per_request(w.system);
        wall += w.per_request(w.wall);
        samples += w.samples; other += w.other; successes += w.successes;
        receipts += w.receipts;
        peak_rss = std::max(peak_rss, w.peak_rss);
        voluntary += w.voluntary; involuntary += w.involuntary;
    }
    auto n = static_cast<double>(pairs.size());
    Json summary = {{"user_us_per_success",user/n},{"system_us_per_success",system/n},{"wall_us_per_success",wall/n},
                    {"samples",samples},{"other_leaf_samples",other},{"successes",successes},{"max_peak_rss_kib",peak_rss}};
    if (receipts) {
        summary["successful_requests_including_preflight"] = receipts;
    } else {
        summary["voluntary_switches"] = voluntary;
        summary["involuntary_switches"] = involuntary;
    }
    return summary;
}

std::string rank(Json& report, const std::vector<Pair>& pairs, unsigned floor_us, unsigned floor_percent) {
    std::set<Symbol> symbols;
    bool enough_cpu = true;
    for (std::size_t arm = 0; arm < arms.size(); ++arm)
        report["resources"][arms[arm]] = resources(pairs, arm);
    for (const auto& pair : pairs) {
        for (const auto& w : pair) {
            if (w.samples < 25 || w.other * 2 > w.samples || !w.user) return "insufficient_symbolized_samples";
        }
        const auto baseline = pair[0].per_request(pair[0].user);
        const auto candidate = pair[1].per_request(pair[1].user);
        if (candidate - baseline <= std::max(static_cast<double>(floor_us), baseline * floor_percent / 100.0)) enough_cpu = false;
        for (const auto& item : pair[1].leaves) if (item.first.first != "[other]") symbols.insert(item.first);
    }
    for (auto name : arms) if (report.at("resources").at(name).at("samples") < 100) return "insufficient_symbolized_samples";
    if (!enough_cpu) return "no_consistent_material_user_cpu_increase";
    std::vector<Json> ranking;
    const auto n = static_cast<double>(pairs.size());
    for (const auto& key : symbols) {
        double base = 0, cand = 0;
        std::vector<double> delta;
        unsigned heaviest = 0, sample_support = 0;
        Json example;
        for (const auto& pair : pairs) {
            const double a = pair[0].estimated_us(key), b = pair[1].estimated_us(key);
            base += a; cand += b;
            delta.push_back(b-a);
            const auto it = pair[1].leaves.find(key);
            if (it != pair[1].leaves.end()) {
                sample_support += it->second.samples;
                if (it->second.heaviest_stack > heaviest) {
                    heaviest = it->second.heaviest_stack;
                    example = it->second.example;
                }
            }
        }
        if (*std::min_element(delta.begin(),delta.end()) <= 0 || sample_support < 10) continue;
        ranking.push_back({{"symbol",key.first},{"source",key.second.empty() ? Json(nullptr) : Json(key.second)},
            {"line",key.second.empty() ? Json(nullptr) : example.at(0).at(2)},
            {"candidate_leaf_samples",sample_support},{"baseline_self_us_per_success",base/n},
            {"candidate_self_us_per_success",cand/n},{"delta_self_us_per_success",(cand-base)/n},
            {"paired_deltas_us_per_success",delta},{"example_leaf_first_stack",example}});
    }
    std::sort(ranking.begin(),ranking.end(),[](const auto& a,const auto& b) {
        return a.at("delta_self_us_per_success") > b.at("delta_self_us_per_success");
    });
    if (ranking.size() > 10) ranking.resize(10);
    if (ranking.empty()) return "no_consistent_symbol_increase";
    report["suspects"] = ranking;
    return "ranked_cpu_suspects";
}
}

std::string_view profile_usage() {
    return "Usage: spe profile-diff MANIFEST.json [--json PATH|-]\n"
           "Compare symbolized application CPU captures after an independent advisory timing regression.\n";
}

ComparisonReport compare_cpu_profiles(const AnalyzeOptions& options) {
    Json report = {{"kind","spe_cpu_profile_comparison"},{"schema_version",1},{"suspects",Json::array()},
                   {"ci_gate",false},{"attribution","sampled hypotheses; validate by intervention"}};
    ComparisonStatus status = ComparisonStatus::insufficient_evidence;
    try {
        const auto plan = read(options.manifest);
        const auto schema = number(plan.at("schema_version"), 1, 2);
        if (schema == 2) {
            fields(plan, {"schema_version","timing_manifest","dataset_sha256","collector","frequency_hz","cpu_floor_us",
                          "cpu_floor_percent","pairs","cpu_capacity","clock_tick_us"});
            report["schema_version"] = schema;
            report["resource_denominator"] = "application-confirmed successful requests including one identical preflight";
            report["cpu_capacity"] = number(plan.at("cpu_capacity"), 1, 1024);
            report["clock_tick_us"] = number(plan.at("clock_tick_us"), 1, 100000);
        } else {
            fields(plan, {"schema_version","timing_manifest","dataset_sha256","collector","frequency_hz","cpu_floor_us",
                          "cpu_floor_percent","pairs"});
        }
        digest(plan.at("dataset_sha256"), 64);
        require(plan.at("collector") == "perf" || plan.at("collector") == "gperftools", "unsupported CPU collector");
        number(plan.at("frequency_hz"), 1, 4000);
        auto floor_us = number(plan.at("cpu_floor_us"), 1, 1000000);
        auto floor_percent = number(plan.at("cpu_floor_percent"), 1, 100);
        const auto directory = fs::path(options.manifest).parent_path();
        const auto timing_path = relative_to(directory,plan.at("timing_manifest"));
        const auto timing = read(timing_path);
        const auto result = analyze_experiment({timing_path.string(), {}, false});
        const auto decision = Json::parse(result.json);
        report["timing_verdict"] = decision.at("verdict");
        if (result.status == ComparisonStatus::rejected) {
            status = result.status;
            report["reason"] = "invalid_primary_timing";
        } else if (decision.at("verdict") != "regression") {
            report["reason"] = "independent_timing_regression_required";
        } else {
            require(decision.at("manifest_sha256").get<std::string>() == sha256_hex(timing.dump()), "timing manifest changed during analysis");
            require(timing.at("workload_id") == plan.at("dataset_sha256"), "timing dataset differs from profiles");
            auto study = collect(plan, directory, timing, timing_path.parent_path());
            const auto combined = compare_saved_results(study.inputs, true);
            if (combined.status == ComparisonStatus::descriptive) {
                check_native(study, Json::parse(combined.json), timing, decision);
                report["collector"] = plan.at("collector");
                report["requested_frequency_hz"] = plan.at("frequency_hz");
                report["profile_pairs"] = study.pairs.size();
                report["primary_manifest_sha256"] = decision.at("manifest_sha256");
                report["reason"] = rank(report, study.pairs, floor_us, floor_percent);
                if (report["reason"] != "insufficient_symbolized_samples") status = ComparisonStatus::descriptive;
            } else {
                status = combined.status;
                report["reason"] = "incompatible_or_incomplete_native_runs";
            }
        }
    } catch (const Json::exception&) {
        status = ComparisonStatus::rejected;
        report["reason"] = "invalid_profile_structure";
    } catch (const std::exception& e) {
        status = ComparisonStatus::rejected;
        report["reason"] = e.what();
    }
    std::string summary = "Differential CPU profile " + report.at("reason").get<std::string>() + "\n";
    for (const auto& row : report.at("suspects")) {
        const auto& source = row.at("source");
        const auto location = source.is_null() ? "ELF symbol only" : source.get<std::string>() + ":" + row.at("line").dump();
        summary += row.at("symbol").get<std::string>() + " (" + location + ") +" +
                   row.at("delta_self_us_per_success").dump() + " estimated user CPU us/success\n";
    }
    return {status, summary, report.dump(2) + "\n"};
}
}
