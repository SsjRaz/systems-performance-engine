"""Measure two SPE binaries against the loopback timing fixture on separate Linux CPUs."""

import argparse
import csv
import hashlib
import json
import math
import multiprocessing
import os
import platform
import resource
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
from integration import TimingService


def measured_child(args):
    # wait4's maximum RSS can include the Python launcher's RSS before exec.
    # Sample the running child's own high-water mark instead (may miss final teardown).
    observed_kib = 0
    deadline = time.monotonic() + 30
    with subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as child:
        status = Path(f"/proc/{child.pid}/status")
        try:
            while True:
                try:
                    stdout, stderr = child.communicate(timeout=0.05)
                    break
                except subprocess.TimeoutExpired:
                    if time.monotonic() >= deadline:
                        raise
                    for line in status.read_text().splitlines():
                        if line.startswith("VmHWM:"):
                            observed_kib = max(observed_kib, int(line.split()[1]))
        finally:
            if child.poll() is None:
                child.kill()
                child.communicate()
    if child.returncode not in (0, 3):
        raise RuntimeError(f"SPE exited {child.returncode}: {stderr}")
    return child.returncode, stdout, observed_kib or None


def trial(engine, client_cpu, server_cpu, rate, concurrency, output):
    # This process handles one trial. Both peak RSS counters therefore start fresh.
    os.sched_setaffinity(0, {server_cpu})
    with TimingService() as fixture:
        args = ["taskset", "-c", str(client_cpu), str(engine), fixture.url + "/ok",
                "--concurrency", str(concurrency), "--timeout-ms", "1000", "--json", "-"]
        args += ["--rate", str(rate), "--duration-ms", "3000"] if rate else ["--requests", "4096"]
        start_cpu = time.process_time()
        start = time.monotonic()
        returncode, stdout, generator_kib = measured_child(args)
        wall = time.monotonic() - start
        server_cpu_s = time.process_time() - start_cpu
        generator = resource.getrusage(resource.RUSAGE_CHILDREN)
        server = resource.getrusage(resource.RUSAGE_SELF)
        receipts = fixture.server.receipts.copy()
    output.write_text(stdout)
    doc = json.loads(stdout)
    c = doc["counts"]
    s = doc["scheduling"] or {}
    late, capacity = s.get("missed_late", 0), s.get("missed_capacity", 0)
    budget = rate * 3 if rate else 4096
    if (doc["interrupted"] or doc["engine_error"] is not None or
            c["requested"] != budget or c["started"] != c["completed"] or
            c["successes"] != c["started"] or c["successes"] != len(receipts) or
            c["successes"] + late + capacity != budget or
            returncode != (3 if late + capacity else 0)):
        raise RuntimeError(f"Unexpected transfer outcome or receipt count: {output}")
    gaps = sorted((end - begin) / 1e6 for begin, end in zip(receipts, receipts[1:]))
    if not gaps:
        raise RuntimeError(f"Insufficient server receipts: {output}")
    return dict(requested=budget, started=c["started"], successes=c["successes"],
                offered_rps=s.get("offered_rps"), dispatched_rps=s.get("dispatched_rps"),
                successful_rps=doc["successful_throughput_rps"], late=late, capacity=capacity,
                dispatch_p95_ms=s["dispatch_delay_ms"]["p95"] if rate else None,
                receipts=len(receipts), receipt_span_s=(receipts[-1] - receipts[0]) / 1e9,
                gap_p50_ms=gaps[math.ceil(.50 * len(gaps)) - 1],
                gap_p95_ms=gaps[math.ceil(.95 * len(gaps)) - 1], gap_max_ms=gaps[-1],
                wall_s=wall, generator_cpu_s=generator.ru_utime + generator.ru_stime,
                server_cpu_s=server_cpu_s, generator_observed_hwm_kib=generator_kib,
                server_peak_kib=server.ru_maxrss)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", required=True, type=Path)
    parser.add_argument("--candidate", required=True, type=Path,
                        help="use the baseline binary here for an unchanged-code control")
    parser.add_argument("--output", required=True, type=Path, help="new results directory")
    parser.add_argument("--client-cpu", type=int, required=True)
    parser.add_argument("--server-cpu", type=int, required=True)
    parser.add_argument("--blocks", type=int, choices=(1, 2), default=1)
    args = parser.parse_args()
    cpus = {args.client_cpu, args.server_cpu}
    if len(cpus) != 2 or not cpus <= os.sched_getaffinity(0):
        parser.error("choose two distinct CPUs in the process affinity mask")
    engines = {"A": args.baseline.resolve(strict=True), "B": args.candidate.resolve(strict=True)}
    manifest = dict(engine_sha256={k: hashlib.sha256(v.read_bytes()).hexdigest()
                                   for k, v in engines.items()},
                    kernel=platform.release(), machine=platform.machine(), python=platform.python_version(),
                    client_cpu=args.client_cpu, server_cpu=args.server_cpu, blocks=args.blocks)
    args.output.mkdir(parents=True, exist_ok=False)
    (args.output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    cases = [(r, 4) for r in (100, 500, 1000, 2000)] + [(0, c) for c in (1, 4, 16)]
    with (args.output / "trials.csv").open("x", newline="") as table:
        writer = None
        # A fresh fixture process owns each SPE child. Only one trial runs at a time.
        with multiprocessing.get_context("spawn").Pool(1, maxtasksperchild=1) as pool:
            for block in range(args.blocks):
                for rate, concurrency in (cases if block == 0 else reversed(cases)):
                    for pos, label in enumerate("ABBA" if block == 0 else "BAAB", 1):
                        name = f"b{block + 1}-r{rate}-c{concurrency}-{pos}{label}"
                        row = dict(trial=name, label=label, target_rps=rate or None,
                                   concurrency=concurrency)
                        row.update(pool.apply(trial, (engines[label], args.client_cpu, args.server_cpu,
                                                      rate, concurrency, args.output / (name + ".json"))))
                        if writer is None:
                            writer = csv.DictWriter(table, fieldnames=row.keys())
                            writer.writeheader()
                        writer.writerow(row)
                        table.flush()
                        print(f"{name}: {row['successes']}/{row['requested']}", flush=True)


if __name__ == "__main__":
    main()
