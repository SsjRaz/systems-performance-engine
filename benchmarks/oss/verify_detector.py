"""Reconcile detector campaign files and independently recompute paired intervals."""

import argparse
import hashlib
import json
import math
from pathlib import Path
from statistics import median
import subprocess


def read(path):
    return json.loads(path.read_text())


def check(condition, message):
    if not condition:
        raise ValueError(message)


def verify(root, engine=None):
    design = read(root / "design.json")
    totals = dict(campaigns=0, runs=0, timed=0, probes=0, receipts=0)
    results = {row["campaign"]: row for row in read(root / "results.json")}
    check(
        set(results) == set(design["order"]) == set(design["manifests"]),
        "incomplete campaigns",
    )
    for name in design["order"]:
        folder = root / name
        raw_manifest = (folder / "manifest.json").read_bytes()
        check(
            hashlib.sha256(raw_manifest).hexdigest() == design["manifests"][name],
            "manifest changed",
        )
        manifest = json.loads(raw_manifest)
        receipt_rows = read(folder / "receipts.json")
        receipts = {r["instance"]: r for r in receipt_rows}
        report = read(folder / "analysis.json")
        check(
            report["verdict"] == results[name]["verdict"]
            and report["ci_gate"] is False,
            "summary differs",
        )
        if engine:
            p = subprocess.run(
                [engine, "analyze", folder / "manifest.json", "--json", "-"],
                capture_output=True,
                text=True,
                timeout=20,
            )
            current = json.loads(p.stdout)
            for field in (
                "verdict",
                "effect_percent",
                "interval_percent",
                "manifest_sha256",
            ):
                check(current[field] == report[field], "replay differs for " + field)
            case = results[name]["case"]
            if case in ("wrong-answer", "missed"):
                reason = (
                    "verification_failed/content_mismatch"
                    if case == "wrong-answer"
                    else "missed_arrivals"
                )
                check(
                    p.returncode == 3 and current["input_states"].get(reason, 0) > 0,
                    "missing failure diagnosis",
                )
            elif case in ("different-rate", "different-model"):
                check(
                    p.returncode == 2
                    and current["reason"] == "incompatible_or_invalid_inputs",
                    "incompatible designs accepted",
                )
            elif case == "short":
                check(
                    p.returncode == 3
                    and current["reason"] == "too_few_independent_pairs",
                    "underreplicated design accepted",
                )
        totals["campaigns"] += 1
        changes = []
        instances = set()
        for block in manifest["blocks"]:
            metrics = {}
            for row in block["runs"]:
                instance = row["instance"]
                check(instance not in instances, "duplicate server instance")
                instances.add(instance)
                run = read(folder / row["result"])
                receipt = receipts[instance]
                probes = run["verification"]["attempted"]
                timed = run["counts"]["started"]
                check(
                    receipt["probes"] == probes and receipt["timed"] == timed,
                    "incorrect client counts",
                )
                check(
                    receipt["server_receipts"] == probes + timed,
                    "server receipt mismatch",
                )
                totals["runs"] += 1
                totals["probes"] += probes
                totals["timed"] += timed
                totals["receipts"] += receipt["server_receipts"]
                if (
                    run["verification"]["status"] == "passed"
                    and run["counts"]["successes"] == run["counts"]["requested"]
                ):
                    metrics[row["arm"]] = run["success_latency_ms"]["mean"]
            if len(metrics) == 2:
                changes.append(
                    100
                    * (metrics["candidate"] - metrics["baseline"])
                    / metrics["baseline"]
                )
        check(len(instances) == len(receipt_rows), "unmatched receipt records")
        if report["effect_percent"] is None:
            check(
                report["verdict"] == "inconclusive"
                and report["interval_percent"] is None,
                "invalid withheld verdict",
            )
            continue
        count = len(changes)
        check(
            count == len(manifest["blocks"]) == report["independent_pairs"],
            "invalid pair count",
        )
        check(
            math.isclose(report["effect_percent"], median(changes), abs_tol=1e-10),
            "effect differs",
        )
        # Compute the binomial tails as exact integers, independently of the C++ recurrence.
        allowed = [
            (k, sum(math.comb(count, j) for j in range(k + 1)))
            for k in range((count + 1) // 2)
        ]
        allowed = [(k, tail) for k, tail in allowed if 40 * tail <= 2**count]
        if not allowed:
            check(
                report["interval_percent"] is None
                and report["verdict"] == "inconclusive",
                "too few pairs",
            )
            continue
        k, tail = allowed[-1]
        ordered = sorted(changes)
        low, high = ordered[k], ordered[-k - 1]
        check(
            math.isclose(report["coverage_lower_bound"], 1 - 2 * tail / 2**count),
            "coverage differs",
        )
        check(
            all(
                math.isclose(a, b, abs_tol=1e-10)
                for a, b in zip([low, high], report["interval_percent"])
            ),
            "interval differs",
        )
        threshold = manifest["meaningful_change_percent"]
        verdict = (
            "regression"
            if low > threshold
            else "improvement"
            if high < -threshold
            else "inconclusive"
        )
        check(verdict == report["verdict"], "verdict differs")
    return totals


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directories", nargs="+", type=Path)
    parser.add_argument(
        "--engine",
        type=Path,
        help="also replay every manifest with this native executable",
    )
    args = parser.parse_args()
    for folder in args.directories:
        print(
            folder.name, verify(folder, args.engine.resolve() if args.engine else None)
        )
