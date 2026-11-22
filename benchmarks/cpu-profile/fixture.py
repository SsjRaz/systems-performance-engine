"""Build three reproducible ELF targets and execute one bounded loopback workload."""

import hashlib
import json
import os
from pathlib import Path
import re
import select
import shutil
import socket
import subprocess

HERE = Path(__file__).resolve().parent
SOURCE_FILES = ("catalog.hpp", "catalog.cpp", "server.cpp")
MASK = (1 << 32) - 1
POLICY = "freight-quote-digest-v1"


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_json(path, doc):
    with path.open("x") as f:
        json.dump(doc, f, indent=2, allow_nan=False)
        f.write("\n")


def dataset(root, size=32768):
    # Independent integer reference for both the catalog and the expected HTTP entity.
    seed, total = 0x47C314AE, 0
    path = root / "parcels.txt"
    with path.open("x") as f:
        for _ in range(size):
            seed = (1664525 * seed + 1013904223) & MASK
            zone, weight, units = (seed >> 11) & 255, (seed >> 3) % 20000, 1 + seed % 9
            f.write(f"{zone} {weight} {units}\n")
            key = weight * 257 + zone
            for _ in range(16):
                key = ((key ^ (key >> 15)) * 2246822519) & MASK
                key ^= key >> 13
            total += (70 + zone * 103 % 911 + key % 97) * units
    body = f'{{"parcels":{size},"cents":{total}}}\n'.encode()
    expected = hashlib.sha256(body).hexdigest()
    policy = root / "policy.json"
    with open(policy, "x", opener=lambda p, flags: os.open(p, flags, 0o600)) as f:
        json.dump({"policy_id": POLICY, "endpoints": [{"sha256": expected}]}, f)
    return dict(
        dataset_sha256=sha(path),
        response_sha256=expected,
        policy_sha256=sha(policy),
        parcels=size,
        total_cents=total,
        seed="47c314ae",
    )


def build(root, variant, compiler="c++", extra_flags=()):
    directory = root / "targets" / variant
    directory.mkdir(parents=True)
    for name in SOURCE_FILES:
        source = (
            root / "targets" / "candidate" / name
            if variant == "revert"
            else HERE / name
        )
        shutil.copyfile(source, directory / name)
    if variant in ("candidate", "revert"):
        command = [
            "patch",
            "--batch",
            "--fuzz=0",
            "-p1",
            "-i",
            str(HERE / "linear-lookup.patch"),
        ]
        if variant == "revert":
            command.insert(1, "--reverse")
        subprocess.run(command, cwd=directory, capture_output=True, check=True)
    flags = [
        *extra_flags,
        "-std=c++20",
        "-O2",
        "-g",
        "-fno-omit-frame-pointer",
        "-fno-optimize-sibling-calls",
        "-fPIE",
        "-pie",
        "-Wl,--build-id=sha1",
        f"-ffile-prefix-map={directory}=/spe-fixture-src",
    ]
    command = [
        compiler,
        *flags,
        "server.cpp",
        "catalog.cpp",
        "-ldl",
        "-o",
        "quote-server",
    ]
    subprocess.run(command, cwd=directory, check=True)
    binary = directory / "quote-server"
    notes = subprocess.check_output(["readelf", "-n", binary], text=True)
    build_id = re.search(r"Build ID: ([0-9a-f]+)", notes).group(1)
    sources = {name: sha(directory / name) for name in SOURCE_FILES}
    return dict(
        binary_sha256=sha(binary),
        build_id=build_id,
        sources=sources,
        compiler=subprocess.check_output(
            [compiler, "--version"], text=True
        ).splitlines()[0],
        flags=[x.replace(str(directory), "<source>") for x in flags],
    )


def open_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def read_marker(stream, timeout=12):
    if not select.select([stream], [], [], timeout)[0]:
        raise RuntimeError("fixture or collector readiness timed out")
    line = stream.readline(8193)
    if not line or len(line) > 8192:
        raise RuntimeError("fixture control stream closed or oversized")
    return line.decode().strip()


def stop(proc):
    if proc is not None and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def run_window(
    root,
    engine,
    instance,
    target,
    version,
    budget,
    port,
    native_file,
    delay_us=0,
    sampler=None,
):
    """Isolate probe, sampling, and timing. The target owns process CPU and receipt accounting."""
    from profile_tools import PerfCapture, RAW_LIMIT, file_cap, sample_stacks

    client_cpu, server_cpu = sorted(os.sched_getaffinity(0))[:2]
    binary = root / "targets" / target / "quote-server"
    config = json.loads((root / "inputs.json").read_text())
    if (
        sha(root / "parcels.txt") != config["dataset_sha256"]
        or sha(root / "policy.json") != config["policy_sha256"]
    ):
        raise ValueError("dataset or assertion policy changed")
    if sha(binary) != version["binary_sha256"]:
        raise ValueError("target ELF changed")
    capture_dir = native_file.parent / (instance + "-sampling")
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("CPUPROFILE", "SPE_FIXTURE_"))
    }
    if sampler:
        capture_dir.mkdir()
        if sampler["kind"] == "gperftools":
            env.update(
                SPE_FIXTURE_GPERF_LIBRARY=sampler["library"],
                SPE_FIXTURE_GPERF_OUT=str(capture_dir / "cpu.prof"),
                CPUPROFILE_FREQUENCY=str(sampler["hz"]),
            )
        else:
            env["SPE_FIXTURE_PERF_CONTROL"] = "1"
    command = [
        "taskset",
        "-c",
        str(server_cpu),
        binary,
        root / "parcels.txt",
        str(port),
        str(budget),
        str(delay_us),
    ]
    server = client = None
    recorder = None
    try:
        server = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            preexec_fn=file_cap(RAW_LIMIT),
        )
        if read_marker(server.stdout) != "READY":
            raise RuntimeError("fixture did not become ready")
        if sampler and sampler["kind"] == "perf":
            recorder = PerfCapture(sampler, server.pid, capture_dir, binary)
        client = subprocess.Popen(
            [
                "taskset",
                "-c",
                str(client_cpu),
                engine,
                f"http://127.0.0.1:{port}/v1/quote",
                "--concurrency",
                "1",
                "--requests",
                str(budget),
                "--timeout-ms",
                "8000",
                "--verify-response",
                root / "policy.json",
                "--json",
                native_file,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        if recorder:
            if read_marker(server.stdout) != "WINDOW":
                raise RuntimeError("perf preflight barrier failed")
            recorder.enable()
            server.stdin.write(b"continue\n")
            server.stdin.flush()
            if read_marker(server.stdout, 45) != "FINISHED":
                raise RuntimeError("perf stop barrier failed")
            recorder.finish()
            server.stdin.write(b"continue\n")
            server.stdin.flush()
        _, client_errors = client.communicate(timeout=48)
        if client.returncode:
            raise RuntimeError(
                f"SPE failed ({client.returncode}): {client_errors[-400:]!r}"
            )
        out, err = server.communicate(timeout=8)
        if server.returncode:
            raise RuntimeError(f"fixture failed ({server.returncode}): {err[-400:]!r}")
        resource = json.loads(out)
        run = json.loads(native_file.read_text())
        expected_collector = sampler["kind"] if sampler else "none"
        if (
            resource["collector"] != expected_collector
            or resource["preflights"] != run["verification"]["passed"]
            or run["verification"]["status"] != "passed"
            or resource["successes"] != budget
            or run["counts"]["successes"] != budget
            or run["counts"]["started"] != budget
        ):
            raise RuntimeError("native preflight or timed receipt mismatch")
        if sampler:
            frames, stacks, total = sample_stacks(
                sampler, capture_dir, binary, err.decode()
            )
            return dict(frames=frames, stacks=stacks, samples=total, resource=resource)
        return resource
    finally:
        if recorder:
            recorder.close()
        stop(client)
        stop(server)
