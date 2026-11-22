"""Native paired-analysis contract tests. Numerical inputs are synthetic fixtures."""

import argparse
import copy
import datetime as dt
import json
import math
from pathlib import Path
import subprocess
import tempfile
import unittest

from integration import RunningService


class PairedAnalysis(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        config = self.root / "rule.json"
        config.touch(mode=0o600)
        import hashlib

        with RunningService() as service:
            from urllib.request import urlopen

            url = service.url + "/ok"
            with urlopen(url) as reply:
                expected = hashlib.sha256(reply.read()).hexdigest()
            config.write_text(
                json.dumps(dict(policy_id="test", endpoints=[dict(sha256=expected)]))
            )
            cmd = [
                self.engine,
                url,
                "--requests",
                "1",
                "--concurrency",
                "1",
                "--verify-response",
                config,
                "--json",
                "-",
            ]
            self.template = json.loads(
                subprocess.check_output(cmd, stderr=subprocess.DEVNULL)
            )
            config.write_text(
                json.dumps(dict(policy_id="test", endpoints=[dict(sha256="0" * 64)]))
            )
            failure = subprocess.run(cmd, capture_output=True, check=False)
            self.assertEqual(failure.returncode, 3)
            self.failed = json.loads(failure.stdout)

    def design(self, changes):
        doc = dict(
            schema_version=1,
            experiment_id="arithmetic",
            workload_id="constant",
            metric="mean_latency_ms",
            baseline_version="v1",
            candidate_version="v2",
            private_inputs_confirmed=False,
            meaningful_change_percent=10,
            blocks=[],
        )
        for i, effect in enumerate(changes):
            rows = []
            arms = (
                ("baseline", "candidate") if i % 2 == 0 else ("candidate", "baseline")
            )
            for j, arm in enumerate(arms):
                name = f"run-{2 * i + j}"
                run = copy.deepcopy(self.template)
                ms = 100 if arm == "baseline" else 100 + effect
                for key in ("success_latency_ms", "completed_latency_ms"):
                    run[key].update({k: float(ms) for k in run[key] if k != "samples"})
                run["elapsed_seconds"] = ms / 1000
                run["completed_throughput_rps"] = run["successful_throughput_rps"] = (
                    1000 / ms
                )
                start = dt.datetime(2026, 1, 1) + dt.timedelta(seconds=2 * i + j)
                run["started_at_utc"] = start.strftime("%Y-%m-%dT%H:%M:%SZ")
                path = name + ".json"
                (self.root / path).write_text(json.dumps(run))
                rows.append(dict(arm=arm, instance=name, result=path))
            doc["blocks"].append(dict(id=f"pair-{i}", runs=rows))
        return doc

    def analyze(self, doc, code=0):
        path = self.root / "design.json"
        path.write_text(json.dumps(doc))
        p = subprocess.run(
            [self.engine, "analyze", path, "--json", "-"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(p.returncode, code, (p.stdout, p.stderr))
        result = json.loads(p.stdout)
        self.assertFalse(result["ci_gate"])
        return result

    def test_exact_interval_and_direction(self):
        doc = self.design(range(21, 31))
        result = self.analyze(doc)
        self.assertEqual(result["verdict"], "regression")
        self.assertEqual(result["effect_percent"], 25.5)
        for bound, expected in zip(result["interval_percent"], [22, 29]):
            self.assertAlmostEqual(bound, expected)
        coverage = 1 - 2 * (math.comb(10, 0) + math.comb(10, 1)) / 2**10
        self.assertAlmostEqual(result["coverage_lower_bound"], coverage)
        doc["metric"] = "successful_rps"
        self.assertEqual(self.analyze(doc)["verdict"], "regression")
        self.assertEqual(self.analyze(self.design([-15] * 6))["verdict"], "improvement")
        for changes in (
            [10] * 8,
            [-10] * 8,
            [-30, 25, 25, 25, 25, 25, 25, 25],
            [0] * 12,
        ):
            self.assertEqual(
                self.analyze(self.design(changes))["verdict"], "inconclusive"
            )

    def test_ineligible_designs(self):
        self.assertEqual(
            self.analyze(self.design([50] * 5), 3)["reason"],
            "too_few_independent_pairs",
        )
        doc = self.design([50] * 6)
        doc["blocks"][0]["runs"][0]["instance"] = doc["blocks"][1]["runs"][0][
            "instance"
        ]
        self.assertEqual(self.analyze(doc, 2)["verdict"], "inconclusive")
        doc = self.design([50] * 6)
        path = self.root / "run-1.json"
        path.write_text(
            json.dumps(json.loads((self.root / "run-0.json").read_text()), indent=4)
        )
        self.assertIn("duplicate measurement", self.analyze(doc, 2)["input_error"])
        doc = self.design([50] * 6)
        doc["blocks"].reverse()
        self.assertIn("timestamps", self.analyze(doc, 2)["input_error"])
        doc = self.design([50] * 6)
        for name in ("run-0.json", "run-1.json"):
            (self.root / name).write_text(json.dumps(self.failed))
        self.assertEqual(self.analyze(doc, 3)["reason"], "failed_or_incomplete_runs")
        doc = self.design([50] * 6)
        for path in self.root.glob("run-*.json"):
            run = json.loads(path.read_text())
            run["schema_version"] = 6
            del run["verification"]
            path.write_text(json.dumps(run))
        self.assertEqual(self.analyze(doc, 3)["reason"], "missing_native_preflight")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--engine", required=True)
    PairedAnalysis.engine = p.parse_args().engine
    unittest.main(argv=[__file__])
