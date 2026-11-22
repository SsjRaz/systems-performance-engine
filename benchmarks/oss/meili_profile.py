"""Reproduce the paired local Meilisearch CPU study with verified, fixed search inputs."""

import argparse
from contextlib import nullcontext
import hashlib
import json
import os
from pathlib import Path
import random
import re
import subprocess
import time

from benchlib import client, cpus, digest, port_number, request
import meilisearch_bench as meili

REQUESTS = 384
RUNTIME = "local-two-cpus-rayon2-warm-catalog"
ARMS = ("baseline", "candidate")


def read(path):
    return json.loads(path.read_text())


def store(path, value):
    with path.open("x") as out:
        json.dump(value, out, separators=(",", ":"), allow_nan=False)
        out.write("\n")


def fingerprint(data):
    return hashlib.sha256(data).hexdigest()


def target_id(arm):
    return fingerprint((arm["sha256"] + "/" + RUNTIME).encode())


def targets(args):
    if args.arms:
        specs = read(args.arms)
    else:
        specs = {
            arm: dict(
                path=str(args.tools / ("meili-" + version)), version=version, sha256=sha
            )
            for arm, (version, sha) in zip(ARMS, meili.RELEASES.values())
        }
    if set(specs) != set(ARMS):
        raise ValueError("two target builds required")
    for arm in specs.values():
        binary = Path(arm["path"]).resolve(strict=True)
        if (
            arm["version"] not in {v for v, _ in meili.RELEASES.values()}
            or digest(binary) != arm["sha256"]
        ):
            raise ValueError("target version or ELF fingerprint mismatch")
        arm["path"] = str(binary)
        notes = subprocess.check_output(["readelf", "-n", binary], text=True)
        arm["build_id"] = re.search(r"Build ID: ([0-9a-f]{40})\b", notes)[1]
    return specs


def pinned_inputs(args):
    setup = read(args.fixture / "evidence/setup.json")
    left, right = [setup["versions"][x]["dataset_sha256"] for x in ("A", "B")]
    if (
        left != right
        or set(left) != set(meili.INDEXES)
        or setup["documents_per_index"] != 100000
        or any(v != left["catalog_a"] for v in left.values())
    ):
        raise ValueError("expected two identical seeded catalogs per release")
    state = args.fixture / "state"
    if (
        fingerprint(meili.body(1000)) != setup["inputs"]["1000"]["sha256"]
        or digest(state / "filter-1000.json") != setup["inputs"]["1000"]["sha256"]
        or digest(state / "headers.json") != setup["header_sha256"]
    ):
        raise ValueError("pinned search body or header identity changed")
    return dict(
        dataset_sha256=left["catalog_a"],
        body_sha256=setup["inputs"]["1000"]["sha256"],
        header_sha256=setup["header_sha256"],
        documents_per_index=setup["documents_per_index"],
    )


def order(prefix, count, rng, profiling=False):
    firsts = list(ARMS) * (count // 2)
    rng.shuffle(firsts)
    result = []
    for i, first in enumerate(firsts, 1):
        tag = f"{prefix}-{i:02}"
        rows = []
        for arm in (first, ARMS[1 - ARMS.index(first)]):
            instance = f"{tag}-{arm}"
            if profiling:
                rows.append(dict(arm=arm, capture=instance + "-capture.json"))
            else:
                rows.append(
                    dict(arm=arm, instance=instance, result=instance + "-native.json")
                )
        result.append(dict(id=tag, runs=rows))
    return result


def declare(args, arms, inputs):
    d = args.output
    d.mkdir(parents=True, exist_ok=False)
    rules = d / "checks.json"
    rules.touch(mode=0o600, exist_ok=False)
    rules.write_text(
        json.dumps(
            dict(
                policy_id="federated-catalog-v1",
                endpoints=[dict(fields={"/estimatedTotalHits": 2000})],
            )
        )
    )
    rng = random.Random(args.seed)
    protocol = dict(
        requests=REQUESTS,
        order_seed=args.seed,
        port=port_number(),
        runtime_id=RUNTIME,
        assigned_cpus=cpus()[1],
        engine_sha256=digest(args.engine),
        **inputs,
        builds={
            key: {k: v for k, v in value.items() if k != "path"}
            for key, value in arms.items()
        },
        resource_scope="all successful searches in one SPE invocation, including its one identical preflight",
        clock_tick_us=1_000_000 // os.sysconf("SC_CLK_TCK"),
        cpu_floor_us=150,
        cpu_floor_percent=10,
    )
    store(d / "protocol.json", protocol)
    store(
        d / "timing.json",
        dict(
            schema_version=1,
            experiment_id=args.name,
            workload_id=inputs["dataset_sha256"],
            baseline_version=target_id(arms["baseline"]),
            candidate_version=target_id(arms["candidate"]),
            metric="mean_latency_ms",
            meaningful_change_percent=10,
            private_inputs_confirmed=True,
            blocks=order(args.name, args.pairs, rng),
        ),
    )
    store(d / "profile-order.json", order(args.name + "-prof", 4, rng, True))


def snapshot(pid):
    stat = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    status = Path(f"/proc/{pid}/status").read_text()
    return (
        time.monotonic_ns(),
        int(stat[11]),
        int(stat[12]),
        int(re.search(r"VmHWM:\s+(\d+) kB", status)[1]),
    )


def run_window(args, arm, protocol, instance, profiler=None):
    d, port, state = args.output, protocol["port"], args.fixture / "state"
    if (
        any(value != protocol[key] for key, value in pinned_inputs(args).items())
        or digest(Path(arm["path"])) != arm["sha256"]
    ):
        raise ValueError("input or executable drift")
    log = d / (instance + ".log")
    result = d / (instance + "-native.json")
    environment = profiler.environment() if profiler else {}
    with meili.serve_binary(
        Path(arm["path"]), state, arm["version"], port, log, environment
    ) as app:
        pid = app.proc.pid
        if (
            not os.path.samefile(f"/proc/{pid}/exe", arm["path"])
            or request(port, "/version")["pkgVersion"] != arm["version"]
            or sorted(os.sched_getaffinity(pid)) != protocol["assigned_cpus"]
        ):
            raise ValueError("live executable, version or CPU affinity mismatch")
        for uid in meili.INDEXES:
            stats = request(port, f"/indexes/{uid}/stats")
            if stats["numberOfDocuments"] != 100000 or stats["isIndexing"]:
                raise ValueError("catalog not ready")
        before_hits = meili.verify_hits(port, 1000)
        offset = log.stat().st_size
        with profiler.active(pid, log) if profiler else nullcontext():
            before = snapshot(pid)
            proc = client(
                [
                    args.engine,
                    f"http://127.0.0.1:{port}/multi-search",
                    "--requests",
                    str(REQUESTS),
                    "--concurrency",
                    "1",
                    "--timeout-ms",
                    "4000",
                    "--method",
                    "POST",
                    "--replay-safe-post",
                    "--headers-file",
                    state / "headers.json",
                    "--body-file",
                    state / "filter-1000.json",
                    "--verify-response",
                    d / "checks.json",
                    "--json",
                    result,
                ]
            )
            after = snapshot(pid)
        if proc.returncode:
            raise RuntimeError("SPE run failed: " + proc.stderr[-400:])
        receipts = meili.search_receipts(log, offset)
        after_hits = meili.verify_hits(port, 1000)
        doc = read(result)
        if (
            doc["verification"]["status"] != "passed"
            or doc["verification"]["passed"] != 1
            or doc["counts"]["successes"] != REQUESTS
            or receipts != REQUESTS + 1
        ):
            raise ValueError("native and application success counts disagree")
        if before_hits["ids"] != after_hits["ids"]:
            raise ValueError("returned identifiers changed within the process")
        tick = protocol["clock_tick_us"]
        resource = dict(
            collector="gperftools" if profiler else "none",
            preflights=1,
            successes=REQUESTS,
            receipts=receipts,
            wall_us=(after[0] - before[0]) // 1000,
            user_us=(after[1] - before[1]) * tick,
            system_us=(after[2] - before[2]) * tick,
            peak_rss_kib=after[3],
            cpu_capacity=len(protocol["assigned_cpus"]),
            clock_tick_us=tick,
        )
    return result, resource


def native_report(args, mode, manifest, filename):
    out = args.output / filename
    proc = subprocess.run(
        [args.engine, mode, args.output / manifest, "--json", out],
        capture_output=True,
        text=True,
        timeout=40,
    )
    if proc.returncode:
        raise RuntimeError(
            f"{mode} failed ({proc.returncode}): {proc.stdout[-250:]} {proc.stderr[-250:]}"
        )
    return read(out)


def collect_profiles(args, builds, protocol):
    from meili_samples import HZ, Sampler, compact, raw_profile

    out = args.output
    if read(out / "decision.json")["verdict"] != "regression":
        raise ValueError("an independent unprofiled regression is required")
    if not args.library:
        raise ValueError("supply the gperftools shared library")
    pairs = read(out / "profile-order.json")
    plan = dict(
        schema_version=2,
        timing_manifest="timing.json",
        dataset_sha256=protocol["dataset_sha256"],
        collector="gperftools",
        frequency_hz=HZ,
        cpu_floor_us=protocol["cpu_floor_us"],
        cpu_floor_percent=protocol["cpu_floor_percent"],
        cpu_capacity=len(protocol["assigned_cpus"]),
        clock_tick_us=protocol["clock_tick_us"],
        pairs=pairs,
    )
    store(out / "profile.json", plan)
    for pair in pairs:
        for row in pair["runs"]:
            name = row["capture"].removesuffix("-capture.json")
            arm = builds[row["arm"]]
            sampler = Sampler(args.library, out / (name + ".prof"))
            native, resources = run_window(args, arm, protocol, name, sampler)
            store(out / (name + "-resources.json"), resources)
            rows, maps = raw_profile(sampler.raw, sampler.interrupts)
            frames, stacks = compact(rows, maps, Path(arm["path"]))
            capture = dict(
                schema_version=2,
                instance=name,
                binary_sha256=arm["sha256"],
                build_id=arm["build_id"],
                runtime_id=protocol["runtime_id"],
                dataset_sha256=plan["dataset_sha256"],
                policy_id="federated-catalog-v1",
                collector=plan["collector"],
                frequency_hz=HZ,
                native_result=native.name,
                native_sha256=digest(native),
                samples=sampler.interrupts,
                frames=frames,
                stacks=stacks,
                resource=resources,
            )
            store(out / row["capture"], capture)
            print(name, sampler.interrupts, "interrupts", flush=True)


def main(args):
    args.engine = args.engine.resolve(strict=True)
    args.fixture = args.fixture.resolve(strict=True)
    args.output = args.output.resolve()
    args.tools = args.tools.resolve()
    if any(
        v
        and (
            k in {"LD_PRELOAD", "LD_AUDIT", "TCMALLOC_STACKTRACE_METHOD"}
            or k.startswith("CPUPROFILE")
        )
        for k, v in os.environ.items()
    ):
        raise ValueError("clear inherited instrumentation")
    builds = targets(args)
    inputs = pinned_inputs(args)
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,30}", args.name):
        raise ValueError("invalid experiment name")
    if args.phase == "timing":
        declare(args, builds, inputs)
    protocol = read(args.output / "protocol.json")
    expected = {
        k: {f: v for f, v in spec.items() if f != "path"} for k, spec in builds.items()
    }
    if (
        protocol["builds"] != expected
        or protocol["engine_sha256"] != digest(args.engine)
        or any(protocol[k] != v for k, v in inputs.items())
        or protocol["assigned_cpus"] != cpus()[1]
    ):
        raise ValueError("study or build identity changed")
    if args.phase == "timing":
        for pair in read(args.output / "timing.json")["blocks"]:
            for row in pair["runs"]:
                _, r = run_window(args, builds[row["arm"]], protocol, row["instance"])
                store(args.output / (row["instance"] + "-resources.json"), r)
        report = native_report(args, "analyze", "timing.json", "decision.json")
        print(
            report["verdict"],
            report["effect_percent"],
            report["interval_percent"],
            flush=True,
        )
    else:
        collect_profiles(args, builds, protocol)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("timing", "profile"))
    for opt in ("engine", "tools", "fixture", "output"):
        parser.add_argument("--" + opt, type=Path, required=True)
    parser.add_argument("--name", default="meili")
    parser.add_argument("--pairs", type=int, choices=(8, 12, 16), default=8)
    parser.add_argument("--seed", type=int, default=56119)
    parser.add_argument(
        "--arms",
        type=Path,
        help="optional pinned builds with path, version and sha256 per arm",
    )
    parser.add_argument(
        "--library", type=Path, help="gperftools libprofiler.so for diagnostic windows"
    )
    main(parser.parse_args())
