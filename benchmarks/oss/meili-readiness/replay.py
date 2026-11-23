"""Recompute the native decisions, semantic comparisons and paired summaries from saved evidence."""

import argparse
import json
from collections import Counter
from math import comb
from pathlib import Path
import re
from statistics import median
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from benchlib import digest
from meili_profile import ARMS, REQUESTS, read, target_id
from meili_replay import replay
from meili_samples import compact
from meilisearch_bench import RELEASES
from validation import N, SEARCHES, catalog_cases, documents, INDEXES, signature


def paired(values):
    baseline, candidate = zip(*values)
    effects = sorted(100 * (b / a - 1) for a, b in values)
    n = len(effects)
    result = dict(
        baseline=median(baseline), candidate=median(candidate), change=median(effects)
    )
    for k in range((n + 1) // 2):
        coverage = 1 - 2 * sum(comb(n, j) for j in range(k + 1)) / (2**n)
        if coverage < 0.95:
            break
        result.update(interval=[effects[k], effects[-1 - k]], coverage=coverage)
    return result


def verify_native(doc, count):
    if (
        doc["counts"]["successes"] != count
        or doc["counts"]["started"] != count
        or doc["counts"]["completed"] != count
        or doc["verification"]["status"] != "passed"
        or doc["verification"]["passed"] != 1
        or doc["interrupted"]
        or doc["engine_error"]
    ):
        raise ValueError("native request or verification totals differ")


def primary(root, engine, seen, binary_map, matched):
    plan = read(root / "protocol.json")
    timing = read(root / "timing.json")
    if timing["workload_id"] != plan["dataset_sha256"] or any(
        timing[a + "_version"] != target_id(plan["builds"][a]) for a in ARMS
    ):
        raise ValueError("timing and build identities differ")
    decision = replay(engine, root, "analyze", "timing.json", "decision.json")
    resources = {"user_ms_per_search": [], "peak_rss_mib": []}
    checked = set()

    def window(instance):
        if instance in seen:
            raise ValueError("reused server window")
        seen.add(instance)
        checked.add(instance)
        doc = read(root / (instance + "-native.json"))
        verify_native(doc, REQUESTS)
        resource = read(root / (instance + "-resources.json"))
        if (
            resource["successes"] != REQUESTS
            or resource["preflights"] != 1
            or resource["receipts"] != REQUESTS + 1
        ):
            raise ValueError("application receipts differ from native totals")
        return resource

    for block in timing["blocks"]:
        rows = {run["arm"]: window(run["instance"]) for run in block["runs"]}
        assert all(rows[a]["collector"] == "none" for a in ARMS)
        resources["user_ms_per_search"].append(
            [rows[a]["user_us"] / rows[a]["receipts"] / 1000 for a in ARMS]
        )
        resources["peak_rss_mib"].append([rows[a]["peak_rss_kib"] / 1024 for a in ARMS])
    result = dict(
        pairs=len(timing["blocks"]),
        verdict=decision["verdict"],
        change=decision["effect_percent"],
        interval=decision["interval_percent"],
        coverage=decision["coverage_lower_bound"],
        arm_mean_ms=[median(r[a] for r in decision["pairs"]) for a in ARMS],
        resources={k: paired(v) for k, v in resources.items()},
    )
    if (root / "profile.json").is_file():
        ranked = replay(engine, root, "profile-diff", "profile.json", "ranking.json")
        samples = windows = resolved = 0
        tracked = (
            "mdb_node_search",
            "mdb_cursor_set",
            "mdb_get",
            "filter_parser::Token::into_owned",
            "filter_parser::IndexFilterCondition::into_owned",
        )
        leaf_counts = {arm: Counter() for arm in ARMS}
        arm_samples = {arm: 0 for arm in ARMS}
        for pair in read(root / "profile.json")["pairs"]:
            for run in pair["runs"]:
                cap = read(root / run["capture"])
                pcs = read(root / (cap["instance"] + "-pcs.json"))
                resource = window(cap["instance"])
                if (
                    pcs["binary_sha256"] != cap["binary_sha256"]
                    or pcs["interrupts"] != cap["samples"]
                    or cap["samples"] != sum(weight for weight, _ in pcs["stacks"])
                    or cap["resource"] != resource
                ):
                    raise ValueError("portable samples and capture disagree")
                if binary := binary_map.get(cap["binary_sha256"]):
                    matched.add(cap["binary_sha256"])
                    frames, stacks = compact(pcs["stacks"], binary)
                    assert [list(f) for f in frames] == cap["frames"] and stacks == cap[
                        "stacks"
                    ]
                    resolved += 1
                for weight, frames in cap["stacks"]:
                    symbol = cap["frames"][frames[0]][0]
                    if symbol in tracked:
                        leaf_counts[run["arm"]][symbol] += weight
                arm_samples[run["arm"]] += cap["samples"]
                samples += cap["samples"]
                windows += 1
        result["profile"] = dict(
            windows=windows,
            samples=samples,
            resymbolized=resolved,
            attribution=ranked["attribution"],
            selected_leaf_counts={
                a: {symbol: leaf_counts[a][symbol] for symbol in tracked} for a in ARMS
            },
            samples_by_arm=arm_samples,
            reason=ranked["reason"],
            suspects=[
                {
                    k: r[k]
                    for k in (
                        "symbol",
                        "delta_self_us_per_success",
                        "paired_deltas_us_per_success",
                    )
                }
                for r in ranked["suspects"][:6]
            ],
        )
    if checked != {
        p.name.removesuffix("-resources.json") for p in root.glob("*-resources.json")
    }:
        raise ValueError("unexpected resource files")
    return result, plan


def seconds(text):
    m = re.fullmatch(r"(\d+(?:\.\d+)?)(ns|µs|μs|us|ms|s)", text)
    if not m:
        raise ValueError(text)
    return (
        float(m[1])
        * {"ns": 1e-9, "µs": 1e-6, "μs": 1e-6, "us": 1e-6, "ms": 1e-3, "s": 1}[m[2]]
    )


def secondary(root, expected, seen, same_release):
    plan = read(root / "plan.json")
    if plan["builds"] != expected["builds"] or any(
        plan["inputs"][k] != expected[k] for k in plan["inputs"]
    ):
        raise ValueError("secondary study uses different binaries or data")
    if plan["corpus_sha256"] != signature({i: documents(i) for i in INDEXES}):
        raise ValueError("generated indexing corpus changed")
    phase = plan["phase"]
    values, semantics, stage_names = {}, {}, {a: set() for a in ARMS}
    groups = []
    for block in plan["pairs"]:
        group = {}
        for run in block["runs"]:
            name, arm = run["instance"], run["arm"]
            if name in seen:
                raise ValueError("reused server window")
            seen.add(name)
            row = read(root / (name + ".json"))
            if phase == "index":
                if len(row["tasks"]) != 2 or any(
                    t["documents"] != N for t in row["tasks"]
                ):
                    raise ValueError("index task receipts differ")
                group[arm] = {
                    k: row[k] for k in ("task_ms", "wall_ms", "peak_rss_kib", "cpu_ms")
                }
                if "semantics" in row:
                    semantics[arm] = row["semantics"]
            elif phase == "catalog":
                if row.keys() != catalog_cases().keys():
                    raise ValueError("incomplete workload set")
                group[arm] = {}
                for case, item in row.items():
                    if item["native"] != f"{name}-{case}-native.json":
                        raise ValueError("native identity differs")
                    doc = read(root / item["native"])
                    verify_native(doc, SEARCHES)
                    if (
                        item["receipts"] != SEARCHES + 1
                        or item["mean_ms"] != doc["success_latency_ms"]["mean"]
                    ):
                        raise ValueError(
                            "secondary timing or application receipt mismatch"
                        )
                    group[arm][case] = item["mean_ms"]
                    semantics.setdefault(arm, {}).setdefault(case, set()).add(
                        item["reply"]
                    )
            else:
                if (
                    len(row) != 8
                    or not row
                    or any(r.keys() != row[0].keys() for r in row)
                ):
                    raise ValueError("inconsistent stage data")
                stage_names[arm].update(row[0])
                group[arm] = {
                    s: 1000 * median(seconds(r[s]) for r in row) for s in row[0]
                }
                # Parent durations include their children. Only subtract direct
                # children from the same request. Values are rounded by Meili.
                children = [
                    s
                    for s in row[0]
                    if s.startswith("search > ") and s.count(" > ") == 1
                ]
                group[arm]["unlabelled within search (derived)"] = 1000 * median(
                    seconds(r["search"]) - sum(seconds(r[s]) for s in children)
                    for r in row
                )
        groups.append(group)
    common = set.intersection(*(set(g[a]) for g in groups for a in ARMS))
    for key in sorted(common):
        pairs = [[g[a][key] for a in ARMS] for g in groups]
        if not min(x for p in pairs for x in p) > 0:
            values[key] = {"arm_medians": [median(x[i] for x in pairs) for i in (0, 1)]}
        else:
            values[key] = paired(pairs)
    result = dict(pairs=len(groups), values=values)
    if phase == "stages":
        result["release_specific_stage_ms"] = {
            a: {
                s: median(g[a][s] for g in groups)
                for s in sorted(stage_names[a] - common)
            }
            for a in ARMS
        }
    else:
        assert (
            semantics.keys() == set(ARMS)
            and semantics["baseline"].keys() == semantics["candidate"].keys()
        )
        if phase == "catalog":
            assert all(
                len(v) == 1 for by_arm in semantics.values() for v in by_arm.values()
            )
        changes = [
            k
            for k in semantics["baseline"]
            if semantics["baseline"][k] != semantics["candidate"][k]
        ]
        if same_release and changes:
            raise ValueError(f"changed replies in {root.name}: {changes}")
        result.update(
            checked_replies=len(semantics["baseline"]), changed_replies=changes
        )
    return result


def audit(root, engine, configs, binaries):
    seen, results, matched = set(), {}, set()
    common_inputs = None
    for config in configs:
        main, plans, controls = {}, {}, {}
        for name in ("aa", "patch", "residual"):
            main[name], plans[name] = primary(
                root / (config + "-" + name), engine, seen, binaries, matched
            )
            inputs = {
                k: plans[name][k]
                for k in (
                    "engine_sha256",
                    "dataset_sha256",
                    "body_sha256",
                    "header_sha256",
                    "assigned_cpus",
                    "requests",
                )
            }
            if common_inputs is None:
                common_inputs = inputs
            if inputs != common_inputs or inputs["requests"] != REQUESTS:
                raise ValueError("workload, measurement engine or CPU assignment drift")
        aa, patch, residual = (plans[n]["builds"] for n in ("aa", "patch", "residual"))
        assert aa["baseline"] == aa["candidate"] == patch["baseline"]
        assert patch["candidate"] == residual["candidate"]
        for phase in ("index", "catalog", "stages"):
            for name in ("aa", "patch", "residual"):
                if phase == "stages" and name != "residual":
                    continue
                key = phase + "-" + name
                controls[key] = secondary(
                    root / (config + "-" + key), plans[name], seen, name != "residual"
                )
        results[config] = dict(primary=main, controls=controls)
    if matched != set(binaries):
        raise ValueError("supplied ELF did not match a capture")
    audit = dict(configurations=results, distinct_processes=len(seen))
    if "default" in configs and (root / "official-semantics").exists():
        official = {}
        for version, sha in RELEASES.values():
            reference = read(root / "official-semantics" / (version + ".json"))
            assert reference["version"] == version and reference["binary_sha256"] == sha
            key, arm = (
                ("residual", "baseline")
                if version == "1.41.0"
                else ("patch", "candidate")
            )
            group = root / ("default-index-" + key)
            first = read(group / "plan.json")["pairs"][0]["runs"]
            row = next(r for r in first if r["arm"] == arm)
            actual = read(group / (row["instance"] + ".json"))["semantics"]
            assert reference["checks"] == actual
            official[version] = len(actual)
        audit["official_release_semantic_matches"] = official
    return audit


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("root", type=Path)
    p.add_argument("--engine", required=True, type=Path)
    p.add_argument("--configuration", action="append", choices=["portable", "default"])
    p.add_argument("--elf", action="append", type=Path, default=[])
    args = p.parse_args()
    if not __debug__:
        p.error("use Python with assertions enabled")
    mapping = {digest(b): b.resolve(strict=True) for b in args.elf}
    print(
        json.dumps(
            audit(
                args.root,
                args.engine.resolve(strict=True),
                args.configuration or ["portable", "default"],
                mapping,
            ),
            indent=2,
            allow_nan=False,
        )
    )
