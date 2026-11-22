"""Collect userspace CPU samples with existing Linux tools and export bounded, leaf-first stacks."""

from collections import Counter
import ctypes.util
import json
import os
from pathlib import Path
import re
import resource
import select
import shutil
import signal
import subprocess
import sys
import tempfile

HZ = 500
RAW_LIMIT = 8 * 1024 * 1024
TEXT_LIMIT = 16 * 1024 * 1024
OTHER = ("[other]", "", 0)


def file_cap(limit):
    def apply():
        resource.setrlimit(resource.RLIMIT_FSIZE, (limit, limit))

    return apply


def export(command, outfile, env=None):
    with outfile.open("xb") as dst:
        proc = subprocess.run(
            command,
            stdout=dst,
            stderr=subprocess.PIPE,
            env=env,
            timeout=40,
            preexec_fn=file_cap(TEXT_LIMIT),
        )
    if proc.returncode or outfile.stat().st_size >= TEXT_LIMIT:
        raise RuntimeError(f"profile export failed: {proc.stderr[-600:]!r}")
    return outfile.read_text(errors="replace")


def probe_perf(perf):
    probe = subprocess.run(
        [perf, "stat", "-e", "cpu-clock:u", "--", "true"],
        capture_output=True,
        text=True,
        timeout=8,
    )
    if probe.returncode:
        return probe.stderr.strip()[:600] or "perf stat failed"
    # A child stat can succeed while attaching to a sibling process or unwinding is restricted.
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        target = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import time\nend=time.monotonic()+3\nwhile time.monotonic()<end: pass",
            ]
        )
        try:
            recorded = subprocess.run(
                [
                    perf,
                    "record",
                    "--no-buildid-cache",
                    "--no-inherit",
                    "-e",
                    "cpu-clock:u",
                    "-c",
                    str(10**9 // HZ),
                    "--call-graph",
                    "fp",
                    "-p",
                    str(target.pid),
                    "-o",
                    folder / "probe.data",
                    "--",
                    "sleep",
                    "0.25",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                timeout=8,
                preexec_fn=file_cap(RAW_LIMIT),
            )
            if recorded.returncode:
                return recorded.stderr.strip()[:600] or "perf attach failed"
            env = dict(os.environ, SPE_FIXTURE_PID=str(target.pid), DEBUGINFOD_URLS="")
            output = export(
                [
                    perf,
                    "script",
                    "-i",
                    folder / "probe.data",
                    "-s",
                    Path(__file__).with_name("perf_frames.py"),
                ],
                folder / "probe.txt",
                env,
            )
            if not any(
                len(json.loads(line)) > 1
                for line in output.splitlines()
                if line.strip()
            ):
                return "perf did not export a userspace call chain"
        finally:
            target.terminate()
            target.wait(timeout=4)
    return ""


def choose(mode, perf, library, pprof):
    perf = perf or shutil.which("perf")
    probe = "perf executable unavailable"
    if perf and mode != "gperftools":
        try:
            probe = probe_perf(perf)
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
            probe = str(error)[:600]
        if not probe:
            return dict(
                kind="perf",
                hz=HZ,
                perf=str(perf),
                version=subprocess.check_output([perf, "version"], text=True).strip(),
                perf_probe="available",
            )
    if mode == "perf":
        raise RuntimeError(probe)
    if mode == "gperftools":
        probe = "gperftools explicitly selected"
    library = library or ctypes.util.find_library("profiler")
    pprof = pprof or shutil.which("google-pprof")
    if not library or not pprof:
        raise RuntimeError(
            f"no available CPU sampler ({probe}); the fallback needs libprofiler and google-pprof"
        )
    return dict(
        kind="gperftools",
        hz=HZ,
        library=str(library),
        pprof=str(pprof),
        perf_probe=probe,
        version=subprocess.check_output(
            [pprof, "--version"], text=True, stderr=subprocess.STDOUT
        )
        .strip()
        .splitlines()[0],
    )


def frame(symbol, filename, line):
    prefix = "/spe-fixture-src/"
    if not filename.startswith(prefix) or not line or symbol in ("??", "unknown", ""):
        return OTHER
    return symbol, filename.removeprefix(prefix), int(line)


def gperf_stacks(text):
    # google-pprof --text --stacks writes the interrupted leaf first.
    active, count, stack = False, 0, []
    rows = []
    pattern = re.compile(
        r"^\s*(?:(\d+)\s+)?\(([0-9a-fA-F]+)\)\s+(.*?):(\d+)(?: \(discriminator \d+\))?:(.*)$"
    )
    for line in text.splitlines():
        if line.strip() == "Stacks:":
            active = True
            continue
        if not active:
            continue
        match = pattern.match(line)
        if match:
            weight, _, file, loc, sym = match.groups()
            if weight is not None:
                if count:
                    rows.append((count, stack))
                count, stack = int(weight), []
            if not count:
                raise ValueError("missing stack weight")
            stack.append(frame(sym, file, int(loc)))
        elif line.strip():
            if "(" in line and re.match(r"^\s*(?:\d+\s+)?\([0-9a-fA-F]+\)", line):
                raise ValueError("unrecognized pprof frame")
            break
    if count:
        rows.append((count, stack))
    return rows


def encode(rows):
    frames, indexes, stacks = [], {}, Counter()
    total = 0
    for count, trace in rows:
        if not 0 < count <= 100000 or not 0 < len(trace) <= 32:
            raise ValueError("stack bounds exceeded")
        ids = []
        for item in trace:
            if item not in indexes:
                indexes[item] = len(frames)
                frames.append(list(item))
            ids.append(indexes[item])
        stacks[tuple(ids)] += count
        total += count
        if len(frames) > 512 or len(stacks) > 1024 or total > 100000:
            raise ValueError("capture bounds exceeded")
    return frames, [[count, list(ids)] for ids, count in sorted(stacks.items())], total


class PerfCapture:
    def __init__(self, sampler, pid, folder, binary):
        self.proc = None
        self.fds = []
        self.folder = folder
        self.perf = sampler["perf"]
        self.log = None
        try:
            # Preserve only executable mappings for the matching target ELF.
            maps = []
            for line in Path(f"/proc/{pid}/maps").read_text().splitlines():
                fields = line.split(maxsplit=5)
                if len(fields) == 6 and fields[5] == str(binary) and "x" in fields[1]:
                    lo, hi = fields[0].split("-")
                    maps.append([int(lo, 16), int(hi, 16), int(fields[2], 16)])
            if not maps:
                raise RuntimeError("missing target executable mapping")
            (folder / "target-map.json").write_text(
                json.dumps(dict(pid=pid, maps=maps))
            )
            read, self.command = os.pipe()
            self.ack, write = os.pipe()
            self.fds = [self.command, self.ack, read, write]
            self.log = (folder / "perf.stderr").open("xb")
            self.proc = subprocess.Popen(
                [
                    self.perf,
                    "record",
                    "--no-buildid-cache",
                    "--no-inherit",
                    "-D",
                    "-1",
                    f"--control=fd:{read},{write}",
                    "-m",
                    "64",
                    "-e",
                    "cpu-clock:u",
                    "-c",
                    str(10**9 // sampler["hz"]),
                    "--call-graph",
                    "fp",
                    "-p",
                    str(pid),
                    "-o",
                    folder / "perf.data",
                ],
                pass_fds=(read, write),
                stdout=subprocess.DEVNULL,
                stderr=self.log,
                preexec_fn=file_cap(RAW_LIMIT),
            )
            os.close(read)
            os.close(write)
            self.fds = [self.command, self.ack]
        except BaseException:
            self.close()
            raise

    def control(self, value):
        os.write(self.command, value + b"\n")
        if (
            not select.select([self.ack], [], [], 8)[0]
            or os.read(self.ack, 32).strip(b"\n\0") != b"ack"
        ):
            raise RuntimeError("perf control acknowledgement missing")

    def enable(self):
        self.control(b"enable")

    def finish(self):
        self.control(b"disable")
        self.proc.send_signal(signal.SIGINT)
        if self.proc.wait(timeout=8) not in (0, -signal.SIGINT, 128 + signal.SIGINT):
            raise RuntimeError("perf failed to finalize the capture")

    def close(self):
        if self.proc and self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait()
        for fd in self.fds:
            os.close(fd)
        self.fds = []
        if self.log:
            self.log.close()


def perf_stacks(sampler, folder, binary):
    perf, raw = sampler["perf"], folder / "perf.data"
    dump = export([perf, "script", "-D", "-i", raw], folder / "record-types.txt")
    if re.search(r"PERF_RECORD_(LOST|THROTTLE)", dump):
        raise ValueError("lost or throttled perf samples")
    mapping = json.loads((folder / "target-map.json").read_text())
    env = dict(os.environ, SPE_FIXTURE_PID=str(mapping["pid"]), DEBUGINFOD_URLS="")
    text = export(
        [perf, "script", "-i", raw, "-s", Path(__file__).with_name("perf_frames.py")],
        folder / "stacks.txt",
        env,
    )
    traces = [json.loads(line) for line in text.splitlines() if line.strip()]
    if len(traces) != len(re.findall(r"\bPERF_RECORD_SAMPLE\b", dump)):
        raise ValueError("export does not cover all recorded perf samples")
    if len(traces) > 100000 or any(not 0 < len(trace) <= 32 for trace in traces):
        raise ValueError("perf sample or stack bounds exceeded")
    return symbolize_perf(traces, mapping["maps"], binary)


def symbolize_perf(traces, maps, binary):
    segments = []
    for line in subprocess.check_output(
        ["readelf", "-lW", binary], text=True
    ).splitlines():
        parts = line.split()
        if parts and parts[0] == "LOAD":
            segments.append(tuple(int(parts[i], 16) for i in (1, 2, 4)))

    def target_vma(pc):
        for low, high, offset in maps:
            if low <= pc < high:
                file_pos = pc - low + offset
                for start, vma, length in segments:
                    if start <= file_pos < start + length:
                        return vma + file_pos - start
        return None

    mapped = [
        tuple(target_vma(pc - (i > 0)) for i, pc in enumerate(trace))
        for trace in traces
    ]
    addresses = sorted({a for trace in mapped for a in trace if a is not None})
    if len(addresses) > 16384:
        raise ValueError("too many symbol addresses")
    process = subprocess.run(
        ["addr2line", "-C", "-f", "-e", binary],
        input="".join(f"{a:x}\n" for a in addresses),
        capture_output=True,
        text=True,
        check=True,
        timeout=20,
    )
    lines = process.stdout.splitlines()
    if len(lines) != 2 * len(addresses):
        raise ValueError("incomplete addr2line output")
    symbols = {None: OTHER}
    for i, addr in enumerate(addresses):
        location = re.match(
            r"^(.*?):(\d+)(?: \(discriminator \d+\))?$", lines[2 * i + 1]
        )
        symbols[addr] = (
            frame(lines[2 * i], location[1], int(location[2])) if location else OTHER
        )
    return [(1, [symbols[addr] for addr in trace]) for trace in mapped]


def sample_stacks(sampler, folder, binary, stderr):
    raw = folder / ("cpu.prof" if sampler["kind"] == "gperftools" else "perf.data")
    if not 0 < raw.stat().st_size < RAW_LIMIT:
        raise ValueError("invalid or oversized raw profile")
    if sampler["kind"] == "perf":
        return encode(perf_stacks(sampler, folder, binary))
    text = export(
        [
            sampler["pprof"],
            "--text",
            "--stacks",
            "--lines",
            "--no-auto-signal-frm",
            binary,
            raw,
        ],
        folder / "stacks.txt",
        dict(os.environ, PPROF_TMPDIR=str(folder), DEBUGINFOD_URLS=""),
    )
    frames, stacks, total = encode(gperf_stacks(text))
    match = re.search(r"interrupts/evictions/bytes\s*=\s*(\d+)/", stderr)
    if not match or int(match[1]) != total:
        raise ValueError(
            f"exported samples {total} disagree with sampling interrupts: {stderr[-200:]}"
        )
    return frames, stacks, total
