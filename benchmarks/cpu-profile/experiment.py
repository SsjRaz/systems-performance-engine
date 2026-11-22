"""Run the opt-in Linux quote-service study in a new, bounded evidence directory."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import random
import subprocess
import sys

import fixture
from profile_tools import choose

BUDGET = 640
DELAY_US = 2000
# Number of independent process pairs is fixed before data collection.
CAMPAIGNS = {
    "cpu": (8, "baseline", "candidate"),
    "wait": (8, "baseline", "wait"),
    "reversal": (8, "candidate", "revert"),
    "recovery": (16, "baseline", "revert"),
}


def read(path):
    return json.loads(path.read_text())


def runtime(target):
    return f"delay-us-{DELAY_US if target == 'wait' else 0}"


def binary_target(target):
    return "baseline" if target == "wait" else target


def version_id(build, target):
    return hashlib.sha256(
        f"{build['binary_sha256']}/{runtime(target)}".encode()
    ).hexdigest()


def blocks(name, count, rng, diagnostic=False):
    order = ["baseline", "candidate"] * (count // 2)
    rng.shuffle(order)
    items = []
    for i, first in enumerate(order):
        block = f"{name}-{i + 1:02}"
        rows = []
        for arm in (first, "candidate" if first == "baseline" else "baseline"):
            instance = f"{block}-{arm}"
            if diagnostic:
                rows.append(dict(arm=arm, capture=f"{instance}-capture.json"))
            else:
                rows.append(
                    dict(arm=arm, instance=instance, result=f"{instance}-native.json")
                )
        items.append(dict(id=block, runs=rows))
    return items


def declare(root, inputs, versions, sampler, seed):
    rng = random.Random(seed)
    for name, (count, base, candidate) in CAMPAIGNS.items():
        directory = root / name
        directory.mkdir()

        # A successful reverse patch and independent rebuild must restore this exact ELF.
        def planned(target):
            return version_id(
                versions["baseline" if target == "revert" else binary_target(target)],
                target,
            )

        study = dict(
            schema_version=1,
            experiment_id=name,
            workload_id=inputs["dataset_sha256"],
            baseline_version=planned(base),
            candidate_version=planned(candidate),
            metric="mean_latency_ms",
            meaningful_change_percent=10,
            private_inputs_confirmed=False,
            blocks=blocks(name, count, rng),
        )
        fixture.write_json(directory / "timing.json", study)
        if name in ("cpu", "wait"):
            profile = dict(
                schema_version=1,
                timing_manifest="timing.json",
                dataset_sha256=inputs["dataset_sha256"],
                collector=sampler["kind"],
                frequency_hz=sampler["hz"],
                cpu_floor_us=150,
                cpu_floor_percent=10,
                pairs=blocks(f"{name}-prof", 4, rng, diagnostic=True),
            )
            fixture.write_json(directory / "profiling.json", profile)


def native(engine, mode, manifest, output):
    proc = subprocess.run(
        [engine, mode, manifest, "--json", output],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if proc.returncode:
        raise RuntimeError(
            f"{mode} exited {proc.returncode}: {proc.stdout[-600:]} {proc.stderr[-600:]}"
        )
    return read(output)


def measure(root, engine, port, versions, name, sampler=None):
    directory = root / name
    plan = read(directory / "timing.json")
    _, base, cand = CAMPAIGNS[name]
    if sampler:
        profile = read(directory / "profiling.json")
        pairs = profile["pairs"]
    else:
        pairs = plan["blocks"]
    for pair in pairs:
        for row in pair["runs"]:
            target = base if row["arm"] == "baseline" else cand
            binary = binary_target(target)
            build = versions[binary]
            if version_id(build, target) != plan[row["arm"] + "_version"]:
                raise ValueError("target diverged from the predeclared version")
            instance = f"{pair['id']}-{row['arm']}"
            result = directory / (
                f"{instance}-native.json" if sampler else row["result"]
            )
            recorded = fixture.run_window(
                root,
                engine,
                instance,
                binary,
                build,
                BUDGET,
                port,
                result,
                DELAY_US if target == "wait" else 0,
                sampler,
            )
            if sampler:
                capture = dict(
                    schema_version=1,
                    instance=instance,
                    binary_sha256=build["binary_sha256"],
                    build_id=build["build_id"],
                    runtime_id=runtime(target),
                    dataset_sha256=profile["dataset_sha256"],
                    policy_id=fixture.POLICY,
                    native_result=result.name,
                    native_sha256=fixture.sha(result),
                    collector=sampler["kind"],
                    frequency_hz=sampler["hz"],
                    **recorded,
                )
                fixture.write_json(directory / row["capture"], capture)
            else:
                fixture.write_json(directory / (instance + "-resources.json"), recorded)
    if sampler:
        report = native(
            engine,
            "profile-diff",
            directory / "profiling.json",
            directory / "diagnosis.json",
        )
        print(name, report["reason"], flush=True)
        return report
    report = native(
        engine, "analyze", directory / "timing.json", directory / "decision.json"
    )
    print(
        name,
        report["verdict"],
        report["effect_percent"],
        report["interval_percent"],
        flush=True,
    )
    return report


def run(args):
    root, engine = args.output.resolve(), args.engine.resolve(strict=True)
    if sys.platform != "linux" or len(os.sched_getaffinity(0)) < 2:
        raise RuntimeError("this study requires Linux and two available CPUs")
    if any(os.environ.get(key) for key in ("LD_PRELOAD", "LD_AUDIT")):
        raise RuntimeError(
            "clear application instrumentation from the study environment"
        )
    sampler = choose(args.collector, args.perf, args.library, args.pprof)
    root.mkdir(parents=True, exist_ok=False)
    inputs = fixture.dataset(root)
    fixture.write_json(root / "inputs.json", inputs)
    versions = {
        target: fixture.build(root, target, args.cxx)
        for target in ("baseline", "candidate")
    }
    fixture.write_json(root / "initial-builds.json", versions)
    port = fixture.open_port()
    declare(root, inputs, versions, sampler, args.seed)
    record = dict(
        requests_per_window=BUDGET,
        delay_control_us=DELAY_US,
        order_seed=args.seed,
        sampler={key: sampler[key] for key in ("kind", "hz", "perf_probe", "version")},
        affinity=sorted(os.sched_getaffinity(0))[:2],
        cpu_quota=Path("/sys/fs/cgroup/cpu.max").read_text().strip()
        if Path("/sys/fs/cgroup/cpu.max").is_file()
        else None,
        profiler_library_sha256=fixture.sha(sampler["library"])
        if sampler["kind"] == "gperftools" and Path(sampler["library"]).is_file()
        else None,
        platform=platform.platform(),
        port=port,
        engine_sha256=fixture.sha(engine),
        injection_sha256=fixture.sha(fixture.HERE / "linear-lookup.patch"),
        recovery_rule="reversal interval below -10% and independent recovery interval within [-10%,10%]",
    )
    fixture.write_json(root / "protocol.json", record)
    for name in ("cpu", "wait"):
        timing = measure(root, engine, port, versions, name)
        if timing["verdict"] != "regression":
            raise RuntimeError(
                f"{name}: independent analysis did not confirm regression; evidence retained"
            )
        profile = measure(root, engine, port, versions, name, sampler)
        if name == "cpu":
            if (
                not profile["suspects"]
                or "freight::lookup_rate" not in profile["suspects"][0]["symbol"]
            ):
                raise RuntimeError(
                    "injected function was not the leading sampled suspect; evidence retained"
                )
        elif profile["reason"] != "no_consistent_material_user_cpu_increase":
            raise RuntimeError(
                "wait control did not separate from CPU growth; evidence retained"
            )
    versions["revert"] = fixture.build(root, "revert", args.cxx)
    fixture.write_json(root / "reverted-build.json", versions["revert"])
    if (
        versions["baseline"] != versions["revert"]
        or versions["baseline"]["binary_sha256"]
        == versions["candidate"]["binary_sha256"]
    ):
        raise RuntimeError(
            "controlled reverse patch did not restore the baseline build"
        )
    reversal = measure(root, engine, port, versions, "reversal")
    recovery = measure(root, engine, port, versions, "recovery")
    low, high = recovery["interval_percent"]
    restored = reversal["verdict"] == "improvement" and -10 <= low <= high <= 10
    fixture.write_json(
        root / "recovery-check.json",
        {
            "within_prespecified_band": restored,
            "fresh_baseline_to_revert_interval_percent": [low, high],
            "candidate_to_revert_interval_percent": reversal["interval_percent"],
        },
    )
    if not restored:
        raise RuntimeError("controlled recovery is inconclusive; evidence retained")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--collector", choices=["auto", "perf", "gperftools"], default="auto"
    )
    parser.add_argument("--perf", help="path to Linux perf")
    parser.add_argument("--library", help="gperftools libprofiler shared library")
    parser.add_argument("--pprof", help="gperftools google-pprof text exporter")
    parser.add_argument("--cxx", default="c++")
    parser.add_argument("--seed", type=int, default=94732)
    run(parser.parse_args())
