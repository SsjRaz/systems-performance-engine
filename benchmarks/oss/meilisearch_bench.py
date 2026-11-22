"""Prepare and measure two local Meilisearch releases using verified federated searches."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time

from benchlib import (
    client,
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

RELEASES = {
    "A": ("1.41.0", "7c94284f47dcbcb2950f5dcb154c396be7157e16d4f4dd600109f3587247dded"),
    "B": ("1.42.0", "1c392f956ded270c40ed06bca9c9bb7a75611b9df5d54b20f2f58f1d26454a7d"),
}
INDEXES = ("catalog_a", "catalog_b")
DOCUMENTS = 100_000
HEADERS = {"Content-Type": "application/json"}
KINDS = ("tea", "coffee", "pasta", "granola", "biscuits", "jam", "rice")
ALLERGENS = ("Gluten", "Lactose", "Nuts", "Soy")
K6_SHA = "6909fd505103a4f19652a2319c2f737258cc3b3227425a648d1e07f309e5ec9d"


def document(n):
    kind = KINDS[n % len(KINDS)]
    return dict(
        articleNumber=str(n),
        title=f"{kind} package {n}",
        description=f"Shelf-stable {kind} from district {n % 43} for the seasonal catalog.",
        allergens=[ALLERGENS[n % 4], ALLERGENS[(n + 1) % 4]],
    )


def body(size):
    predicate = "articleNumber IN [" + ", ".join(f"'{n}'" for n in range(1, size + 1)) + "]"
    return encode(
        dict(
            queries=[dict(indexUid=uid, q="", filter=predicate) for uid in INDEXES],
            federation=dict(limit=20),
        )
    )


def serve(tools, state, label, port, log):
    version, sha = RELEASES[label]
    binary = tools / f"meili-{version}"
    if digest(binary) != sha:
        raise RuntimeError(f"Wrong Meilisearch binary for {version}")
    db = state / f"db-{version}"
    return launch(
        [
            binary,
            "--db-path",
            db,
            "--http-addr",
            f"127.0.0.1:{port}",
            "--env",
            "development",
            "--no-analytics",
            "--max-indexing-memory",
            "201326592",
            "--max-indexing-threads",
            "1",
            "--log-level",
            "INFO",
        ],
        state,
        dict(os.environ, RAYON_NUM_THREADS="2"),
        port,
        "/health",
        log,
    )


def task(port, path, method, data):
    uid = request(port, path, method, data, HEADERS)["taskUid"]
    deadline = time.monotonic() + 75
    while time.monotonic() < deadline:
        item = request(port, f"/tasks/{uid}")
        if item["status"] == "succeeded":
            return dict(uid=uid, type=item["type"], status=item["status"], details=item["details"])
        if item["status"] in ("failed", "canceled"):
            raise RuntimeError(str(item))
        time.sleep(0.05)
    raise RuntimeError(f"Task {uid} did not finish")


def verify_hits(port, size):
    data = request(port, "/multi-search", "POST", body(size), HEADERS)
    hits = data["hits"]
    seen = set()
    for item in hits:
        n = int(item["articleNumber"])
        uid = item["_federation"]["indexUid"]
        if uid not in INDEXES or not 1 <= n <= size or any(item[k] != v for k, v in document(n).items()):
            raise RuntimeError("Unexpected hit contents, source index or filtered article number")
        seen.add((uid, n))
    if (
        data["estimatedTotalHits"] != 2 * size
        or len(seen) != len(hits)
        or len(hits) != min(20, 2 * size)
    ):
        raise RuntimeError(
            f"Unexpected federated answer: total={data['estimatedTotalHits']}, hits={len(hits)}"
        )
    if size <= 10 and seen != {(uid, n) for uid in INDEXES for n in range(1, size + 1)}:
        raise RuntimeError("Small complete result differs from independently enumerated set")
    return dict(
        size=size,
        hits=len(hits),
        estimated_total=data["estimatedTotalHits"],
        processing_ms=data["processingTimeMs"],
        ids=[list(pair) for pair in sorted(seen)],
    )


def prepare(args):
    state, out = new_directories(args.output)
    port = port_number()
    versions = {}
    for label, (version, _) in RELEASES.items():
        log = state / f"prepare-{version}.log"
        with serve(args.tools, state, label, port, log):
            actual = request(port, "/version")
            if actual["pkgVersion"] != version:
                raise RuntimeError("Wrong server version")
            tasks, data_hashes = [], {}
            for uid in INDEXES:
                tasks.append(
                    task(port, "/indexes", "POST", encode(dict(uid=uid, primaryKey="articleNumber")))
                )
                tasks.append(
                    task(
                        port,
                        f"/indexes/{uid}/settings",
                        "PATCH",
                        encode(
                            dict(
                                filterableAttributes=["articleNumber"],
                                pagination=dict(maxTotalHits=5000),
                            )
                        ),
                    )
                )
                hash_input = hashlib.sha256()
                for begin in range(1, DOCUMENTS + 1, 5000):
                    batch = encode([document(n) for n in range(begin, begin + 5000)])
                    hash_input.update(batch)
                    done = task(port, f"/indexes/{uid}/documents", "POST", batch)
                    if done["details"]["indexedDocuments"] != 5000:
                        raise RuntimeError("Incomplete document batch")
                    tasks.append(done)
                data_hashes[uid] = hash_input.hexdigest()
                stats = request(port, f"/indexes/{uid}/stats")
                if stats["numberOfDocuments"] != DOCUMENTS or stats["isIndexing"]:
                    raise RuntimeError("Index is not ready")
            semantics = [verify_hits(port, size) for size in (3, 100, 1000)]
            # A nonempty full-text query checks indexing separately from the filter benchmark.
            query = request(
                port, "/indexes/catalog_a/search", "POST", encode(dict(q="granola", limit=3)), HEADERS
            )
            if len(query["hits"]) != 3 or any("granola" not in h["title"] for h in query["hits"]):
                raise RuntimeError("Full-text indexing check failed")
            versions[label] = dict(
                version=actual, tasks=tasks, dataset_sha256=data_hashes, semantic_checks=semantics
            )
        print("Indexed", version, DOCUMENTS, "documents in each index", flush=True)
    if versions["A"]["dataset_sha256"] != versions["B"]["dataset_sha256"]:
        raise RuntimeError("Unequal indexed input")
    for size in (100, 1000):
        path = state / f"filter-{size}.json"
        path.write_bytes(body(size))
        path.chmod(0o600)
    headers = state / "headers.json"
    headers.write_bytes(encode([dict(name="Content-Type", value="application/json")]))
    headers.chmod(0o600)
    disk_kib = int(subprocess.check_output(["du", "-sk", state], text=True).split()[0])
    if disk_kib > 2 * 1024 * 1024:
        raise RuntimeError("Database footprint exceeds the two-GiB study budget")
    save(
        out / "setup.json",
        dict(
            port=port,
            versions=versions,
            documents_per_index=DOCUMENTS,
            inputs={
                str(size): dict(bytes=len(body(size)), sha256=hashlib.sha256(body(size)).hexdigest())
                for size in (100, 1000, 5000)
            },
            header_sha256=digest(headers),
            disk_kib=disk_kib,
        ),
    )


def search_receipts(log, offset):
    with log.open("rb") as f:
        f.seek(offset)
        lines = [line for line in f if b"route=/multi-search " in line and b"method=POST " in line]
    if any(b"status_code=200" not in line for line in lines):
        raise RuntimeError("Meilisearch logged an unsuccessful search")
    return len(lines)


def measure(args):
    state, out = args.output / "state", args.output / "evidence"
    setup = json.loads((out / "setup.json").read_text())
    port = setup["port"]
    if digest(args.k6) != K6_SHA:
        raise RuntimeError("Wrong k6 release binary")
    rows, checks = [], []
    suite = out / "raw"
    suite.mkdir()
    plan = [("aa", "ABBA"), ("ab", "BAAB"), ("aa", "BAAB"), ("ab", "ABBA")]
    save(
        out / "design.json",
        dict(
            blocks=plan,
            cpus=cpus(),
            spe_sha256=digest(args.engine),
            k6_sha256=digest(args.k6),
            window_ms=3000,
            arrivals_per_second=6,
            closed_requests=24,
            concurrency=1,
        ),
    )
    for part, (phase, order) in enumerate(plan):
        for pos, label in enumerate(order):
            block = part * 4 + pos + 1
            binary = "A" if phase == "aa" else label
            name = f"{block:02d}-{phase}-{label}"
            log = state / (name + ".log")
            with serve(args.tools, state, binary, port, log) as app:
                if request(port, "/version")["pkgVersion"] != RELEASES[binary][0]:
                    raise RuntimeError("Unexpected version in measurement block")
                checks.append(
                    dict(
                        block=block,
                        when="before",
                        answers=[verify_hits(port, s) for s in (3, 100, 1000)],
                    )
                )
                cases = [("short", 100), ("large", 1000), ("paced", 1000), ("k6", 1000)]
                if pos % 2:
                    cases.reverse()
                for case, size in cases:
                    path = state / f"filter-{size}.json"
                    if (
                        digest(path) != setup["inputs"][str(size)]["sha256"]
                        or digest(state / "headers.json") != setup["header_sha256"]
                    ):
                        raise RuntimeError("Private inputs changed since indexing")
                    verify_hits(port, size)  # constant warm-up immediately before each tool
                    offset = log.stat().st_size
                    raw = suite / f"{name}-{case}.json"
                    if case == "k6":
                        env = dict(
                            os.environ,
                            K6_NO_USAGE_REPORT="true",
                            K6_NO_COLOR="true",
                            BENCH_PORT=str(port),
                            BENCH_BODY=str(path),
                            BENCH_SUMMARY=str(raw),
                        )
                        cmd = [
                            args.k6,
                            "run",
                            "--quiet",
                            "--address",
                            "127.0.0.1:0",
                            Path(__file__).with_name("meili_reference.js"),
                        ]
                        run, stats = app.measured(lambda: client(cmd, env))
                        if run.returncode:
                            raise RuntimeError(run.stderr)
                        result = json.loads(raw.read_text())
                        if result["valid"] != 24 or result["requests"] != 24:
                            raise RuntimeError("k6 content checks failed")
                        stats.update(result)
                        started = result["requests"]
                    else:
                        flags = [
                            "--method",
                            "POST",
                            "--body-file",
                            path,
                            "--headers-file",
                            state / "headers.json",
                            "--concurrency",
                            "1",
                        ]
                        flags += (
                            ["--rate", "6", "--duration-ms", "3000"]
                            if case == "paced"
                            else ["--requests", "24"]
                        )
                        doc, stats = spe_trial(args.engine, port, "/multi-search", flags, raw, app)
                        started = doc["counts"]["started"]
                        if doc["counts"]["successes"] != started:
                            raise RuntimeError("Search HTTP error")
                    count = search_receipts(log, offset)
                    if count != started:
                        raise RuntimeError(
                            f"Application request log contains {count} searches for {started} starts; see {log}"
                        )
                    rows.append(
                        dict(
                            block=block,
                            phase=phase,
                            label=label,
                            version=RELEASES[binary][0],
                            case=case,
                            receipts=count,
                            raw=str(raw.relative_to(out)),
                            **stats,
                        )
                    )
                checks.append(
                    dict(
                        block=block, when="after", answers=[verify_hits(port, s) for s in (3, 100, 1000)]
                    )
                )
            print(name, RELEASES[binary][0], flush=True)
            save(out / "trials.json", rows)
            save(out / "semantic-checks.json", checks)
    for phase, _ in plan[:2]:
        for case in ("short", "large", "paced"):
            group = [r for r in rows if r["phase"] == phase and r["case"] == case]
            compare(
                args.engine,
                group,
                out / f"compare-{phase}-{case}.json",
                ["--allow-redacted-headers", "--allow-redacted-body"],
            )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "measure"))
    parser.add_argument("--tools", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--engine", type=Path)
    parser.add_argument("--k6", type=Path)
    args = parser.parse_args()
    args.tools = args.tools.resolve()
    args.output = args.output.resolve()
    if args.action == "prepare":
        prepare(args)
    else:
        if not args.engine or not args.k6:
            parser.error("measure requires --engine and --k6")
        args.engine = args.engine.resolve()
        args.k6 = args.k6.resolve()
        measure(args)
