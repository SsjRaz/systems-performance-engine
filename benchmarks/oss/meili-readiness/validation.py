"""Bounded semantic, indexing, secondary timing and search-stage checks on Linux."""

import argparse
from contextlib import closing
import hashlib
import http.client
import json
import os
from pathlib import Path
import random
import re
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import meili_profile as profile
import meilisearch_bench as meili
from benchlib import client, cpus, encode, port_number, request

N = 512
INDEXES = ("north", "south")
SKUS = ("cardamom", "café noir", "o'brien", "red tea", "陶器", "녹차", "rye", "oats")
LABELS = ("orchard", "東京の緑茶", "서울의 녹차", "summer oats")
SEARCHES = 128


def signature(value):
    data = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(data.encode()).hexdigest()


def stable(data):
    return {
        k: v for k, v in data.items() if k not in ("processingTimeMs", "requestUid")
    }


def documents(uid):
    shift = INDEXES.index(uid)
    return [
        dict(
            id=i,
            sku=SKUS[(i + shift) % len(SKUS)],
            price=(i + shift) % 17,
            title=LABELS[(i + shift) % len(LABELS)],
            shelf=uid,
        )
        for i in range(N)
    ]


def call(port, path, query):
    with closing(http.client.HTTPConnection("127.0.0.1", port, timeout=12)) as conn:
        conn.request("POST", path, encode(query), meili.HEADERS)
        response = conn.getresponse()
        raw = response.read(4_194_305)
        if len(raw) > 4_194_304:
            raise ValueError("response exceeds study limit")
        return response.status, json.loads(raw)


def semantic_suite(port, corpus, default_features):
    checks = {}
    filters = [
        ("empty", "sku IN []", lambda d: False),
        ("one", "id IN [8]", lambda d: d["id"] == 8),
        ("repeated", "id IN [2, 2, 99, 600]", lambda d: d["id"] in (2, 99)),
        (
            "unicode",
            'sku IN ["café noir", "陶器", "녹차"]',
            lambda d: d["sku"] in SKUS[1:2] + SKUS[4:6],
        ),
        ("escaped", r"sku IN ['o\'brien', 'red tea']", lambda d: d["sku"] in SKUS[2:4]),
        (
            "boolean",
            "(price >= 14 OR price = 2) AND NOT sku = 'rye'",
            lambda d: (d["price"] >= 14 or d["price"] == 2) and d["sku"] != "rye",
        ),
        ("range", "price 7 TO 9", lambda d: 7 <= d["price"] <= 9),
        (
            "array",
            [["id = 4", "id = 9"], "price >= 2"],
            lambda d: d["id"] in (4, 9) and d["price"] >= 2,
        ),
        (
            "wider",
            "id IN [" + ", ".join(str(i) for i in range(40)) + "]",
            lambda d: d["id"] < 40,
        ),
    ]
    for name, predicate, accepts in filters:
        for route in (*INDEXES, "federated"):
            indexes = INDEXES if route == "federated" else (route,)
            if route == "federated":
                path = "/multi-search"
                query = dict(
                    federation=dict(limit=1000),
                    queries=[dict(indexUid=i, filter=predicate) for i in indexes],
                )
            else:
                path = f"/indexes/{route}/search"
                query = dict(filter=predicate, limit=1000)
            status, data = call(port, path, query)
            if status != 200:
                raise ValueError((name, route, status, data))
            wanted = {(i, d["id"]) for i in indexes for d in corpus[i] if accepts(d)}
            found = []
            for hit in data["hits"]:
                uid = hit["_federation"]["indexUid"] if route == "federated" else route
                doc = {k: v for k, v in hit.items() if k != "_federation"}
                assert doc == corpus[uid][doc["id"]]
                found.append((uid, doc["id"]))
            assert (
                set(found) == wanted
                and len(found) == len(wanted) == data["estimatedTotalHits"]
            )
            checks[name + "/" + route] = dict(
                status=status, total=len(found), reply=signature(stable(data))
            )
    for name, predicate in (
        ("missing-operand", "sku ="),
        ("unknown-field", "shelf = north"),
    ):
        for route in ("north", "federated"):
            path, body = ("/indexes/north/search", dict(filter=predicate))
            if route == "federated":
                path, body = (
                    "/multi-search",
                    dict(
                        federation={},
                        queries=[dict(indexUid="north", filter=predicate)],
                    ),
                )
            status, data = call(port, path, body)
            assert (
                status == 400
                and data["type"] == "invalid_request"
                and data["code"] == "invalid_search_filter"
            )
            checks[name + "/" + route] = dict(status=status, error=stable(data))
    # Query entire indexed terms and check the full returned documents. Shorter
    # Japanese and Korean terms additionally exercise default segmentation.
    for name, term, label in (
        ("english", "orchard", LABELS[0]),
        ("japanese", LABELS[1], LABELS[1]),
        ("korean", LABELS[2], LABELS[2]),
    ):
        status, data = call(port, "/indexes/north/search", dict(q=term, limit=N))
        assert status == 200 and data["hits"]
        assert all(d == corpus["north"][d["id"]] for d in data["hits"])
        expected = {d["id"] for d in corpus["north"] if d["title"] == label}
        assert expected <= {d["id"] for d in data["hits"]}
        checks[name] = dict(
            status=status,
            total=data["estimatedTotalHits"],
            reply=signature(stable(data)),
        )
    if default_features:
        for lang, term, label in (
            ("jpn", "緑茶", LABELS[1]),
            ("kor", "녹차", LABELS[2]),
        ):
            status, data = call(
                port, "/indexes/north/search", dict(q=term, locales=[lang], limit=N)
            )
            assert status == 200 and data["hits"]
            assert all(d == corpus["north"][d["id"]] for d in data["hits"])
            expected = {d["id"] for d in corpus["north"] if d["title"] == label}
            assert expected == {d["id"] for d in data["hits"]}
            checks["segmented-" + lang] = dict(
                status=status, total=len(expected), reply=signature(stable(data))
            )
    return checks


def duration_ms(value):
    match = re.fullmatch(r"PT(\d+(?:\.\d+)?)S", value)
    if not match:
        raise ValueError(value)
    return 1000 * float(match[1])


def upload(port, uid, batch):
    task = request(port, f"/indexes/{uid}/documents", "POST", batch, meili.HEADERS)[
        "taskUid"
    ]
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        info = request(port, f"/tasks/{task}")
        if info["status"] == "succeeded":
            assert info["details"]["indexedDocuments"] == N
            return {"documents": N, "task_ms": duration_ms(info["duration"])}
        if info["status"] in ("failed", "canceled"):
            raise RuntimeError(info)
        time.sleep(0.01)
    raise RuntimeError("indexing task timed out")


def index_window(args, arm, instance, port, corpus, verify):
    with tempfile.TemporaryDirectory(prefix="db-", dir=args.output) as tmp:
        state = Path(tmp)
        with meili.serve_binary(
            Path(arm["path"]), state, arm["version"], port, state / "app.log"
        ) as app:
            identity(app, arm, port)
            for uid in INDEXES:
                meili.task(
                    port, "/indexes", "POST", encode(dict(uid=uid, primaryKey="id"))
                )
                meili.task(
                    port,
                    f"/indexes/{uid}/settings",
                    "PATCH",
                    encode(
                        dict(
                            filterableAttributes=["id", "price", "sku"],
                            searchableAttributes=["title"],
                        )
                    ),
                )
            batches = {uid: encode(docs) for uid, docs in corpus.items()}
            before = profile.snapshot(app.proc.pid)
            tasks = [upload(port, uid, batches[uid]) for uid in INDEXES]
            after = profile.snapshot(app.proc.pid)
            row = dict(
                instance=instance,
                tasks=tasks,
                task_ms=sum(x["task_ms"] for x in tasks),
                wall_ms=(after[0] - before[0]) / 1e6,
                peak_rss_kib=after[3],
                cpu_ms=1000
                * (sum(after[1:3]) - sum(before[1:3]))
                / os.sysconf("SC_CLK_TCK"),
            )
            for uid in INDEXES:
                page = request(port, f"/indexes/{uid}/documents?limit={N}")
                assert (
                    page["total"] == N
                    and sorted(page["results"], key=lambda d: d["id"]) == corpus[uid]
                )
                stats = request(port, f"/indexes/{uid}/stats")
                assert stats["numberOfDocuments"] == N and not stats["isIndexing"]
            if verify:
                row["semantics"] = semantic_suite(port, corpus, args.default_features)
            return row


def identity(app, arm, port):
    assert os.path.samefile(f"/proc/{app.proc.pid}/exe", arm["path"])
    assert request(port, "/version")["pkgVersion"] == arm["version"]
    assert sorted(os.sched_getaffinity(app.proc.pid)) == cpus()[1]


def catalog_cases():
    one = json.loads(meili.body(1000))["queries"][0]["filter"]
    single = "/indexes/catalog_a/search"
    return {
        "short-federated": ("/multi-search", json.loads(meili.body(3))),
        "single-1000": (single, dict(q="", filter=one)),
        "equality": (single, dict(filter="articleNumber = '42'")),
        "ordinary-text": (single, dict(q="granola", limit=10)),
    }


def check_catalog(name, data):
    if name == "short-federated":
        meili.validate_hits(data, 3)
    else:
        hits = data["hits"]
        assert hits and all(h == meili.document(int(h["articleNumber"])) for h in hits)
        if name == "single-1000":
            assert data["estimatedTotalHits"] == 1000 and len(hits) == 20
            assert all(1 <= int(h["articleNumber"]) <= 1000 for h in hits)
        elif name == "equality":
            assert hits == [meili.document(42)] and data["estimatedTotalHits"] == 1
        elif name == "ordinary-text":
            assert len(hits) == 10 and all("granola" in h["title"] for h in hits)


def catalog_window(args, arm, instance, port, cases):
    state, out = args.fixture / "state", args.output
    log = out / (instance + ".log")
    with meili.serve_binary(Path(arm["path"]), state, arm["version"], port, log) as app:
        identity(app, arm, port)
        rows = {}
        for name, (route, query) in cases:
            body = encode(query)
            data = request(port, route, "POST", body, meili.HEADERS)
            check_catalog(name, data)
            checks = dict(
                policy_id="catalog-" + name,
                endpoints=[
                    dict(fields={"/estimatedTotalHits": data["estimatedTotalHits"]})
                ],
            )
            checkfile, bodyfile = (
                out / (name + "-checks.json"),
                out / (name + "-body.json"),
            )
            for path, content in ((checkfile, encode(checks)), (bodyfile, body)):
                if path.exists():
                    assert path.read_bytes() == content
                else:
                    path.write_bytes(content)
                    path.chmod(0o600)
            offset = log.stat().st_size
            native = out / (instance + "-" + name + "-native.json")
            result = client(
                [
                    args.engine,
                    f"http://127.0.0.1:{port}{route}",
                    "--method",
                    "POST",
                    "--replay-safe-post",
                    "--body-file",
                    bodyfile,
                    "--headers-file",
                    state / "headers.json",
                    "--verify-response",
                    checkfile,
                    "--requests",
                    str(SEARCHES),
                    "--concurrency",
                    "1",
                    "--timeout-ms",
                    "4000",
                    "--json",
                    native,
                ]
            )
            if result.returncode:
                raise RuntimeError(result.stderr[-500:])
            doc = profile.read(native)
            with log.open("rb") as stream:
                stream.seek(offset)
                receipts = [
                    line
                    for line in stream
                    if b"method=POST " in line and b"status_code=" in line
                ]
            assert len(receipts) == SEARCHES + 1 and all(
                b"status_code=200" in line for line in receipts
            )
            assert (
                doc["counts"]["successes"] == SEARCHES
                and doc["verification"]["status"] == "passed"
            )
            after = request(port, route, "POST", body, meili.HEADERS)
            assert stable(data) == stable(after)
            rows[name] = dict(
                native=native.name,
                receipts=len(receipts),
                reply=signature(stable(after)),
                mean_ms=doc["success_latency_ms"]["mean"],
            )
        return rows


def stage_window(args, arm, instance, port):
    state = args.fixture / "state"
    query = json.loads(meili.body(1000))
    query["federation"]["showPerformanceDetails"] = True
    with meili.serve_binary(
        Path(arm["path"]),
        state,
        arm["version"],
        port,
        args.output / (instance + ".log"),
    ) as app:
        identity(app, arm, port)
        reports = []
        for i in range(9):
            data = request(port, "/multi-search", "POST", encode(query), meili.HEADERS)
            meili.validate_hits(data, 1000)
            stages = data["performanceDetails"]
            if not stages:
                raise ValueError("missing stage details")
            if i:  # one warm-up, eight diagnostic requests per process
                reports.append(stages)
        return reports


def main(args):
    if not __debug__:
        raise ValueError("run with Python assertions enabled")
    args.output = args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=False)
    args.fixture = args.fixture.resolve()
    args.engine = args.engine.resolve()
    arms = profile.targets(args)
    inputs = profile.pinned_inputs(args)
    count = 4 if args.phase == "stages" else 8
    rng = random.Random(args.seed)
    pairs = profile.order(args.name, count, rng)
    # The timing helper also allocates a native filename. Here each process
    # has one application observation and can have several native searches.
    for pair in pairs:
        for run in pair["runs"]:
            del run["result"]
    corpus = {i: documents(i) for i in INDEXES}
    catalog = list(catalog_cases().items())
    # Within each pair both arms have the same case order.
    orders = []
    for _ in pairs:
        order = catalog.copy()
        rng.shuffle(order)
        orders.append(order)
    record = dict(
        phase=args.phase,
        seed=args.seed,
        pairs=pairs,
        inputs=inputs,
        builds={
            k: {f: v for f, v in a.items() if f != "path"} for k, a in arms.items()
        },
        catalog_order=[[k for k, _ in order] for order in orders],
        default_features=args.default_features,
        documents_per_index=N,
        corpus_sha256=signature(corpus),
        server_cpus=cpus()[1],
        search_requests=SEARCHES,
    )
    profile.store(args.output / "plan.json", record)
    port = port_number()
    for i, pair in enumerate(pairs):
        for run in pair["runs"]:
            name, arm = run["instance"], arms[run["arm"]]
            if args.phase == "index":
                row = index_window(args, arm, name, port, corpus, i == 0)
            elif args.phase == "catalog":
                row = catalog_window(args, arm, name, port, orders[i])
            else:
                row = stage_window(args, arm, name, port)
            profile.store(args.output / (name + ".json"), row)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("index", "catalog", "stages"))
    for key in ("output", "arms", "fixture", "engine"):
        parser.add_argument("--" + key, required=True, type=Path)
    parser.add_argument(
        "--default-features",
        action="store_true",
        help="also verify Japanese and Korean segmented subterms",
    )
    parser.add_argument("--name", required=True)
    parser.add_argument("--seed", required=True, type=int)
    main(parser.parse_args())
