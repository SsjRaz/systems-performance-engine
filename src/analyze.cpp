#include "spe/analyze.hpp"
#include "config_json.hpp"
#include "spe/result.hpp"
#include "spe/tls.hpp"

#include <algorithm>
#include <cmath>
#include <filesystem>
#include <numeric>
#include <limits>
#include <set>
#include <sstream>
#include <stdexcept>

namespace spe {
namespace {
using Json = nlohmann::json;
namespace fs = std::filesystem;

void require(bool ok, const char* message) {
    if (!ok) throw std::invalid_argument(message);
}

void fields(const Json& object, std::initializer_list<const char*> names) {
    require(object.is_object() && object.size() == names.size(), "manifest has missing or unknown fields");
    for (auto name : names) require(object.contains(name), "missing manifest field");
}

std::string identifier(const Json& value) {
    require(value.is_string() && valid_public_id(value.get_ref<const std::string&>()), "invalid public manifest identifier");
    return value.get<std::string>();
}

struct Design {
    CompareOptions comparison;
    std::vector<bool> baseline_first;
};

Design read_design(const Json& doc, const fs::path& directory) {
    fields(doc, {"schema_version", "experiment_id", "workload_id", "baseline_version", "candidate_version",
                 "metric", "meaningful_change_percent", "private_inputs_confirmed", "blocks"});
    require(doc.at("schema_version").is_number_unsigned() && doc.at("schema_version") == 1, "unknown experiment schema");
    for (auto key : {"experiment_id", "workload_id", "baseline_version", "candidate_version"}) identifier(doc.at(key));
    const auto& metric = doc.at("metric");
    require(metric == "mean_latency_ms" || metric == "p95_latency_ms" || metric == "successful_rps", "unsupported primary metric");
    const auto& threshold = doc.at("meaningful_change_percent");
    require(threshold.is_number() && threshold > 0 && threshold < 100, "meaningful threshold must be between 0 and 100 percent");
    require(doc.at("private_inputs_confirmed").is_boolean(), "private_inputs_confirmed must be boolean");
    const auto& blocks = doc.at("blocks");
    require(blocks.is_array() && !blocks.empty() && blocks.size() <= 100, "experiment requires 1..100 paired blocks");
    std::set<std::string> ids, instances;
    Design design;
    design.comparison.allow_redacted_headers = design.comparison.allow_redacted_body = doc.at("private_inputs_confirmed").get<bool>();
    for (const auto& block : blocks) {
        fields(block, {"id", "runs"});
        require(ids.insert(identifier(block.at("id"))).second, "duplicate paired block");
        const auto& runs = block.at("runs");
        require(runs.is_array() && runs.size() == 2, "each paired block requires exactly two server instances");
        const bool first = runs.at(0).at("arm") == "baseline";
        design.baseline_first.push_back(first);
        for (std::size_t i = 0; i < 2; ++i) {
            const auto& run = runs.at(i);
            fields(run, {"arm", "instance", "result"});
            const bool baseline = i == 0 ? first : !first;
            require(run.at("arm") == (baseline ? "baseline" : "candidate"), "each block requires both arms in execution order");
            require(instances.insert(identifier(run.at("instance"))).second, "a server instance occurs more than once");
            const auto& path = run.at("result").get_ref<const std::string&>();
            const fs::path relative(path);
            require(!path.empty() && path.size() <= 1024 && path.find('\0') == path.npos && relative.is_relative() &&
                    std::find(relative.begin(), relative.end(), "..") == relative.end(), "invalid relative result path");
            (baseline ? design.comparison.baseline : design.comparison.candidate).push_back((directory / relative).string());
        }
    }
    return design;
}

double observation(const Json& input, const std::string& metric) {
    if (metric == "successful_rps")
        return input.at("counts").at("successes").get<double>() / input.at("elapsed_seconds").get<double>();
    return input.at("success_latency_ms").at(metric == "mean_latency_ms" ? "mean" : "p95").get<double>();
}

ComparisonReport finish(Json report, ComparisonStatus status, std::string reason) {
    report["reason"] = std::move(reason);
    std::ostringstream summary;
    summary << "Paired analysis " << report.at("verdict").get<std::string>() << " ("
            << report.at("reason").get<std::string>() << ")\n";
    if (report.contains("input_error")) summary << report.at("input_error").get<std::string>() << '\n';
    if (report.contains("meaningful_change_percent"))
        summary << "Predeclared threshold " << report.at("meaningful_change_percent") << "%, "
                << report.value("declared_pairs", 0U) << " declared pairs\n";
    if (!report.at("effect_percent").is_null()) {
        summary << report.at("metric").get<std::string>() << " median paired change " << report.at("effect_percent")
                << "% over " << report.at("independent_pairs") << " declared independent pairs\n";
        if (!report.at("interval_percent").is_null())
            summary << "Exact median interval " << report.at("interval_percent") << "%, coverage at least "
                    << report.at("coverage_lower_bound").get<double>() * 100 << "%\n";
    }
    summary << "Advisory decision. Independence and predeclaration rely on the experiment record.\n";
    return {status, summary.str(), report.dump(2) + "\n"};
}

// Invert the two-sided sign test. For sorted independent paired effects the
// endpoints x[k], x[n-1-k] cover a population median with probability at least
// 1 - 2 * P(Binomial(n, 1/2) <= k). Ties make this interval conservative.
void estimate(Json& report, std::vector<double> changes, double threshold, bool higher_is_better) {
    std::sort(changes.begin(), changes.end());
    const auto n = changes.size();
    const auto middle = n / 2;
    report["effect_percent"] = n % 2 ? changes[middle] : std::midpoint(changes[middle - 1], changes[middle]);
    double probability = std::ldexp(1.0, -static_cast<int>(n));
    double tail = 0;
    for (std::size_t k = 0; k < (n + 1) / 2; ++k) {
        tail += probability;
        const double coverage = 1 - 2 * tail;
        if (coverage < 0.95) break;
        const double low = changes[k], high = changes[n - 1 - k];
        report["interval_percent"] = {low, high};
        report["coverage_lower_bound"] = coverage;
        const double boundary = threshold + 64 * std::numeric_limits<double>::epsilon() * 100;
        if (low > boundary) report["verdict"] = higher_is_better ? "improvement" : "regression";
        else if (high < -boundary) report["verdict"] = higher_is_better ? "regression" : "improvement";
        else report["verdict"] = "inconclusive";
        probability *= static_cast<double>(n - k) / static_cast<double>(k + 1);
    }
}
}

AnalyzeOptions parse_analyze_cli(std::span<const std::string_view> arguments) {
    AnalyzeOptions options;
    for (std::size_t i = 0; i < arguments.size(); ++i) {
        const auto value = arguments[i];
        if (value == "--help" || value == "-h") options.help = true;
        else if (value == "--json") {
            require(options.json_path.empty() && ++i < arguments.size(), "expected one --json output");
            const auto path = arguments[i];
            require(!path.empty() && (path == "-" || !path.starts_with('-')), "invalid --json output");
            options.json_path = path;
        } else {
            require(options.manifest.empty() && !value.empty() && !value.starts_with('-'), "expected one experiment manifest");
            options.manifest = value;
        }
    }
    require(options.help || !options.manifest.empty(), "missing paired experiment manifest");
    return options;
}

std::string_view analyze_usage() {
    return "Usage: spe analyze MANIFEST.json [--json PATH|-]\n"
           "Advisory paired analysis of verified, complete runs. See docs/REGRESSION.md.\n";
}

ComparisonReport analyze_experiment(const AnalyzeOptions& options) {
    Json report = {{"kind","spe_paired_analysis"},{"schema_version",1},{"verdict","inconclusive"},{"ci_gate",false},
        {"method","exact_sign_interval_for_median_paired_percent_change"},{"target_coverage",0.95},
        {"effect_percent",nullptr},{"interval_percent",nullptr},{"coverage_lower_bound",nullptr},{"independent_pairs",0},
        {"pairs",Json::array()},{"limitations",{"Independence, application identity, host conditions and predeclaration are operator attestations.",
        "One prespecified metric and one fixed sample size. Repeated looks or multiple metrics change error rates.",
        "Preflight verifies separate requests. An inconclusive result leaves material effects unresolved."}}};
    try {
        const auto manifest = read_config_json(options.manifest, ConfigFile::experiment);
        const auto design = read_design(manifest, fs::path(options.manifest).parent_path());
        for (auto key : {"experiment_id","workload_id","baseline_version","candidate_version","metric","meaningful_change_percent"})
            report[key] = manifest.at(key);
        report["manifest_sha256"] = sha256_hex(manifest.dump());
        report["declared_pairs"] = design.baseline_first.size();
        // The descriptive reader validates and snapshots each run once. Consume
        // that snapshot so paths cannot change between eligibility and estimation.
        const auto comparison = compare_saved_results(design.comparison, true);
        const auto evidence = Json::parse(comparison.json);
        if (comparison.status == ComparisonStatus::rejected) {
            report["input_error"] = evidence.value("error", "native comparison rejected inputs");
            return finish(std::move(report), ComparisonStatus::rejected, "incompatible_or_invalid_inputs");
        }
        if (comparison.status != ComparisonStatus::descriptive) {
            auto& failures = report["input_states"] = Json::object();
            for (auto side : {"baseline", "candidate"}) {
                for (const auto& input : evidence.at(side).at("inputs")) {
                    auto state = input.at("state").get<std::string>();
                    if (state == "verification_failed") state += "/" + input.at("verification").at("failure").get<std::string>();
                    failures[state] = failures.value(state, 0U) + 1;
                }
            }
            return finish(std::move(report), ComparisonStatus::insufficient_evidence, "failed_or_incomplete_runs");
        }
        if (evidence.at("input_result_schema_version") != 7)
            return finish(std::move(report), ComparisonStatus::insufficient_evidence, "missing_native_preflight");
        const auto metric = manifest.at("metric").get<std::string>();
        const auto& left = evidence.at("baseline").at("inputs");
        const auto& right = evidence.at("candidate").at("inputs");
        std::string previous;
        std::vector<double> changes;
        for (std::size_t i = 0; i < left.size(); ++i) {
            const auto& a = left.at(i);
            const auto& b = right.at(i);
            require(a.at("verification").at("status") == "passed" && b.at("verification").at("status") == "passed",
                    "missing successful preflight");
            const auto& first = (design.baseline_first[i] ? a : b).at("started_at_utc").get_ref<const std::string&>();
            const auto& last = (design.baseline_first[i] ? b : a).at("started_at_utc").get_ref<const std::string&>();
            require(previous <= first && first <= last, "run timestamps contradict manifest order");
            previous = last;
            const double baseline = observation(a, metric), candidate = observation(b, metric);
            if (baseline <= 0 || !std::isfinite(baseline) || !std::isfinite(candidate))
                return finish(std::move(report), ComparisonStatus::insufficient_evidence, "unusable_primary_metric");
            const double change = (candidate - baseline) / baseline * 100;
            if (!std::isfinite(change))
                return finish(std::move(report), ComparisonStatus::insufficient_evidence, "unusable_primary_metric");
            changes.push_back(change);
            report["pairs"].push_back({{"id",manifest.at("blocks").at(i).at("id")},{"baseline",baseline},
                                       {"candidate",candidate},{"change_percent",change}});
        }
        report["independent_pairs"] = changes.size();
        const auto first_count = std::count(design.baseline_first.begin(), design.baseline_first.end(), true);
        if (std::abs(2 * first_count - static_cast<std::ptrdiff_t>(changes.size())) > 1)
            return finish(std::move(report), ComparisonStatus::insufficient_evidence, "unbalanced_run_order");
        estimate(report, changes, manifest.at("meaningful_change_percent").get<double>(), metric == "successful_rps");
        if (report.at("interval_percent").is_null())
            return finish(std::move(report), ComparisonStatus::insufficient_evidence, "too_few_independent_pairs");
        const bool unresolved = report.at("verdict") == "inconclusive";
        return finish(std::move(report), ComparisonStatus::descriptive,
                      unresolved ? "interval_overlaps_meaningful_change_band" : "interval_beyond_meaningful_change_band");
    } catch (const Json::exception&) {
        return finish(std::move(report), ComparisonStatus::rejected, "invalid_manifest_or_evidence_structure");
    } catch (const std::exception& e) {
        report["input_error"] = e.what();
        return finish(std::move(report), ComparisonStatus::rejected, "invalid_experiment");
    }
}
}
