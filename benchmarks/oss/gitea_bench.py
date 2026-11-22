"""Measure unchanged Gitea on seeded Git objects and count Git starts in a separate diagnostic."""

import argparse
import base64
from concurrent.futures import ThreadPoolExecutor
import getpass
import json
import os
from pathlib import Path
import secrets
import signal
import subprocess
import time

from benchlib import (
    compare,
    cpus,
    digest,
    encode,
    launch,
    new_directories,
    port_number,
    request,
    save,
    spe_trial,
)

VERSION = "1.24.6"
BINARY_SHA = "bb09d565d7e22ed07a9525d22e6f7c468ec00b58484a9691d61ff70940452676"
OWNER = "catalogbench"
REPOS = ("warehouse-catalog", "warehouse-ledger")
BLOB = "region-00/stock-000.tsv"


def command(binary, state, *args):
    return [str(binary), "--work-path", str(state), "--config", str(state / "app.ini"), *args]


def environment():
    return dict(
        os.environ,
        GOMAXPROCS="2",
        GOMEMLIMIT="256MiB",
        GIT_TERMINAL_PROMPT="0",
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL="/dev/null",
    )


def server(binary, state, port, log, trace=None):
    env = dict(environment(), GIT_CONFIG_GLOBAL=str(state / "gitea.gitconfig"))
    if trace is not None:
        env["GIT_TRACE2_EVENT"] = str(trace)
    return launch(command(binary, state, "web"), state, env, port, "/api/v1/version", log, children=True)


def configure(state, port):
    path = state / "app.ini"
    path.write_text(f"""APP_NAME = SPE local validation
RUN_USER = {getpass.getuser()}
RUN_MODE = prod
[repository]
ROOT = {state}/repositories
DEFAULT_BRANCH = main
[server]
HTTP_ADDR = 127.0.0.1
HTTP_PORT = {port}
ROOT_URL = http://127.0.0.1:{port}/
APP_DATA_PATH = {state}/data
OFFLINE_MODE = true
DISABLE_SSH = true
SSH_ROOT_PATH = {state}/ssh
SSH_CREATE_AUTHORIZED_KEYS_FILE = false
SSH_CREATE_AUTHORIZED_PRINCIPALS_FILE = false
[database]
DB_TYPE = sqlite3
PATH = {state}/gitea.db
[security]
INSTALL_LOCK = true
SECRET_KEY = {secrets.token_hex(32)}
INTERNAL_TOKEN = {secrets.token_hex(64)}
[service]
DISABLE_REGISTRATION = true
[picture]
DISABLE_GRAVATAR = true
ENABLE_FEDERATED_AVATAR = false
[cron]
ENABLED = false
[actions]
ENABLED = false
[log]
MODE = console
LEVEL = Info
ROOT_PATH = {state}/log
logger.router.MODE =
logger.access.MODE = console
ACCESS_LOG_TEMPLATE = SPE_RECEIPT {{{{.Ctx.Req.Method}}}} {{{{.Ctx.Req.URL.Path}}}} {{{{.ResponseWriter.Status}}}}
""")
    path.chmod(0o600)


def git(repo, *args, env=None):
    result = subprocess.run(
        ["git", "-C", str(repo), *args], env=env or environment(), capture_output=True, timeout=25
    )
    if result.returncode:
        raise RuntimeError(result.stderr.decode(errors="replace")[:400])
    return result.stdout


def stock(repo, n, revision):
    return f"item\twarehouse\tquantity\trevision\n{repo}-{n:03d}\t{n % 16}\t{(17 * n + revision) % 997}\t{revision}\n"


def seed_repo(state, port, repo, authorization):
    request(
        port,
        "/api/v1/user/repos",
        "POST",
        encode(dict(name=repo, default_branch="main", private=False)),
        {"Content-Type": "application/json", "Authorization": authorization},
    )
    work = state / ("client-" + repo)
    work.mkdir()
    git(work, "init", "--initial-branch=main", "--object-format=sha1")
    (work / "README.md").write_text(f"# {repo}\n\nWeekly regional inventory in tab-separated files.\n")
    for revision in range(16):
        # 128 product files and an inventory update on each subsequent commit.
        changes = range(128) if revision == 0 else range(revision % 4, 128, 19)
        for n in changes:
            file = work / f"region-{n % 16:02d}" / f"stock-{n:03d}.tsv"
            file.parent.mkdir(exist_ok=True)
            file.write_text(stock(repo, n, revision))
        date = f"2025-01-{revision + 1:02d}T12:00:00Z"
        env = dict(
            environment(),
            GIT_AUTHOR_NAME="Inventory Fixture",
            GIT_COMMITTER_NAME="Inventory Fixture",
            GIT_AUTHOR_EMAIL="fixture@example.invalid",
            GIT_COMMITTER_EMAIL="fixture@example.invalid",
            GIT_AUTHOR_DATE=date,
            GIT_COMMITTER_DATE=date,
        )
        git(work, "add", ".")
        git(
            work,
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-m",
            f"Inventory snapshot {revision:02d}",
            env=env,
        )
    env = dict(
        environment(),
        GIT_CONFIG_COUNT="1",
        GIT_CONFIG_KEY_0="http.extraHeader",
        GIT_CONFIG_VALUE_0="Authorization: " + authorization,
    )
    git(work, "push", f"http://127.0.0.1:{port}/{OWNER}/{repo}.git", "main", env=env)
    advertised = git(
        work, "ls-remote", f"http://127.0.0.1:{port}/{OWNER}/{repo}.git", "refs/heads/main"
    ).split()
    if not advertised or advertised[0] != git(work, "rev-parse", "HEAD").strip():
        raise RuntimeError("Gitea did not advertise the seeded commit")
    return dict(
        head=git(work, "rev-parse", "HEAD").decode().strip(),
        tree=git(work, "rev-parse", "HEAD^{tree}").decode().strip(),
        blob=git(work, "rev-parse", "HEAD:" + BLOB).decode().strip(),
        commits=int(git(work, "rev-list", "--count", "HEAD")),
    )


def paths(identities):
    return [
        f"/api/v1/repos/{OWNER}/{REPOS[0]}/contents/{BLOB}",
        f"/api/v1/repos/{OWNER}/{REPOS[1]}/git/trees/{identities[REPOS[1]]['tree']}",
    ]


def check_objects(port, state, identities, endpoints):
    blob = request(port, endpoints[0])
    expected = git(state / ("client-" + REPOS[0]), "show", "HEAD:" + BLOB)
    if (
        blob["type"] != "file"
        or blob["sha"] != identities[REPOS[0]]["blob"]
        or blob["encoding"] != "base64"
        or base64.b64decode(blob["content"]) != expected
    ):
        raise RuntimeError("Gitea contents differ from local Git blob")
    tree = request(port, endpoints[1])
    entries = {}
    for row in (
        git(state / ("client-" + REPOS[1]), "ls-tree", "-z", "HEAD").decode().strip("\0").split("\0")
    ):
        metadata, name = row.split("\t")
        mode, kind, oid = metadata.split()
        entries[name] = (kind, oid)
    actual = {e["path"]: (e["type"], e["sha"]) for e in tree["tree"]}
    if (
        tree["truncated"]
        or tree["sha"] != identities[REPOS[1]]["tree"]
        or actual != entries
        or len(actual) != 17
    ):
        raise RuntimeError("Gitea directory listing differs from local Git tree")
    return dict(blob_sha=blob["sha"], bytes=len(expected), tree_sha=tree["sha"], entries=len(actual))


def receipts(log, offset, endpoints, expected_status=200):
    counts = {path: 0 for path in endpoints}
    with log.open("rb") as f:
        f.seek(offset)
        for line in f:
            if b"SPE_RECEIPT GET " not in line:
                continue
            path, status = line.decode().split("SPE_RECEIPT GET ", 1)[1].strip().split()[:2]
            if path in counts:
                if int(status) != expected_status:
                    raise RuntimeError("Unexpected Gitea access-log status")
                counts[path] += 1
    return counts


def pause_after_receipt(app, log, offset, endpoints):
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        if any(receipts(log, offset, endpoints).values()):
            break
        time.sleep(0.005)
    else:
        raise RuntimeError("First request did not reach the application")
    start = time.monotonic()
    os.kill(app.proc.pid, signal.SIGSTOP)
    try:
        time.sleep(0.32)
    finally:
        os.kill(app.proc.pid, signal.SIGCONT)
    return time.monotonic() - start


def measure_pause(binary, state, port, endpoints, engine, out):
    """Temporarily hold the one local service slot. Keep this diagnostic out of timing comparisons."""
    log = state / "pause.log"
    rows = []
    with server(binary, state, port, log) as app, ThreadPoolExecutor(max_workers=1) as pool:
        for label in ("A", "B"):
            for path in endpoints:
                request(port, path)
            offset = log.stat().st_size
            raw = out / f"pause-{label}.json"
            future = (
                pool.submit(pause_after_receipt, app, log, offset, endpoints) if label == "B" else None
            )
            flags = [
                "--workload-file",
                state / "mix.json",
                "--rate",
                "20",
                "--duration-ms",
                "2000",
                "--concurrency",
                "1",
            ]
            doc, stats = spe_trial(engine, port, "", flags, raw, app)
            seconds = future.result() if future is not None else 0
            seen = receipts(log, offset, endpoints)
            if seen != {e["path"]: e["counts"]["started"] for e in doc["endpoint_results"]}:
                raise RuntimeError("Pause-control receipts disagree with the client")
            if label == "B" and stats["scheduling"]["missed_capacity"] == 0:
                raise RuntimeError("The held service slot did not cause a capacity miss")
            rows.append(
                dict(label=label, raw=raw.name, server_pause_seconds=seconds, receipts=seen, **stats)
            )
    report = compare(engine, rows, out / "compare-pause.json")
    if any(m["change_percent"] is not None for m in report["metrics"].values()):
        raise RuntimeError("Unexpected native delta for a partial schedule")
    save(out / "pause-diagnostic.json", rows)


def diagnostic(binary, state, port, endpoints, engine, out):
    trace = state / "git-trace2.jsonl"
    samples = []
    with server(binary, state, port, state / "diagnostic.log", trace) as app:
        for path in (None, *endpoints):
            start = trace.stat().st_size if trace.exists() else 0
            if path is None:
                time.sleep(0.4)
            else:
                for _ in range(6):
                    request(port, path)
            time.sleep(0.1)
            commands = {}
            with trace.open("rb") as f:
                f.seek(start)
                for line in f:
                    event = json.loads(line)
                    if event.get("event") == "cmd_name":
                        name = event["name"]
                        commands[name] = commands.get(name, 0) + 1
                    if event.get("event") == "start":
                        argv = event.get("argv", [])
                        if "cat-file" in argv:
                            flags = [
                                a for a in argv[argv.index("cat-file") + 1 :] if a.startswith("--batch")
                            ]
                            for flag in flags:
                                name = "cat-file " + flag.split("=")[0]
                                commands[name] = commands.get(name, 0) + 1
            samples.append(dict(path=path, requests=0 if path is None else 6, git_commands=commands))
        rows = []
        for label in ("A", "B"):
            raw = out / f"missing-{label}.json"
            path = endpoints[0] + ".absent"
            offset = (state / "diagnostic.log").stat().st_size
            doc, _ = spe_trial(engine, port, path, ["--requests", "2", "--concurrency", "1"], raw, app)
            if (
                doc["http_status_counts"] != {"404": 2}
                or doc["counts"]["http_errors"] != 2
                or receipts(state / "diagnostic.log", offset, [path], 404)[path] != 2
            ):
                raise RuntimeError("Missing Git object did not yield two correctly classified 404s")
            rows.append(dict(raw=raw.name, label=label))
        report = compare(engine, rows, out / "compare-errors.json")
        if any(m["change_percent"] is not None for m in report["metrics"].values()):
            raise RuntimeError("Unexpected native delta for an all-error workload")
    save(out / "git-diagnostic.json", samples)


def main(args):
    binary = args.binary.resolve()
    engine = args.engine.resolve()
    if digest(binary) != BINARY_SHA:
        raise RuntimeError("Gitea binary SHA-256 mismatch")
    state, out = new_directories(args.output.resolve())
    port = port_number()
    configure(state, port)
    password = secrets.token_urlsafe(28)
    auth = "Basic " + base64.b64encode((OWNER + ":" + password).encode()).decode()
    with (state / "migration.log").open("wb") as log:
        for cmd in (
            ["migrate"],
            [
                "admin",
                "user",
                "create",
                "--username",
                OWNER,
                "--email",
                "fixture@example.invalid",
                "--password",
                password,
                "--must-change-password=false",
            ],
        ):
            result = subprocess.run(
                command(binary, state, *cmd),
                env=environment(),
                cwd=state,
                stdout=log,
                stderr=log,
                timeout=45,
            )
            if result.returncode:
                raise RuntimeError(f"Gitea initialization failed. See {state}/migration.log")
    with server(binary, state, port, state / "seed.log"):
        if request(port, "/api/v1/version")["version"] != VERSION:
            raise RuntimeError("Wrong server version")
        identities = {repo: seed_repo(state, port, repo, auth) for repo in REPOS}
        endpoints = paths(identities)
        check_objects(port, state, identities, endpoints)
    mix = state / "mix.json"
    mix.write_bytes(encode(dict(endpoints=[dict(path=p, weight=w) for p, w in zip(endpoints, (3, 2))])))
    order = "ABBABAAB"
    disk_kib = int(subprocess.check_output(["du", "-sk", state], text=True).split()[0])
    if disk_kib > 256 * 1024:
        raise RuntimeError("Seeded Gitea state exceeds the 256 MiB study budget")
    save(
        out / "setup.json",
        dict(
            version=VERSION,
            binary_sha256=BINARY_SHA,
            spe_sha256=digest(engine),
            git_version=subprocess.check_output(["git", "--version"], text=True).strip(),
            repos=identities,
            endpoints=endpoints,
            mix_sha256=digest(mix),
            order=order,
            cpus=cpus(),
            port=port,
            disk_kib=disk_kib,
        ),
    )
    rows = []
    for block, label in enumerate(order, 1):
        log = state / f"block-{block}.log"
        with server(binary, state, port, log) as app:
            before = check_objects(port, state, identities, endpoints)
            for path in endpoints:
                request(port, path)
            offset = log.stat().st_size
            raw = out / f"block-{block}-{label}.json"
            flags = [
                "--workload-file",
                mix,
                "--rate",
                "20",
                "--duration-ms",
                "3000",
                "--concurrency",
                "2",
            ]
            doc, stats = spe_trial(engine, port, "", flags, raw, app)
            counts = receipts(log, offset, endpoints)
            engine_counts = {e["path"]: e["counts"]["started"] for e in doc["endpoint_results"]}
            if counts != engine_counts or doc["counts"]["successes"] != doc["counts"]["started"]:
                raise RuntimeError(f"Mixed server receipts differ from SPE: {counts}, {engine_counts}")
            if before != check_objects(port, state, identities, endpoints):
                raise RuntimeError("Git objects changed")
            rows.append(
                dict(
                    block=block,
                    label=label,
                    receipts=counts,
                    verified_objects=before,
                    raw=raw.name,
                    **stats,
                )
            )
        print(block, label, stats["successes"], flush=True)
        save(out / "trials.json", rows)
    compare(engine, rows, out / "compare-aa.json")
    diagnostic(binary, state, port, endpoints, engine, out)
    measure_pause(binary, state, port, endpoints, engine, out)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--binary", required=True, type=Path)
    p.add_argument("--engine", required=True, type=Path)
    p.add_argument("--output", required=True, type=Path)
    main(p.parse_args())
