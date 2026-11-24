"""Local Git study orchestration, embedded in the spe executable by CMake."""

import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import random
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time


class StudyError(Exception):
    def __init__(self, message, code=1):
        super().__init__(message)
        self.code = code


def require(condition, message):
    if not condition:
        raise StudyError(message, 2)


def unique_keys(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, "duplicate key in study configuration")
        result[key] = value
    return result


def fields(obj, required, optional=()):
    require(isinstance(obj, dict), "expected a configuration object")
    for key in required:
        require(key in obj, f"missing configuration field {key}")
    require(not (obj.keys() - set(required) - set(optional)), "unknown configuration field")


def integer(obj, key, low, high):
    value = obj[key]
    require(type(value) is int and low <= value <= high, f"{key} must be an integer in {low}..{high}")


def route(path):
    require(
        isinstance(path, str)
        and path.startswith("/")
        and not path.startswith("//")
        and len(path) <= 8000
        and re.fullmatch(r"[!-~]+", path)
        and not any(c in path for c in "\\#"),
        "endpoint and readiness paths must be origin-relative ASCII paths",
    )


def load_config(path):
    with path.open("rb") as stream:
        raw = stream.read(65537)
    require(len(raw) <= 65536, "configuration exceeds 64 KiB")
    try:
        cfg = json.loads(raw, object_pairs_hook=unique_keys)
    except (ValueError, RecursionError) as error:
        raise StudyError("invalid study configuration JSON", 2) from error
    fields(
        cfg,
        (
            "schema_version",
            "workload_id",
            "build",
            "start",
            "readiness",
            "endpoint",
            "expected_response",
        ),
        ("measurement", "build_timeout_s", "port"),
    )
    integer(cfg, "schema_version", 1, 1)
    require(
        isinstance(cfg["workload_id"], str)
        and re.fullmatch(r"[A-Za-z0-9._-]{1,80}", cfg["workload_id"]),
        "workload_id must be a public identifier of 1..80 characters",
    )
    require(isinstance(cfg["build"], list), "build must be a list of commands")
    for cmd in [*cfg["build"], cfg["start"]]:
        require(
            isinstance(cmd, list)
            and cmd
            and all(isinstance(arg, str) and "\0" not in arg for arg in cmd)
            and cmd[0],
            "commands must be argument arrays with an executable",
        )
    route(cfg["endpoint"])
    ready = cfg["readiness"]
    fields(ready, ("path",), ("timeout_ms",))
    route(ready["path"])
    ready.setdefault("timeout_ms", 10000)
    integer(ready, "timeout_ms", 1, 3600000)
    cfg.setdefault("port", 0)
    integer(cfg, "port", 0, 65535)
    cfg.setdefault("build_timeout_s", 600)
    integer(cfg, "build_timeout_s", 1, 86400)
    rule = cfg["expected_response"]
    require(
        isinstance(rule, dict) and set(rule) in ({"fields"}, {"sha256"}),
        "expected_response requires one native fields or sha256 rule",
    )
    measure = cfg.setdefault("measurement", {})
    defaults = dict(
        pairs=8,
        requests=50,
        concurrency=1,
        timeout_ms=1000,
        metric="mean_latency_ms",
        meaningful_change_percent=10,
        seed=secrets.randbits(32),
    )
    fields(measure, (), defaults)
    for key, value in defaults.items():
        measure.setdefault(key, value)
    for key, low, high in (
        ("pairs", 6, 100),
        ("requests", 1, 1000000),
        ("concurrency", 1, 1024),
        ("timeout_ms", 1, 3600000),
        ("seed", 0, 2**32 - 1),
    ):
        integer(measure, key, low, high)
    require(
        measure["metric"] in ("mean_latency_ms", "p95_latency_ms", "successful_rps"),
        "unknown primary metric",
    )
    threshold = measure["meaningful_change_percent"]
    require(
        type(threshold) in (float, int) and 0 < threshold < 100,
        "threshold must be between 0 and 100 percent",
    )
    return cfg


stop_signal = 0


def request_stop(signum, _frame):
    # Defer unwinding until every spawned group is inside its cleanup scope.
    global stop_signal
    stop_signal = signum


def checkpoint():
    if stop_signal:
        raise StudyError(f"interrupted by {signal.Signals(stop_signal).name}", 128 + stop_signal)


@contextmanager
def process(argv, cwd, *, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT, env=None):
    checkpoint()
    child = subprocess.Popen(
        argv,
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=stdout,
        stderr=stderr,
        start_new_session=True,
    )
    try:
        yield child
    finally:
        # The group may still have workers even if its foreground leader exited.
        try:
            os.killpg(child.pid, signal.SIGTERM)
            try:
                child.wait(timeout=1)
            except subprocess.TimeoutExpired:
                pass
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        child.wait()


def alive(server):
    if server is not None and server.poll() is not None:
        raise StudyError(f"application exited with status {server.returncode}")


def run(argv, cwd, timeout, *, server=None, **options):
    with process(argv, cwd, **options) as child:
        deadline = time.monotonic() + timeout
        while True:
            checkpoint()
            alive(server)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise StudyError("command timed out")
            try:
                code = child.wait(timeout=min(remaining, 0.1))
            except subprocess.TimeoutExpired:
                continue
            checkpoint()
            alive(server)
            return code


def checkout_environment():
    # A parent Git hook may have exported its own index or working tree.
    return {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}


def git(cwd, *args):
    env = checkout_environment()
    env.update(
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_NO_LAZY_FETCH="1",
        GIT_NO_REPLACE_OBJECTS="1",
        GIT_ALLOW_PROTOCOL="file",
        GIT_TERMINAL_PROMPT="0",
    )
    with tempfile.TemporaryFile() as output:
        code = run(
            ["git", "-c", "core.hooksPath=" + os.devnull, *args],
            cwd,
            600,
            env=env,
            stdout=output,
            stderr=None,
        )
        if code:
            raise StudyError(f"git {args[0]} failed (exit {code})")
        output.seek(0)
        return os.fsdecode(output.read()).rstrip("\n")


def write_json(path, doc):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(doc, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def make_plan(cfg, revisions):
    settings = cfg["measurement"]
    order = [i % 2 == 0 for i in range(settings["pairs"])]
    random.Random(settings["seed"]).shuffle(order)
    blocks = []
    for i, baseline_first in enumerate(order, 1):
        name = f"pair-{i:03}"
        runs = []
        for arm in ("baseline", "candidate") if baseline_first else ("candidate", "baseline"):
            instance = f"{name}-{arm}"
            runs.append(dict(arm=arm, instance=instance, result=instance + ".json"))
        blocks.append(dict(id=name, runs=runs))
    return dict(
        schema_version=1,
        experiment_id="study-" + secrets.token_hex(8),
        workload_id=cfg["workload_id"],
        baseline_version=revisions["baseline"],
        candidate_version=revisions["candidate"],
        metric=settings["metric"],
        meaningful_change_percent=settings["meaningful_change_percent"],
        private_inputs_confirmed=False,
        blocks=blocks,
    )


def build_revisions(repo, work, output, revisions, cfg, stage):
    private = work / "source.git"
    git(work, "clone", "--quiet", "--bare", "--shared", "--template=", "--", repo, private)
    checkouts = {}
    for revision in dict.fromkeys(revisions.values()):
        checkout = work / revision
        stage(f"checkout {revision}")
        git(private, "worktree", "add", "--quiet", "--detach", "--", checkout, revision)
        log = output / f"build-{revision}.log"
        with log.open("xb") as stream:
            for index, command in enumerate(cfg["build"], 1):
                stage(f"build {revision[:12]} step {index} ({log.name})")
                code = run(
                    command, checkout, cfg["build_timeout_s"], stdout=stream, env=checkout_environment()
                )
                if code:
                    raise StudyError(f"build command exited with status {code}")
        checkouts[revision] = checkout
    return checkouts


def available_port(port):
    with socket.socket() as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", port))
        sock.listen(1)
        return sock.getsockname()[1]


def wait_ready(engine, server, origin, ready, work):
    deadline = time.monotonic() + ready["timeout_ms"] / 1000
    while True:
        checkpoint()
        alive(server)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise StudyError("readiness timed out")
        timeout_ms = max(1, min(300, int(1000 * remaining)))
        code = run(
            [
                engine,
                origin + ready["path"],
                "--requests",
                "1",
                "--concurrency",
                "1",
                "--timeout-ms",
                str(timeout_ms),
            ],
            work,
            2 + timeout_ms / 1000,
            server=server,
        )
        if code == 0 and time.monotonic() <= deadline:
            return
        if code not in (0, 3):
            raise StudyError(f"native readiness request failed (exit {code})")
        time.sleep(min(0.05, max(0, deadline - time.monotonic())))


@contextmanager
def local_instance(engine, checkout, work, cfg, port, log):
    origin = f"http://127.0.0.1:{port}"
    command = [arg.replace("{port}", str(port)) for arg in cfg["start"]]
    with tempfile.TemporaryDirectory(dir=work) as instance:
        env = dict(
            checkout_environment(), SPE_PORT=str(port), SPE_INSTANCE_DIR=instance, TMPDIR=instance
        )
        available_port(port)
        with process(command, checkout, env=env, stdout=log) as server:
            wait_ready(engine, server, origin, cfg["readiness"], work)
            yield server, origin
        # A foreground command must release its listener when its group stops.
        available_port(port)


def measure(engine, checkout, work, output, row, cfg, port, policy, stage):
    name = row["instance"]
    settings = cfg["measurement"]
    stage(f"{name} startup ({name}.log)")
    with (
        (output / (name + ".log")).open("xb") as log,
        local_instance(engine, checkout, work, cfg, port, log) as (server, origin),
    ):
        stage(f"{name} measurement ({name}.log)")
        result = output / row["result"]
        argv = [engine, origin + cfg["endpoint"], "--verify-response", policy, "--json", result]
        for key in ("requests", "concurrency", "timeout_ms"):
            argv.extend(["--" + key.replace("_", "-"), str(settings[key])])
        budget = 5 + (settings["requests"] + 1) * settings["timeout_ms"] / 1000
        code = run(argv, work, budget, stdout=log, server=server)
        if code == 3:
            failure = json.loads(result.read_text())["verification"]["failure"]
            raise StudyError(failure or "incomplete native measurement", 3)
        if code:
            raise StudyError(f"native measurement exited with status {code}", 2 if code == 2 else 1)


def study(args, engine, cfg, output):
    status = dict(state="running", stage="resolve local revisions", measurement=cfg["measurement"])

    def stage(label):
        checkpoint()
        status["stage"] = label
        write_json(output / "status.json", status)
        print(label, flush=True)

    try:
        stage(status["stage"])
        repo = Path(git(None, "rev-parse", "--show-toplevel"))
        revisions = {
            arm: git(repo, "rev-parse", "--verify", "--end-of-options", rev + "^{commit}")
            for arm, rev in (("baseline", args.baseline), ("candidate", args.candidate))
        }
        plan = make_plan(cfg, revisions)
        write_json(output / "manifest.json", plan)
        with tempfile.TemporaryDirectory(prefix="spe-study-work-") as directory:
            work = Path(directory).resolve()
            policy = work / "policy.json"
            policy.touch(mode=0o600, exist_ok=False)
            with policy.open("w", encoding="utf-8") as stream:
                json.dump(
                    dict(policy_id=cfg["workload_id"], endpoints=[cfg["expected_response"]]),
                    stream,
                    allow_nan=False,
                )
            stage("prepare isolated checkouts")
            checkouts = build_revisions(repo, work, output, revisions, cfg, stage)
            stage("select local application port")
            port = available_port(cfg["port"])
            status["port"] = port
            for block in plan["blocks"]:
                for row in block["runs"]:
                    checkout = checkouts[revisions[row["arm"]]]
                    measure(engine, checkout, work, output, row, cfg, port, policy, stage)
        stage("paired analysis")
        with (output / "report.txt").open("x+") as summary:
            print(
                f"Baseline  {revisions['baseline']}\nCandidate {revisions['candidate']}\n"
                f"GET {cfg['endpoint']} ({cfg['workload_id']})",
                file=summary,
                flush=True,
            )
            code = run(
                [engine, "analyze", "manifest.json", "--json", "report.json"],
                output,
                120,
                stdout=summary,
            )
            summary.seek(0)
            print(summary.read(), end="")
        if code:
            raise StudyError("native analysis could not issue a decision (see report.json)", code)
        checkpoint()
        status.update(state="complete")
    except (StudyError, OSError, ValueError) as error:
        status.update(state="interrupted" if stop_signal else "failed", error=str(error))
        print(f"{status['stage']}: {error}", file=sys.stderr)
        return 128 + stop_signal if stop_signal else getattr(error, "code", 1)
    finally:
        write_json(output / "status.json", status)
    return 0


def main():
    parser = argparse.ArgumentParser(
        prog="spe study",
        description="Compare two trusted local Git revisions with paired GET workloads.",
    )
    parser.add_argument("--baseline", required=True, metavar="REV")
    parser.add_argument("--candidate", required=True, metavar="REV")
    parser.add_argument("--config", required=True, type=Path, help="explicit trusted JSON configuration")
    parser.add_argument(
        "--output", type=Path, help="new results directory (default: unique directory in system temp)"
    )
    args = parser.parse_args(sys.argv[2:])
    try:
        require(sys.version_info >= (3, 10), "spe study requires Python 3.10 or newer")
        engine = shutil.which(sys.argv[1])
        require(engine is not None, "could not locate the calling spe executable")
        cfg = load_config(args.config)
        if args.output:
            output = args.output.resolve()
            output.mkdir(mode=0o700)
        else:
            output = Path(tempfile.mkdtemp(prefix="spe-study-results-")).resolve()
        print(f"Study results: {output}", flush=True)
        return study(args, Path(engine).resolve(), cfg, output)
    except (OSError, StudyError) as error:
        print(f"spe study: {error}", file=sys.stderr)
        return getattr(error, "code", 1)


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    sys.exit(main())
