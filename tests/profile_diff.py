"""Numerical contract tests and a real, unprofiled HTTP target/revert smoke test."""

import argparse
import contextlib
import copy
import hashlib
import io
import importlib
import json
import os
from pathlib import Path
import runpy
import subprocess
import sys
import unittest
from unittest import mock

import analysis

FIXTURE = Path(__file__).resolve().parents[1] / "benchmarks" / "cpu-profile"
sys.path.insert(0, str(FIXTURE))
fixture = importlib.import_module("fixture")
profile_tools = importlib.import_module("profile_tools")


class ProfileComparison(unittest.TestCase):
    setUp = analysis.PairedAnalysis.setUp

    def inputs(self):
        def write(path, value):
            path.write_text(json.dumps(value))

        timing = analysis.PairedAnalysis.design(self, [40] * 6)
        dataset = "c" * 64
        timing["workload_id"] = dataset
        for arm, elf in (("baseline", "a" * 64), ("candidate", "b" * 64)):
            timing[arm + "_version"] = hashlib.sha256(
                (elf + "/v1").encode()
            ).hexdigest()
        write(self.root / "timing.json", timing)
        plan = dict(
            schema_version=1,
            timing_manifest="timing.json",
            dataset_sha256=dataset,
            collector="perf",
            frequency_hz=500,
            cpu_floor_us=150,
            cpu_floor_percent=10,
            pairs=[],
        )
        caps = []
        for i in range(2):
            rows = []
            for j, arm in enumerate(
                ("baseline", "candidate") if i == 0 else ("candidate", "baseline")
            ):
                candidate = arm == "candidate"
                instance = f"diagnostic-{i}-{arm}"
                native = self.root / (instance + "-native.json")
                run = copy.deepcopy(self.template)
                run["started_at_utc"] = f"2026-01-01T00:01:0{2 * i + j}Z"
                write(native, run)
                weights = [70, 10, 20] if candidate else [20, 55, 25]
                # Equal weight per process, even when one capture has twice the samples.
                factor = i + 1
                r = dict(
                    collector="perf",
                    successes=1,
                    preflights=1,
                    user_us=3000 if candidate else 1000,
                    system_us=150,
                    wall_us=10000,
                    peak_rss_kib=3000,
                    voluntary_switches=2,
                    involuntary_switches=0,
                )
                c = dict(
                    schema_version=1,
                    instance=instance,
                    binary_sha256=("b" if candidate else "a") * 64,
                    build_id=("b" if candidate else "a") * 40,
                    runtime_id="v1",
                    dataset_sha256=dataset,
                    collector="perf",
                    frequency_hz=500,
                    policy_id="test",
                    native_result=native.name,
                    native_sha256=fixture.sha(native),
                    resource=r,
                    samples=100 * factor,
                    frames=[
                        ["app::run", "server.cpp", 30],
                        ["app::lookup", "rates.cpp", 19],
                        ["[other]", "", 0],
                    ],
                    stacks=[
                        [weights[0] * factor, [1, 0]],
                        [weights[1] * factor, [0]],
                        [weights[2] * factor, [2, 0]],
                    ],
                )
                path = self.root / (instance + ".json")
                write(path, c)
                caps.append((path, c))
                rows.append(dict(arm=arm, capture=path.name))
            plan["pairs"].append(dict(id=f"diagnostic-{i}", runs=rows))
        return plan, caps

    def compare(self, plan, code=0):
        path = self.root / "profile.json"
        path.write_text(json.dumps(plan))
        run = subprocess.run(
            [self.engine, "profile-diff", path, "--json", "-"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(run.returncode, code, (run.stdout, run.stderr))
        doc = json.loads(run.stdout)
        self.assertFalse(doc["ci_gate"])
        return doc

    def test_leaf_cost_stack_unknowns_and_wait(self):
        plan, caps = self.inputs()
        report = self.compare(plan)
        self.assertEqual(report["reason"], "ranked_cpu_suspects")
        (row,) = report["suspects"]
        self.assertEqual(
            (row["symbol"], row["source"], row["line"]),
            ("app::lookup", "rates.cpp", 19),
        )
        self.assertEqual(row["baseline_self_us_per_success"], 200)
        self.assertEqual(row["candidate_self_us_per_success"], 2100)
        self.assertEqual(row["paired_deltas_us_per_success"], [1900, 1900])
        self.assertEqual(
            row["example_leaf_first_stack"][1], ["app::run", "server.cpp", 30]
        )
        self.assertEqual(report["resources"]["candidate"]["other_leaf_samples"], 60)
        # Strong wall-time regression in the primary evidence; no corresponding CPU rise.
        for path, cap in caps:
            cap["resource"]["user_us"] = 1000
            path.write_text(json.dumps(cap))
        wait = self.compare(plan)
        self.assertEqual(wait["reason"], "no_consistent_material_user_cpu_increase")
        self.assertEqual(wait["suspects"], [])

    def test_timing_gate_and_cross_phase_identity(self):
        plan, caps = self.inputs()
        timing = self.root / "timing.json"
        data = json.loads(timing.read_text())
        data["meaningful_change_percent"] = 50
        timing.write_text(json.dumps(data))
        self.assertEqual(
            self.compare(plan, 3)["reason"], "independent_timing_regression_required"
        )
        data["meaningful_change_percent"] = 10
        timing.write_text(json.dumps(data))
        path, cap = caps[0]
        for name, value in (
            ("collector", "gperftools"),
            ("dataset_sha256", "d" * 64),
            ("build_id", "d" * 40),
            ("policy_id", "another"),
            ("native_sha256", "d" * 64),
            ("runtime_id", "v2"),
            ("instance", data["blocks"][0]["runs"][0]["instance"]),
            ("native_result", "../result.json"),
            ("frames", [["short", "/private/main.cpp", 2]]),
            ("samples", 99),
            ("stacks", [[100, [5]]]),
        ):
            with self.subTest(name=name):
                path.write_text(json.dumps(dict(cap, **{name: value})))
                self.assertEqual(self.compare(plan, 2)["suspects"], [])
        path.write_text(json.dumps(cap))
        native = self.root / cap["native_result"]
        run = json.loads(native.read_text())
        run["workload"]["timeout_ms"] += 1
        native.write_text(json.dumps(run))
        path.write_text(json.dumps(dict(cap, native_sha256=fixture.sha(native))))
        self.assertEqual(
            self.compare(plan, 2)["reason"], "incompatible_or_incomplete_native_runs"
        )

    def test_missing_symbols_sparse_profiles_and_clone(self):
        plan, caps = self.inputs()
        path, cap = caps[0]
        for total, stack in [(12, [1, 0]), (100, [2, 1])]:
            path.write_text(
                json.dumps(dict(cap, samples=total, stacks=[[total, stack]]))
            )
            self.assertEqual(
                self.compare(plan, 3)["reason"], "insufficient_symbolized_samples"
            )
        timing = json.loads((self.root / "timing.json").read_text())
        old = self.root / timing["blocks"][0]["runs"][0]["result"]
        native = self.root / cap["native_result"]
        native.write_text(json.dumps(json.loads(old.read_text()), indent=2))
        path.write_text(json.dumps(dict(cap, native_sha256=fixture.sha(native))))
        self.assertEqual(
            self.compare(plan, 2)["reason"], "incompatible_or_incomplete_native_runs"
        )

    def test_exporter_stack_direction(self):
        sample = """Total: 5 samples
Stacks:

3  (00aa) /spe-fixture-src/rates.cpp:19:app::lookup
   (00ab) /spe-fixture-src/server.cpp:30:app::run

2  (b0ff) ??:0:libc
   (00ab) /spe-fixture-src/server.cpp:30:app::run

Flat profile:
"""
        frames, stacks, total = profile_tools.encode(profile_tools.gperf_stacks(sample))
        self.assertEqual((total, stacks), (5, [[3, [0, 1]], [2, [2, 1]]]))
        self.assertEqual(tuple(frames[2]), profile_tools.OTHER)
        with mock.patch.dict(os.environ, SPE_FIXTURE_PID="123"):
            callback = runpy.run_path(str(FIXTURE / "perf_frames.py"))["process_event"]
            event = {
                "ev_name": "cpu-clock:u",
                "sample": {"pid": 123, "ip": 170},
                "callchain": [{"ip": 2**64 - 512}, {"ip": 170}, {"ip": 171}],
            }
            with contextlib.redirect_stdout(io.StringIO()) as out:
                callback(event)
            self.assertEqual(json.loads(out.getvalue()), [170, 171])
            event["sample"]["pid"] = 124
            with self.assertRaises(ValueError):
                callback(event)

    @unittest.skipUnless(
        sys.platform == "linux"
        and len(getattr(os, "sched_getaffinity", lambda _: set())(0)) >= 2,
        "Linux loopback fixture needs two available CPUs",
    )
    def test_actual_binary_response_reverse_patch_and_elf_symbols(self):
        inputs = fixture.dataset(self.root, 128)
        fixture.write_json(self.root / "inputs.json", inputs)
        compiler = os.environ.get("CXX", "c++")
        sanitizer_flags = (
            ("-fsanitize=address,undefined", "-fno-sanitize-recover=all")
            if self.template["engine"]["sanitizers"] == "ON"
            else ()
        )
        builds = {}
        port = fixture.open_port()
        for target in ("baseline", "candidate", "revert"):
            builds[target] = fixture.build(self.root, target, compiler, sanitizer_flags)
            result = fixture.run_window(
                self.root,
                Path(self.engine),
                "live-" + target,
                target,
                builds[target],
                4,
                port,
                self.root / (target + ".json"),
            )
            self.assertEqual(
                (result["preflights"], result["successes"], result["collector"]),
                (1, 4, "none"),
            )
        self.assertEqual(builds["baseline"], builds["revert"])
        self.assertNotEqual(
            builds["baseline"]["binary_sha256"], builds["candidate"]["binary_sha256"]
        )
        binary = self.root / "targets" / "candidate" / "quote-server"
        symbol = next(
            row
            for row in subprocess.check_output(
                ["nm", "-nC", binary], text=True
            ).splitlines()
            if " T freight::lookup_rate(" in row
        )
        # Synthetic addresses exercise PIE relocation and real ELF/DWARF source resolution offline.
        address = int(symbol.split()[0], 16)
        phdrs = [
            row.split()
            for row in subprocess.check_output(
                ["readelf", "-lW", binary], text=True
            ).splitlines()
            if row.lstrip().startswith("LOAD ")
        ]
        ph = next(
            row
            for row in phdrs
            if int(row[2], 16) <= address < int(row[2], 16) + int(row[4], 16)
        )
        start = 0x70000000
        pc = start + address - int(ph[2], 16)
        mapping = [[start, start + int(ph[4], 16), int(ph[1], 16)]]
        trace = profile_tools.symbolize_perf([[pc, 3]], mapping, binary)[0][1]
        self.assertIn("freight::lookup_rate", trace[0][0])
        self.assertEqual(trace[0][1], "catalog.cpp")
        self.assertEqual(trace[1], profile_tools.OTHER)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--engine", required=True)
    ProfileComparison.engine = parser.parse_args().engine
    unittest.main(argv=[__file__])
