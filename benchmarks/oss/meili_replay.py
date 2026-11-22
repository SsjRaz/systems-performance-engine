"""Replay the four Meilisearch comparisons and optionally resolve their sampled PCs again."""

import argparse
import json
from pathlib import Path
import statistics
import subprocess

from benchlib import digest
from meili_profile import ARMS, REQUESTS, RUNTIME, read, target_id
from meili_samples import compact


def replay(engine, study, mode, manifest, saved):
    proc = subprocess.run(
        [engine, mode, study / manifest, "--json", "-"],
        capture_output=True,
        text=True,
        timeout=45,
    )
    observed = json.loads(proc.stdout)
    if proc.returncode not in (0, 3) or observed != read(study / saved):
        raise ValueError(f"{study.name}/{saved} did not replay")
    return observed


def audit(root, engine, elfs):
    summaries, protocols = {}, {}
    windows, native_hashes, matched_elfs = set(), set(), set()
    resolved = reports = successes = probes = total_samples = 0
    for name in ("release", "source", "intervention", "residual"):
        study = root / name
        protocol = protocols[name] = read(study / "protocol.json")
        design = read(study / "timing.json")
        if (
            protocol["runtime_id"] != RUNTIME
            or design["workload_id"] != protocol["dataset_sha256"]
            or any(
                design[a + "_version"] != target_id(protocol["builds"][a]) for a in ARMS
            )
        ):
            raise ValueError("study identity differs from native timing manifest")
        decision = replay(engine, study, "analyze", "timing.json", "decision.json")
        reports += 1
        registered = {}
        cpu = {a: [] for a in ARMS}
        for block in design["blocks"]:
            for row in block["runs"]:
                registered[row["instance"]] = None
                r = read(study / (row["instance"] + "-resources.json"))
                cpu[row["arm"]].append(r["user_us"] / r["receipts"] / 1000)
        summaries[name] = dict(
            pairs=decision["independent_pairs"],
            verdict=decision["verdict"],
            paired_change_percent=decision["effect_percent"],
            interval_percent=decision["interval_percent"],
            median_mean_latency_ms={
                a: statistics.median(row[a] for row in decision["pairs"]) for a in ARMS
            },
            median_user_cpu_ms_per_receipt={a: statistics.median(cpu[a]) for a in ARMS},
        )
        if (study / "profile.json").exists():
            replay(engine, study, "profile-diff", "profile.json", "diagnosis.json")
            reports += 1
            for pair in read(study / "profile.json")["pairs"]:
                for row in pair["runs"]:
                    cap = read(study / row["capture"])
                    registered[cap["instance"]] = cap["resource"]
                    portable = read(study / (cap["instance"] + "-pcs.json"))
                    if (
                        portable["binary_sha256"] != cap["binary_sha256"]
                        or portable["interrupts"] != cap["samples"]
                        or sum(w for w, _ in portable["stacks"]) != cap["samples"]
                    ):
                        raise ValueError("ELF-relative samples differ from capture")
                    total_samples += cap["samples"]
                    if binary := elfs.get(cap["binary_sha256"]):
                        matched_elfs.add(cap["binary_sha256"])
                        frames, stacks = compact(portable["stacks"], binary)
                        if [list(f) for f in frames] != cap["frames"] or stacks != cap[
                            "stacks"
                        ]:
                            raise ValueError("fresh symbolization differs from capture")
                        resolved += 1
        if set(registered) != {
            file.name.removesuffix("-resources.json")
            for file in study.glob("*-resources.json")
        }:
            raise ValueError("registered and saved process windows differ")
        for instance, captured_resource in registered.items():
            file = study / (instance + "-resources.json")
            native = study / (instance + "-native.json")
            sha = digest(native)
            if instance in windows or sha in native_hashes:
                raise ValueError("reused window")
            windows.add(instance)
            native_hashes.add(sha)
            r = read(file)
            run = read(native)
            if captured_resource is not None and r != captured_resource:
                raise ValueError("captured and independently recorded resources differ")
            if (
                r["successes"] != run["counts"]["successes"]
                or r["successes"] != REQUESTS
                or r["preflights"] != run["verification"]["passed"]
                or r["preflights"] != 1
                or r["receipts"] != r["successes"] + r["preflights"]
            ):
                raise ValueError("application and native accounting differ")
            successes += r["successes"]
            probes += r["preflights"]
    for key in (
        "engine_sha256",
        "body_sha256",
        "header_sha256",
        "dataset_sha256",
        "assigned_cpus",
    ):
        if any(p[key] != protocols["release"][key] for p in protocols.values()):
            raise ValueError("cross-study workload or engine drift")
    if matched_elfs != set(elfs):
        raise ValueError("an explicitly supplied ELF differs from every profiled build")
    source = protocols["source"]["builds"]
    changed = protocols["intervention"]["builds"]
    residual = protocols["residual"]["builds"]
    if changed["baseline"] != source["candidate"] or residual != {
        "baseline": source["baseline"],
        "candidate": changed["candidate"],
    }:
        raise ValueError("control and intervention builds differ")
    return dict(
        studies=summaries,
        process_windows=len(windows),
        timed_successes=successes,
        native_preflights=probes,
        profile_samples=total_samples,
        reports_replayed=reports,
        profiles_resymbolized=resolved,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "root", type=Path, help="directory containing the unpacked studies"
    )
    parser.add_argument("--engine", type=Path, required=True)
    parser.add_argument(
        "--elf",
        action="append",
        default=[],
        type=Path,
        help="exact measured ELF, repeatable",
    )
    args = parser.parse_args()
    binaries = {digest(binary): binary.resolve(strict=True) for binary in args.elf}
    print(
        json.dumps(
            audit(args.root, args.engine.resolve(strict=True), binaries), indent=2
        )
    )
