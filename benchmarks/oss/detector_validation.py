"""Predeclare paired SPE experiments, then execute bounded local fresh-process trials."""

import argparse
from contextlib import contextmanager, nullcontext
from http.server import BaseHTTPRequestHandler, HTTPServer
import itertools
import json
import multiprocessing as mp
import os
from pathlib import Path
import platform
import random
import time

from benchlib import client, cpus, digest, port_number, save
import gitea_bench as gitea
import meilisearch_bench as meili


# Locked before the held-out trials. One decision per campaign. Independent
# server starts are observations inside that decision.
SEED = 831947
PAIRS = 8
CAMPAIGNS = 8
CASES = ("unchanged", "cpu", "delay", "small", "contention")
DIAGNOSTICS = ("short", "different-model", "different-rate", "missed", "wrong-answer")
STUDIES = ("meili-versions", "meili-unchanged", "gitea-unchanged")


def policy(path, name, rules):
    path.touch(mode=0o600, exist_ok=False)
    save(path, dict(policy_id=name, endpoints=rules))


def plan(root, name, number, versions, source, rng, private=False):
    folder = root / f"{name}-{number:02}"
    folder.mkdir()
    count = 4 if name == "short" else PAIRS
    order = [i % 2 == 0 for i in range(count)]
    rng.shuffle(order)
    blocks = []
    for n, baseline_first in enumerate(order, 1):
        arms = ["baseline", "candidate"]
        if not baseline_first:
            arms.reverse()
        rows = []
        for arm in arms:
            instance = f"{folder.name}-{n:02}-{arm}"
            rows.append(dict(arm=arm, instance=instance, result=instance + ".json"))
        blocks.append(dict(id=f"block-{n:02}", runs=rows))
    doc = dict(
        schema_version=1,
        experiment_id=folder.name,
        workload_id=source,
        baseline_version=versions[0],
        candidate_version=versions[1],
        metric="mean_latency_ms",
        meaningful_change_percent=10,
        private_inputs_confirmed=private,
        blocks=blocks,
    )
    save(folder / "manifest.json", doc)
    return folder


def native(engine, *args, expected=(0,)):
    run = client([engine, *args])
    if run.returncode not in expected:
        raise RuntimeError(f"SPE exit {run.returncode}: {run.stderr[-1000:]}")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    disable_nagle_algorithm = True
    timeout = 2

    def log_message(self, *_):
        pass

    def do_GET(self):
        self.server.received.value += 1
        time.sleep(self.server.delay)
        end = time.thread_time_ns() + self.server.cpu_ns
        while time.thread_time_ns() < end:
            pass
        payload = b'{"answer":0}' if self.server.wrong else b'{"answer":42}'
        self.send_response(200)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def service(port, cpu, delay, cpu_ns, wrong, received, ready, stop):
    os.sched_setaffinity(0, {cpu})
    with HTTPServer(("127.0.0.1", port), Handler) as server:
        server.timeout = 0.01
        server.delay, server.cpu_ns, server.wrong, server.received = (
            delay,
            cpu_ns,
            wrong,
            received,
        )
        ready.set()
        deadline = time.monotonic() + 12
        while not stop.is_set() and time.monotonic() < deadline:
            server.handle_request()


def burn(cpu, ready, stop):
    os.sched_setaffinity(0, {cpu})
    ready.set()
    deadline = time.monotonic() + 12
    while not stop.is_set() and time.monotonic() < deadline:
        for _ in range(500):
            pass


@contextmanager
def process(target, *args):
    ctx = mp.get_context("fork")
    ready, stop = ctx.Event(), ctx.Event()
    child = ctx.Process(target=target, args=(*args, ready, stop))
    child.start()
    try:
        if not ready.wait(4):
            raise RuntimeError("local fixture startup failed")
        yield child.pid
    finally:
        stop.set()
        child.join(3)
        if child.is_alive():
            child.terminate()
            child.join(2)
        if child.exitcode != 0:
            raise RuntimeError("local fixture did not exit cleanly")


def fixture_run(engine, name, arm, port, checks, out):
    candidate = arm == "candidate"
    cpu = cpus()[1][0]
    delay = (
        0.0015
        + (0.003 if name == "delay" and candidate else 0)
        + (0.00007 if name == "small" and candidate else 0)
    )
    work = 300_000 + (2_500_000 if name == "cpu" and candidate else 0)
    flags = ["--requests", "12"]
    if name == "different-model" and candidate:
        flags = ["--rate", "50", "--duration-ms", "240"]
    if name == "different-rate":
        flags = (
            ["--rate", "50", "--duration-ms", "240"]
            if candidate
            else ["--rate", "25", "--duration-ms", "480"]
        )
    if name == "missed":
        flags = ["--rate", "100", "--duration-ms", "120"]
        if candidate:
            delay += 0.030
    received = mp.get_context("fork").Value("i", 0)
    with process(burn, cpu) if name == "contention" else nullcontext():
        with process(
            service,
            port,
            cpu,
            delay,
            work,
            name == "wrong-answer" and candidate,
            received,
        ) as pid:
            native(
                engine,
                f"http://127.0.0.1:{port}/work",
                *flags,
                "--concurrency",
                "1",
                "--verify-response",
                checks,
                "--json",
                out,
                expected=(0, 3),
            )
    doc = json.loads(out.read_text())
    probes, timed = doc["verification"]["attempted"], doc["counts"]["started"]
    if received.value != probes + timed:
        raise RuntimeError("fixture server receipts differ from SPE")
    return dict(pid=pid, server_receipts=received.value, probes=probes, timed=timed)


def app_run(args, name, arm, out, checks, setup, port, log):
    engine, tools = args.engine, args.tools
    if name.startswith("meili"):
        state = args.meili / "state"
        label = "B" if name == "meili-versions" and arm == "candidate" else "A"
        with meili.serve(tools, state, label, port, log) as app:
            if (
                digest(state / "filter-1000.json") != setup["inputs"]["1000"]["sha256"]
                or digest(state / "headers.json") != setup["header_sha256"]
            ):
                raise RuntimeError("private input snapshots changed")
            meili.verify_hits(port, 1000)
            offset = log.stat().st_size
            native(
                engine,
                f"http://127.0.0.1:{port}/multi-search",
                "--requests",
                "24",
                "--concurrency",
                "1",
                "--method",
                "POST",
                "--headers-file",
                state / "headers.json",
                "--body-file",
                state / "filter-1000.json",
                "--replay-safe-post",
                "--verify-response",
                checks,
                "--json",
                out,
            )
            count = meili.search_receipts(log, offset)
            meili.verify_hits(port, 1000)
            pid = app.proc.pid
    else:
        state = args.gitea / "state"
        endpoints = setup["endpoints"]
        if digest(state / "mix.json") != setup["mix_sha256"]:
            raise RuntimeError("Gitea mix changed")
        binary = tools / f"gitea-{gitea.VERSION}"
        if digest(binary) != gitea.BINARY_SHA:
            raise RuntimeError("Gitea release identity changed")
        with gitea.server(binary, state, port, log) as app:
            gitea.check_objects(port, state, setup["repos"], endpoints)
            offset = log.stat().st_size
            native(
                engine,
                f"http://127.0.0.1:{port}",
                "--rate",
                "20",
                "--duration-ms",
                "1500",
                "--concurrency",
                "2",
                "--workload-file",
                state / "mix.json",
                "--verify-response",
                checks,
                "--json",
                out,
                expected=(0, 3),
            )
            by_path = gitea.receipts(log, offset, endpoints)
            run = json.loads(out.read_text())
            if any(
                by_path[e["path"]] != e["counts"]["started"] + 1
                for e in run["endpoint_results"]
            ):
                raise RuntimeError(
                    "Gitea endpoint receipts differ from timed reads plus preflight"
                )
            count = sum(by_path.values())
            gitea.check_objects(port, state, setup["repos"], endpoints)
            pid = app.proc.pid
    run = json.loads(out.read_text())
    timed, probes = run["counts"]["started"], run["verification"]["attempted"]
    if count != timed + probes:
        raise RuntimeError("application receipts differ from SPE")
    return dict(pid=pid, server_receipts=count, probes=probes, timed=timed)


def main(args):
    root = args.output.resolve()
    root.mkdir(parents=True, exist_ok=False)
    args.engine = args.engine.resolve()
    rng = random.Random(SEED)
    campaigns = []
    app_setup = {}
    if args.mode == "fixture":
        for number in range(1, CAMPAIGNS + 1):
            for case in CASES:
                candidate = case if case in ("cpu", "delay", "small") else "baseline"
                campaigns.append(
                    (
                        case,
                        plan(
                            root,
                            case,
                            number,
                            ("baseline", candidate),
                            "fixture-v1",
                            rng,
                        ),
                    )
                )
        for case in DIAGNOSTICS:
            campaigns.append(
                (case, plan(root, case, 1, ("baseline", case), "fixture-v1", rng))
            )
        policy(root / "checks.json", "fixture-answer-v1", [{"fields": {"/answer": 42}}])
    else:
        if not all((args.meili, args.gitea, args.tools)):
            raise ValueError("apps mode requires --meili, --gitea and --tools")
        args.tools, args.meili, args.gitea = (
            p.resolve() for p in (args.tools, args.meili, args.gitea)
        )
        app_setup = {
            kind: json.loads((path / "evidence/setup.json").read_text())
            for kind, path in (("meili", args.meili), ("gitea", args.gitea))
        }
        ids = app_setup["gitea"]["repos"]
        policy(
            root / "gitea-checks.json",
            "git-objects-v1",
            [
                {"fields": {"/sha": ids[gitea.REPOS[0]]["blob"], "/type": "file"}},
                {"fields": {"/sha": ids[gitea.REPOS[1]]["tree"], "/truncated": False}},
            ],
        )
        policy(
            root / "meili-checks.json",
            "federated-catalog-v1",
            [{"fields": {"/estimatedTotalHits": 2000}}],
        )
        for name in STUDIES:
            search = name.startswith("meili")
            versions = (
                ("1.41.0", "1.42.0" if name == "meili-versions" else "1.41.0")
                if search
                else (gitea.VERSION, gitea.VERSION)
            )
            campaigns.append(
                (
                    name,
                    plan(
                        root,
                        name,
                        1,
                        versions,
                        "catalog-1000" if search else "git-objects",
                        rng,
                        private=search,
                    ),
                )
            )
    rng.shuffle(campaigns)
    # Freeze every manifest, including the control cases, before the first response.
    save(
        root / "design.json",
        dict(
            seed=SEED,
            engine_sha256=digest(args.engine),
            driver_sha256=digest(Path(__file__)),
            platform=platform.platform(),
            cpus=cpus(),
            order=[p.name for _, p in campaigns],
            manifests={p.name: digest(p / "manifest.json") for _, p in campaigns},
        ),
    )
    results = []
    for case, folder in campaigns:
        manifest = json.loads((folder / "manifest.json").read_text())
        port = port_number()
        observations = []
        for row in itertools.chain.from_iterable(b["runs"] for b in manifest["blocks"]):
            out = folder / row["result"]
            if args.mode == "fixture":
                observation = fixture_run(
                    args.engine, case, row["arm"], port, root / "checks.json", out
                )
            else:
                app = case.split("-")[0]
                setup = app_setup[app]
                observation = app_run(
                    args,
                    case,
                    row["arm"],
                    out,
                    root / f"{app}-checks.json",
                    setup,
                    setup["port"],
                    folder / (row["instance"] + ".log"),
                )
            observations.append(dict(instance=row["instance"], **observation))
        save(folder / "receipts.json", observations)
        native(
            args.engine,
            "analyze",
            folder / "manifest.json",
            "--json",
            folder / "analysis.json",
            expected=(0, 2, 3),
        )
        analysis = json.loads((folder / "analysis.json").read_text())
        results.append(
            dict(
                campaign=folder.name,
                case=case,
                verdict=analysis["verdict"],
                reason=analysis["reason"],
                effect=analysis["effect_percent"],
                interval=analysis["interval_percent"],
                timed=sum(r["timed"] for r in observations),
                probes=sum(r["probes"] for r in observations),
            )
        )
        save(root / "results.json", results)
        print(folder.name, analysis["verdict"], analysis["reason"], flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("fixture", "apps"))
    for arg in ("engine", "output", "meili", "gitea", "tools"):
        parser.add_argument("--" + arg, type=Path, required=arg in ("engine", "output"))
    main(parser.parse_args())
