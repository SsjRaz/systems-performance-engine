"""Linux helpers for bounded, loopback-only application measurements."""

import contextlib
import hashlib
import http.client
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import threading
import time


def encode(value):
    return json.dumps(value, separators=(",", ":"), ensure_ascii=True).encode()


def save(path, value):
    path.write_text(json.dumps(value, indent=2) + "\n")


def digest(path):
    checksum = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            checksum.update(chunk)
    return checksum.hexdigest()


def port_number():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def request(port, path, method="GET", data=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=12)
    try:
        conn.request(method, path, body=data, headers=headers or {})
        response = conn.getresponse()
        raw = response.read(4 * 1024 * 1024 + 1)
        if len(raw) > 4 * 1024 * 1024 or not 200 <= response.status < 300:
            raise RuntimeError(f"{method} {path}: HTTP {response.status} {raw[:200]!r}")
        return json.loads(raw) if raw else None
    finally:
        conn.close()


def cpus():
    allowed = sorted(os.sched_getaffinity(0))
    if len(allowed) < 3:
        raise RuntimeError("This protocol uses a client CPU and two separate application CPUs.")
    return allowed[0], allowed[1:3]


def proc_numbers(pid):
    # Fields 14–17 contain user/system time for the process and waited-for children.
    fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    return sum(int(v) for v in fields[11:15]) / os.sysconf("SC_CLK_TCK")


def rss_kib(pid):
    try:
        pages = int(Path(f"/proc/{pid}/statm").read_text().split()[1])
        return pages * os.sysconf("SC_PAGE_SIZE") // 1024
    except (FileNotFoundError, ProcessLookupError):
        return 0


class Application:
    def __init__(self, proc, children):
        self.proc = proc
        self.children = children
        self.peak = 0
        self.failure = None
        self.lock = threading.Lock()
        self.done = threading.Event()

    def memory(self):
        value = rss_kib(self.proc.pid)
        if self.children:
            # Git commands are normally direct children. Count each PID once.
            pids = set()
            for entry in Path(f"/proc/{self.proc.pid}/task").glob("*/children"):
                try:
                    pids.update(entry.read_text().split())
                except (FileNotFoundError, ProcessLookupError):
                    pass
            value += sum(rss_kib(pid) for pid in pids)
        return value

    def monitor(self):
        deadline = time.monotonic() + 240
        while not self.done.wait(0.05):
            if self.proc.poll() is not None:
                return
            try:
                with self.lock:
                    value = self.memory()
                    self.peak = max(self.peak, value)
                if value <= 1536 * 1024 and time.monotonic() < deadline:
                    continue
                self.failure = "application exceeded the 1.5 GiB sampled RSS or four-minute block budget"
                os.killpg(self.proc.pid, signal.SIGKILL)
                return
            except ProcessLookupError:
                return

    def measured(self, action):
        with self.lock:
            self.peak = self.memory()
        start_cpu = proc_numbers(self.proc.pid)
        start = time.monotonic()
        result = action()
        elapsed = time.monotonic() - start
        cpu = proc_numbers(self.proc.pid) - start_cpu
        with self.lock:
            memory = max(self.peak, self.memory())
        return result, dict(
            wall_s=elapsed,
            app_cpu_s=cpu,
            app_cpu_pct_one_core=100 * cpu / elapsed,
            sampled_peak_rss_kib=memory,
        )


@contextlib.contextmanager
def launch(command, cwd, environment, port, health, logfile, children=False):
    _, server_cpus = cpus()
    with socket.socket() as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind(("127.0.0.1", port))
    args = ["taskset", "-c", ",".join(map(str, server_cpus)), *map(str, command)]
    with logfile.open("wb") as log:
        proc = subprocess.Popen(
            args, cwd=cwd, env=environment, stdout=log, stderr=subprocess.STDOUT, start_new_session=True
        )
        app = Application(proc, children)
        monitor = threading.Thread(target=app.monitor, daemon=True)
        monitor.start()
        try:
            deadline = time.monotonic() + 35
            while True:
                if proc.poll() is not None or time.monotonic() > deadline:
                    raise RuntimeError(f"Application startup failed. See {logfile}")
                try:
                    request(port, health)
                    break
                except (OSError, http.client.HTTPException, RuntimeError, ValueError):
                    time.sleep(0.1)
            yield app
            if app.failure or proc.poll() is not None:
                raise RuntimeError(app.failure or "Application exited during the block")
        finally:
            app.done.set()
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGTERM)
                try:
                    proc.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait()
            monitor.join(timeout=1)


def client(args, env=None):
    cpu, _ = cpus()
    return subprocess.run(
        ["taskset", "-c", str(cpu), *map(str, args)], text=True, capture_output=True, env=env, timeout=55
    )


def spe_trial(engine, port, path, flags, out, app):
    run, resources = app.measured(
        lambda: client(
            [engine, f"http://127.0.0.1:{port}{path}", *flags, "--timeout-ms", "4000", "--json", "-"]
        )
    )
    if run.returncode not in (0, 3):
        raise RuntimeError(run.stderr)
    doc = json.loads(run.stdout)
    out.write_text(run.stdout)
    c, s = doc["counts"], doc["scheduling"]
    if doc["interrupted"] or doc["engine_error"] or c["cancelled"] or c["started"] != c["completed"]:
        raise RuntimeError(f"Unexpected termination: {out}")
    if s and (
        c["requested"] != s["offered"]
        or c["started"] + s["missed_late"] + s["missed_capacity"] != s["offered"]
    ):
        raise RuntimeError(f"Arrival accounting mismatch: {out}")
    if (run.returncode == 0) != (c["requested"] == c["successes"]):
        raise RuntimeError(f"Unexpected exit status: {out}")
    return doc, dict(
        resources,
        exit=run.returncode,
        requested=c["requested"],
        started=c["started"],
        successes=c["successes"],
        http_errors=c["http_errors"],
        successful_rps=doc["successful_throughput_rps"],
        latency_ms=doc["success_latency_ms"],
        scheduling=s,
    )


def compare(engine, rows, out, acknowledgements=()):
    args = [engine, "compare", *acknowledgements]
    for row in rows:
        args.extend(["--baseline" if row["label"] == "A" else "--candidate", out.parent / row["raw"]])
    run = client([*args, "--json", "-"])
    if run.returncode not in (0, 3):
        raise RuntimeError(f"Native comparison rejected inputs: {run.stderr}")
    report = json.loads(run.stdout)
    out.write_text(run.stdout)
    return report


def new_directories(out):
    out.mkdir(parents=True, exist_ok=False)
    state = out / "state"
    evidence = out / "evidence"
    state.mkdir(mode=0o700)
    evidence.mkdir()
    return state, evidence
