"""Exercise native saved-result comparison, including real HTTP producer/consumer checks.

Arithmetic and rejection fixtures are synthetic test inputs, never benchmark evidence.
Historical artifacts and newly executed HTTP workloads are identified separately.
"""

import argparse
import copy
import datetime
import hashlib
import json
import os
import platform
import statistics
import subprocess
import tempfile
import unittest
from pathlib import Path

from integration import RunningService


ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "benchmarks/evidence/petclinic-delay"


def numerical_fixture(elapsed=10, latency=400):
    """A constant-duration distribution for independently checkable arithmetic."""
    result = json.loads((EVIDENCE / "spe-baseline.json").read_text())
    result["workload"]["requests"] = 100
    result["counts"] = {name: 100 if name in ("requested", "started", "completed", "successes") else 0
                        for name in result["counts"]}
    result["elapsed_seconds"] = elapsed
    result["completed_throughput_rps"] = result["successful_throughput_rps"] = 100 / elapsed
    result["http_status_counts"] = {"200": 100}
    for name in ("success_latency_ms", "completed_latency_ms"):
        result[name] = {key: 100 if key == "samples" else latency for key in result[name]}
    return result


def all_http_errors():
    result = numerical_fixture()
    result["counts"].update(successes=0, http_errors=100)
    result["successful_throughput_rps"] = 0
    result["http_status_counts"] = {"503": 100}
    result["success_latency_ms"] = {key: 0 if key == "samples" else None for key in result["success_latency_ms"]}
    return result


def current_fixture(https=False, ca_hash=None):
    result = numerical_fixture()
    result["schema_version"] = 2
    result["engine"]["version"] = "0.2.0"
    result["counts"]["tls_errors"] = 0
    result["workload"]["tls"] = None
    if https:
        result["workload"]["url"] = result["workload"]["url"].replace("http://", "https://")
        result["workload"]["tls"] = dict(verify_peer=True, verify_host=True, minimum_version="1.2",
            ca_source="libcurl_default" if ca_hash is None else "custom_bundle", ca_bundle_sha256=ca_hash)
    return result


def header_fixture(names=()):
    result = current_fixture()
    result["schema_version"] = 3
    result["engine"]["version"] = "0.3.0"
    result["workload"]["headers"] = dict(names=list(names), values="redacted")
    return result


class ComparisonTests(unittest.TestCase):
    engine = None
    artifacts = None
    observations = []

    def setUp(self):
        self.directory = self.artifacts / self._testMethodName
        self.directory.mkdir(parents=True, exist_ok=True)
        self.sequence = 0

    def save(self, document, name=None):
        self.sequence += 1
        path = self.directory / (name or f"input-{self.sequence}.json")
        path.write_text(json.dumps(document, allow_nan=False) + "\n")
        return path

    def command(self, args, expected=0):
        self.sequence += 1
        command = [str(self.engine), *map(str, args)]
        process = subprocess.run(command, capture_output=True, text=True, timeout=20)
        stem = self.directory / f"command-{self.sequence}"
        stem.with_suffix(".stdout").write_text(process.stdout)
        stem.with_suffix(".stderr").write_text(process.stderr)
        self.observations.append(dict(test=self._testMethodName, command=command,
                                      returncode=process.returncode))
        self.assertEqual(process.returncode, expected, (command, process.stdout, process.stderr))
        return process

    def compare(self, a, b, *, expected=0, extra=()):
        process = self.command(["compare", a, b, *extra, "--json", "-"], expected)
        report = json.loads(process.stdout, parse_constant=lambda value: self.fail(f"nonfinite output {value}"))
        self.assertEqual(report["schema_version"], 3)
        self.assertEqual(report["kind"], "spe_comparison")
        self.assertEqual(report["statistical_verdict"], "not_assessed")
        self.assertIn("Statistical verdict: NOT ASSESSED.", process.stderr)
        if expected == 2:
            self.assertEqual(report["status"], "rejected")
            self.assertEqual(report["metrics"], {})
            self.assertNotIn("baseline", report)
        return report, process

    def test_single_run_arithmetic_format_and_json_streams(self):
        report, process = self.compare(self.save(numerical_fixture()), self.save(numerical_fixture(5, 200)))
        self.assertEqual(report["status"], "descriptive_comparison")
        metric = report["metrics"]["successful_throughput_rps"]
        self.assertEqual((metric["baseline"], metric["candidate"], metric["absolute_change"], metric["change_percent"]),
                         (10, 20, 10, 100))
        self.assertEqual(metric["preferred_direction"], "higher")
        for name in ("mean_request_latency_ms", "median_run_p50_ms", "median_run_p95_ms", "median_run_p99_ms"):
            metric = report["metrics"][name]
            self.assertEqual((metric["baseline"], metric["candidate"], metric["change_percent"]), (400, 200, -50))
            self.assertEqual(metric["preferred_direction"], "lower")
        self.assertIn("+100.00%", process.stderr)
        self.assertIn("-50.00%", process.stderr)
        self.assertIn("single run per version", process.stderr)
        self.assertIn("not pooled request percentiles", process.stderr)
        self.assertNotIn("PERFORMANCE COMPARISON", process.stdout)
        self.assertIsNone(report["baseline"]["run_variability"]["successful_throughput_rps"]["sample_stddev"])

    def test_multiple_runs_use_totals_and_run_variability(self):
        a = [self.save(numerical_fixture()), self.save(numerical_fixture(5, 200))]
        b = [self.save(numerical_fixture(8, 320)), self.save(numerical_fixture(4, 160)),
             self.save(numerical_fixture(2, 80))]
        args = ["compare"]
        for flag, paths in (("--baseline", a), ("--candidate", b)):
            for path in paths:
                args.extend([flag, path])
        process = self.command([*args, "--json", "-"])
        report = json.loads(process.stdout)
        self.assertAlmostEqual(report["metrics"]["successful_throughput_rps"]["baseline"], 200 / 15)
        self.assertAlmostEqual(report["metrics"]["successful_throughput_rps"]["candidate"], 300 / 14)
        self.assertEqual(report["baseline"]["counts"]["successes"], 200)
        self.assertEqual(report["metrics"]["mean_request_latency_ms"]["baseline"], 300)
        self.assertAlmostEqual(report["metrics"]["mean_request_latency_ms"]["candidate"], 560 / 3)
        self.assertEqual(report["metrics"]["median_run_p95_ms"]["baseline"], 300)
        self.assertEqual(report["metrics"]["median_run_p95_ms"]["candidate"], 160)
        spread = report["baseline"]["run_variability"]["successful_throughput_rps"]
        self.assertEqual((spread["runs"], spread["minimum"], spread["maximum"], spread["median"]), (2, 10, 20, 15))
        self.assertAlmostEqual(spread["sample_stddev"], statistics.stdev([10, 20]))
        self.assertIn("independence of runs is not established", process.stderr)
        self.assertIn("sample_stddev=7.071", process.stderr)

    def test_partial_group_uses_success_weighted_latency_and_error_denominator(self):
        mixed = numerical_fixture()
        mixed["counts"].update(successes=50, http_errors=50)
        mixed["successful_throughput_rps"] = 5
        mixed["http_status_counts"] = {"200": 50, "503": 50}
        mixed["success_latency_ms"] = dict(samples=50, min=600, mean=600, p50=600, p95=600, p99=600, max=600)
        mixed["completed_latency_ms"] = dict(samples=100, min=200, mean=400, p50=200, p95=600, p99=600, max=600)
        a, b, c = self.save(numerical_fixture()), self.save(mixed), self.save(numerical_fixture())
        process = self.command(["compare", "--baseline", a, "--baseline", b, "--candidate", c, "--json", "-"], expected=3)
        report = json.loads(process.stdout)
        self.assertEqual(report["status"], "insufficient_evidence")
        self.assertAlmostEqual(report["baseline"]["metrics"]["mean_request_latency_ms"], (100 * 400 + 50 * 600) / 150)
        self.assertEqual(report["baseline"]["metrics"]["successful_throughput_rps"], 150 / 20)
        self.assertEqual(report["baseline"]["completed_error_rate_percent"], 50 / 200 * 100)
        self.assertIsNone(report["metrics"]["successful_throughput_rps"]["change_percent"])

    def test_identical_runs_have_zero_variation(self):
        # Distinct fixture files isolate numerical aggregation, without implying independent evidence.
        args = ["compare"]
        for _ in range(6):
            args.extend(["--baseline", self.save(numerical_fixture())])
        args.extend(["--candidate", self.save(numerical_fixture()), "--json", "-"])
        process = self.command(args)
        report = json.loads(process.stdout)
        for metric in report["baseline"]["run_variability"].values():
            self.assertEqual(metric["sample_stddev"], 0)

    def test_actual_historical_measurements(self):
        baseline, candidate = EVIDENCE / "spe-baseline.json", EVIDENCE / "spe-candidate.json"
        before = [hashlib.sha256(path.read_bytes()).hexdigest() for path in (baseline, candidate)]
        report, _ = self.compare(baseline, candidate)
        source = [json.loads(path.read_text()) for path in (baseline, candidate)]
        for group, run in zip(("baseline", "candidate"), source):
            self.assertEqual(report[group]["counts"]["successes"], 10000)
            self.assertEqual(report[group]["metrics"]["mean_request_latency_ms"], run["success_latency_ms"]["mean"])
        a, b = (run["counts"]["successes"] / run["elapsed_seconds"] for run in source)
        self.assertAlmostEqual(report["metrics"]["successful_throughput_rps"]["change_percent"], (b - a) / a * 100)
        self.assertEqual(before, [hashlib.sha256(path.read_bytes()).hexdigest() for path in (baseline, candidate)])

    def test_current_and_legacy_contracts_remain_distinct(self):
        for fixture, schema in ((numerical_fixture(), 1), (current_fixture(), 2), (current_fixture(True), 2), (header_fixture(), 3)):
            with self.subTest(schema=schema, tls=fixture["workload"].get("tls")):
                report, _ = self.compare(self.save(fixture), self.save(fixture))
                self.assertEqual(report["input_result_schema_version"], schema)
                self.assertEqual(report["baseline"]["counts"]["tls_errors"], 0)
        self.compare(self.save(numerical_fixture()), self.save(current_fixture()), expected=2)
        self.compare(self.save(current_fixture()), self.save(header_fixture()), expected=2)
        legacy = numerical_fixture()
        legacy["workload"]["url"] = legacy["workload"]["url"].replace("http://", "https://")
        self.compare(self.save(legacy), self.save(legacy), expected=2)

    def test_redacted_headers_require_explicit_acknowledgement(self):
        a, b = self.save(header_fixture(["accept", "authorization"])), self.save(header_fixture(["accept", "authorization"]))
        self.compare(a, b, expected=2)
        self.compare(a, b, expected=2, extra=("--allow-origin-change",))
        report, process = self.compare(a, b, extra=("--allow-redacted-headers",))
        self.assertTrue(report["redacted_headers_acknowledged"])
        self.assertIn("Equality and authorization context are unverified", process.stderr)
        for names in ([], ["authorization"], ["accept", "x-api-key"]):
            self.compare(a, self.save(header_fixture(names)), expected=2, extra=("--allow-redacted-headers",))
        for metadata in ({"names": ["authorization", "accept"], "values": "redacted"},
                         {"names": ["accept", "accept"], "values": "redacted"},
                         {"names": ["Authorization"], "values": "redacted"},
                         {"names": ["host"], "values": "redacted"},
                         {"names": ["x-unsafe\r\nfield"], "values": "redacted"},
                         {"names": [None], "values": "redacted"},
                         {"names": ["authorization"], "values": "plaintext"},
                         {"names": ["authorization"], "values": "redacted", "secret": "dummy"}):
            changed = header_fixture()
            changed["workload"]["headers"] = metadata
            self.compare(a, self.save(changed), expected=2, extra=("--allow-redacted-headers",))

    def test_https_trust_and_transport_settings_must_match(self):
        baseline = self.save(current_fixture(True, "a" * 64))
        self.compare(baseline, self.save(current_fixture(True, "a" * 64)))
        for changed in (current_fixture(), current_fixture(True), current_fixture(True, "b" * 64)):
            self.compare(baseline, self.save(changed), expected=2, extra=("--allow-origin-change",))
        for key, value in (("verify_peer", False), ("verify_host", False), ("minimum_version", "1.0"),
                           ("ca_source", "unknown"), ("ca_bundle_sha256", None), ("ca_bundle_sha256", "invalid")):
            with self.subTest(key=key):
                changed = current_fixture(True, "a" * 64)
                changed["workload"]["tls"][key] = value
                self.compare(baseline, self.save(changed), expected=2)
        changed = current_fixture(True)
        changed["workload"]["tls"]["ca_bundle_sha256"] = "a" * 64
        self.compare(baseline, self.save(changed), expected=2)
        changed = current_fixture(True)
        changed["workload"]["tls"] = None
        self.compare(baseline, self.save(changed), expected=2)
        changed = current_fixture()
        changed["workload"]["tls"] = current_fixture(True)["workload"]["tls"]
        self.compare(baseline, self.save(changed), expected=2)

    def test_tls_failures_and_transport_accounting(self):
        baseline = self.save(current_fixture(True))
        failed = current_fixture(True)
        failed["counts"].update(successes=0, tls_errors=100)
        failed["http_status_counts"] = {}
        failed["successful_throughput_rps"] = 0
        failed["success_latency_ms"] = {key: 0 if key == "samples" else None for key in failed["success_latency_ms"]}
        failed["transport_error_counts"] = {"60": dict(count=100, description="certificate validation failure")}
        report, process = self.compare(baseline, self.save(failed), expected=3)
        self.assertEqual(report["candidate"]["counts"]["tls_errors"], 100)
        self.assertIn("TLS=100", process.stderr)
        self.assertTrue(all(metric["change_percent"] is None for metric in report["metrics"].values()))
        corrupt = copy.deepcopy(failed)
        corrupt["transport_error_counts"]["60"]["count"] = 99
        self.compare(baseline, self.save(corrupt), expected=2)
        corrupt = copy.deepcopy(failed)
        corrupt["counts"].update(tls_errors=0, network_errors=100)
        self.compare(baseline, self.save(corrupt), expected=2)

    def test_k6_normalization_is_not_native_measurement_compatibility(self):
        reference = EVIDENCE / "k6-baseline.json"
        self.assertTrue(reference.is_file())
        self.compare(EVIDENCE / "spe-baseline.json", reference, expected=2)

    def test_malformed_duplicate_and_nonfinite_json(self):
        baseline = self.save(numerical_fixture())
        original = json.dumps(numerical_fixture())
        cases = ["{", "null", "[]", original + " trailing", "/* comment */" + original,
                 original[:-1] + ",}", original.replace('"elapsed_seconds": 10', '"elapsed_seconds": NaN'),
                 original.replace('"elapsed_seconds": 10', '"elapsed_seconds": Infinity'),
                 original.replace('"elapsed_seconds": 10', '"elapsed_seconds": 1e10000'),
                 original[:-1] + ', "schema_version": 1}',
                 original.replace('"requests": 100', '"requests": 100, "requ\\u0065sts": 100'),
                 "[" * 34 + "0" + "]" * 34]
        for i, value in enumerate(cases):
            with self.subTest(case=i):
                path = self.directory / f"malformed-{i}.json"
                path.write_text(value)
                self.compare(baseline, path, expected=2)
        invalid_utf = self.directory / "invalid-utf.json"
        invalid_utf.write_bytes(original.encode().replace(b"AppleClang", b"\xff"))
        self.compare(baseline, invalid_utf, expected=2)

    def test_malformed_inputs_never_echo_private_payloads(self):
        dummy = "spe-dummy-private-payload-must-not-be-echoed"
        path = self.directory / "mistaken-private-config.json"
        path.write_text('{"value":"' + dummy + '\n')
        report, process = self.compare(self.save(numerical_fixture()), path, expected=2)
        self.assertNotIn(dummy, process.stdout + process.stderr)
        self.assertIn("invalid result JSON", report["error"])

    def test_required_fields_types_bounds_and_measurement_integrity(self):
        baseline = self.save(numerical_fixture())
        cases = [(("schema_version",), 2), (("schema_version",), True), (("schema_version",), "1"),
                 (("engine", "version"), "0.2.0"), (("engine", "compiler"), None),
                 (("engine", "sanitizers"), "maybe"), (("engine", "unexpected"), 1),
                 (("workload", "requests"), 0), (("workload", "requests"), 1000001),
                 (("workload", "requests"), 100.0), (("workload", "concurrency"), 0),
                 (("workload", "timeout_ms"), 0), (("workload", "max_body_bytes"), 0),
                 (("workload", "method"), "POST"), (("workload", "connection_reuse"), False),
                 (("counts", "successes"), -1), (("counts", "successes"), 99),
                 (("counts", "completed"), "100"), (("counts", "started"), 101),
                 (("counts", "http_errors"), 1), (("http_status_counts",), {"200": 99}),
                 (("http_status_counts",), {"0200": 100}), (("http_status_counts",), {"200": 100, "503": 0}),
                 (("transport_error_counts",), {"28": {"count": 1, "description": "timeout"}}),
                 (("elapsed_seconds",), 0), (("elapsed_seconds",), -1), (("elapsed_seconds",), 1e13),
                 (("successful_throughput_rps",), 11), (("completed_throughput_rps",), 11),
                 (("peak_in_flight",), 0), (("peak_in_flight",), 5), (("interrupted",), 0),
                 (("engine_error",), ""), (("success_latency_ms", "samples"), 99),
                 (("success_latency_ms", "mean"), "400"), (("success_latency_ms", "min"), None),
                 (("success_latency_ms", "min"), -1), (("success_latency_ms", "p50"), 500),
                 (("completed_latency_ms", "mean"), 401), (("started_at_utc",), "2026-02-30T12:00:00Z"),
                 (("started_at_utc",), "2026-11-16T-1:00:00Z")]
        for path, value in cases:
            with self.subTest(path=path, value=value):
                changed = numerical_fixture()
                target = changed
                for key in path[:-1]:
                    target = target[key]
                target[path[-1]] = value
                self.compare(baseline, self.save(changed), expected=2)
        for field in ("counts", "completed_latency_ms", "engine", "workload", "started_at_utc"):
            changed = numerical_fixture()
            del changed[field]
            self.compare(baseline, self.save(changed), expected=2)
        changed = numerical_fixture(latency=10001)
        self.compare(baseline, self.save(changed), expected=2)
        changed = numerical_fixture(latency=401)  # Each sample fits, but their sum exceeds four in-flight slots.
        self.compare(baseline, self.save(changed), expected=2)

    def test_latency_mean_and_observed_peak_integrity(self):
        baseline = self.save(numerical_fixture())
        changed = numerical_fixture()
        changed["peak_in_flight"] = 1  # Sum of request durations exceeds this observed capacity.
        self.compare(baseline, self.save(changed), expected=2)
        for average in (0, 400, 1e-310):
            with self.subTest(mean=average):
                changed = numerical_fixture(latency=average)
                for name in ("success_latency_ms", "completed_latency_ms"):
                    changed[name].update(min=0, p50=0, p95=0, p99=0, max=400)
                self.compare(baseline, self.save(changed), expected=2)
        changed = numerical_fixture(latency=4)
        for name in ("success_latency_ms", "completed_latency_ms"):
            changed[name].update(min=0, p50=0, p95=0, p99=0, max=400)
        self.compare(baseline, self.save(changed))  # One 400 ms sample and 99 zero samples.

    def test_singleton_distribution_integrity(self):
        run = numerical_fixture()
        run["workload"]["requests"] = 1
        for name in ("requested", "started", "completed", "successes"):
            run["counts"][name] = 1
        run["peak_in_flight"] = 1
        run["http_status_counts"] = {"200": 1}
        run["successful_throughput_rps"] = run["completed_throughput_rps"] = 0.1
        for key in ("success_latency_ms", "completed_latency_ms"):
            run[key]["samples"] = 1
        baseline = self.save(run)
        self.compare(baseline, self.save(run))
        run["success_latency_ms"]["max"] = 401
        run["completed_latency_ms"]["max"] = 401
        self.compare(baseline, self.save(run), expected=2)

    def test_engine_and_workload_compatibility(self):
        baseline = self.save(numerical_fixture())
        changes = [("engine", "compiler", "GCC 13.3"), ("engine", "build_type", "Debug"),
                   ("engine", "source_revision", "different"), ("engine", "build_flags", "-O0"),
                   ("engine", "sanitizers", "ON"), ("engine", "platform", "Linux x86_64"),
                   ("engine", "libcurl", "libcurl/8.5.0"), ("workload", "concurrency", 5),
                   ("workload", "timeout_ms", 3000), ("workload", "max_body_bytes", 1024)]
        for section, name, value in changes:
            with self.subTest(section=section, name=name):
                changed = numerical_fixture()
                changed[section][name] = value
                self.compare(baseline, self.save(changed), expected=2, extra=("--allow-origin-change",))
        changed = numerical_fixture()
        changed["workload"]["requests"] = 101
        changed["counts"]["requested"] = changed["counts"]["started"] = changed["counts"]["completed"] = changed["counts"]["successes"] = 101
        changed["http_status_counts"]["200"] = 101
        changed["completed_throughput_rps"] = changed["successful_throughput_rps"] = 10.1
        for name in ("success_latency_ms", "completed_latency_ms"):
            changed[name]["samples"] = 101
            for metric in changed[name]:
                if metric != "samples":
                    changed[name][metric] = 300
        report, _ = self.compare(baseline, self.save(changed), expected=2)
        self.assertIn("workload settings differ", report["error"])

    def test_origin_acknowledgement_and_endpoint_contract(self):
        a = numerical_fixture()
        a["workload"]["url"] = "http://LOCALHOST/owners/1?view=full"
        baseline = self.save(a)
        b = copy.deepcopy(a)
        b["workload"]["url"] = "http://localhost:80/owners/1?view=full"
        report, _ = self.compare(baseline, self.save(b))
        self.assertFalse(report["origin_change_acknowledged"])
        b["workload"]["url"] = "http://127.0.0.1:8082/owners/1?view=full"
        candidate = self.save(b)
        self.compare(baseline, candidate, expected=2)
        report, process = self.compare(baseline, candidate, extra=("--allow-origin-change",))
        self.assertTrue(report["origin_change_acknowledged"])
        self.assertIn("equivalence remains unverified", process.stderr)
        for url in ("http://localhost/owners/2?view=full", "http://localhost/owners/1?view=other",
                    "https://localhost/owners/1?view=full", "http://user:secret@localhost/owners/1?view=full",
                    "http://localhost/owners/1?view=full#fragment", "http://localhost:0/owners/1?view=full"):
            with self.subTest(url=url):
                b["workload"]["url"] = url
                self.compare(baseline, self.save(b), expected=2, extra=("--allow-origin-change",))
        a["workload"]["url"] = "http://localhost/owners/1?"
        b["workload"]["url"] = "http://localhost/owners/1"
        self.compare(self.save(a), self.save(b), expected=2)

    def test_failures_and_zero_success_suppress_performance_claims(self):
        baseline = self.save(numerical_fixture())
        errors = all_http_errors()
        report, process = self.compare(baseline, self.save(errors), expected=3)
        self.assertEqual(report["status"], "insufficient_evidence")
        self.assertEqual(report["candidate"]["completed_errors"], 100)
        self.assertEqual(report["candidate"]["completed_error_rate_percent"], 100)
        self.assertIsNone(report["candidate"]["metrics"]["mean_request_latency_ms"])
        self.assertIn("completed_with_failures", process.stderr)
        for metric in report["metrics"].values():
            self.assertIsNone(metric["absolute_change"])
            self.assertIsNone(metric["change_percent"])
            self.assertEqual(metric["change_note"], "failed_or_incomplete_runs")
        self.assertIn("performance deltas suppressed", process.stderr)
        self.compare(self.save(errors), self.save(errors), expected=3)
        errors["successful_throughput_rps"] = 1e-25  # Zero numerator requires exactly zero throughput.
        self.compare(baseline, self.save(errors), expected=2)
        errors = all_http_errors()
        errors["success_latency_ms"]["mean"] = 0
        self.compare(baseline, self.save(errors), expected=2)

    def test_ipv6_scopes_require_explicit_origin_acknowledgement(self):
        a = numerical_fixture()
        a["workload"]["url"] = "http://[fe80::1%25en0]:8080/owners/1"
        baseline = self.save(a)
        self.compare(baseline, self.save(a))
        b = copy.deepcopy(a)
        for url in ("http://[fe80::1%25en1]:8080/owners/1", "http://[fe80::1]:8080/owners/1"):
            with self.subTest(url=url):
                b["workload"]["url"] = url
                candidate = self.save(b)
                self.compare(baseline, candidate, expected=2)
                report, _ = self.compare(baseline, candidate, extra=("--allow-origin-change",))
                self.assertTrue(report["origin_change_acknowledged"])
        b["workload"]["url"] = "http://[fe80::1%25en1]:8080/owners/2"
        self.compare(baseline, self.save(b), expected=2, extra=("--allow-origin-change",))

    def test_mixed_success_and_transport_failure(self):
        baseline = self.save(numerical_fixture())
        for outcome, status, code in (("http_errors", "503", None), ("timeouts", None, "28"),
                                      ("network_errors", None, "18"), ("connection_errors", None, "7"),
                                      ("response_limit_errors", None, "23")):
            with self.subTest(outcome=outcome):
                run = numerical_fixture()
                run["counts"].update(successes=50)
                run["counts"][outcome] = 50
                run["http_status_counts"] = {"200": 50}
                if status:
                    run["http_status_counts"][status] = 50
                if code:
                    run["transport_error_counts"] = {code: dict(count=50, description="test transport failure")}
                run["successful_throughput_rps"] = 5
                run["success_latency_ms"]["samples"] = 50
                report, _ = self.compare(baseline, self.save(run), expected=3)
                self.assertEqual(report["candidate"]["counts"][outcome], 50)
                self.assertEqual(report["candidate"]["completed_error_rate_percent"], 50)

    def test_interrupted_cancelled_and_not_started(self):
        baseline = self.save(numerical_fixture())
        interrupted = numerical_fixture()
        interrupted["counts"].update(started=60, completed=50, successes=50, cancelled=10, not_started=40)
        interrupted["http_status_counts"] = {"200": 50}
        interrupted["interrupted"] = True
        interrupted["successful_throughput_rps"] = interrupted["completed_throughput_rps"] = 5
        for name in ("success_latency_ms", "completed_latency_ms"):
            interrupted[name]["samples"] = 50
        report, _ = self.compare(baseline, self.save(interrupted), expected=3)
        self.assertEqual(report["candidate"]["inputs"][0]["state"], "incomplete_or_interrupted")
        interrupted["interrupted"] = False
        self.compare(baseline, self.save(interrupted), expected=2)
        interrupted["engine_error"] = "controlled test failure"
        self.compare(baseline, self.save(interrupted), expected=3)
        for flag in ("interrupted", "engine_error"):
            run = numerical_fixture()
            run[flag] = True if flag == "interrupted" else "controlled test failure"
            self.compare(baseline, self.save(run), expected=3)

    def test_no_dispatch_and_zero_elapsed(self):
        run = all_http_errors()
        run["counts"] = {key: 100 if key in ("requested", "not_started") else 0 for key in run["counts"]}
        run["http_status_counts"] = {}
        run["elapsed_seconds"] = run["successful_throughput_rps"] = run["completed_throughput_rps"] = 0
        run["peak_in_flight"] = 0
        run["interrupted"] = True
        run["completed_latency_ms"] = copy.deepcopy(run["success_latency_ms"])
        report, process = self.compare(self.save(run), self.save(run), expected=3)
        self.assertEqual(report["baseline"]["metrics"]["successful_throughput_rps"], 0)
        self.assertIsNone(report["baseline"]["completed_error_rate_percent"])
        self.assertNotIn("n/a%", process.stderr)

    def test_zero_latency_denominator_and_finite_extremes(self):
        report, _ = self.compare(self.save(numerical_fixture(latency=0)), self.save(numerical_fixture()))
        metric = report["metrics"]["mean_request_latency_ms"]
        self.assertEqual(metric["absolute_change"], 400)
        self.assertIsNone(metric["change_percent"])
        self.assertEqual(metric["change_note"], "baseline_zero")
        report, _ = self.compare(self.save(numerical_fixture(latency=1e-310)), self.save(numerical_fixture()))
        metric = report["metrics"]["mean_request_latency_ms"]
        self.assertIsNone(metric["change_percent"])
        self.assertEqual(metric["change_note"], "percentage_outside_numeric_range")

    def test_duplicate_artifacts_and_aliases_are_rejected(self):
        baseline = self.save(numerical_fixture())
        candidate = self.save(numerical_fixture())
        self.compare(baseline, baseline, expected=2)
        alias = self.directory / "hardlink.json"
        alias.hardlink_to(baseline)
        self.compare(baseline, alias, expected=2)
        symlink = self.directory / "symlink.json"
        symlink.symlink_to(baseline)
        self.compare(baseline, symlink, expected=2)
        process = self.command(["compare", "--baseline", baseline, "--baseline", baseline,
                                "--candidate", candidate, "--json", "-"], expected=2)
        self.assertIn("duplicate input file", json.loads(process.stdout)["error"])

    def test_missing_nonregular_and_oversize_inputs(self):
        baseline = self.save(numerical_fixture())
        for path in (self.directory / "missing.json", self.directory):
            self.compare(baseline, path, expected=2)
        fifo = self.directory / "pipe.json"
        os.mkfifo(fifo)
        self.compare(baseline, fifo, expected=2)  # Must not wait for a writer.
        large = self.directory / "large.json"
        large.write_bytes(b" " * (1024 * 1024 + 1))
        self.compare(baseline, large, expected=2)
        large.unlink()

    def test_combined_input_bound(self):
        # Valid JSON padded with whitespace; exercise the total limit independently of per-file size.
        paths = []
        payload = json.dumps(numerical_fixture()).encode()
        try:
            for i in range(33):
                path = self.directory / f"padded-{i}.json"
                path.write_bytes(payload + b" " * (1024 * 1024 - 1 - len(payload)))
                paths.append(path)
            args = ["compare"]
            for path in paths[:-1]:
                args.extend(["--baseline", path])
            process = self.command([*args, "--candidate", paths[-1], "--json", "-"], expected=2)
            self.assertIn("combined input exceeds", json.loads(process.stdout)["error"])
        finally:
            for path in paths:
                path.unlink()

    def test_safe_output_preserves_measurements(self):
        baseline, candidate = self.save(numerical_fixture()), self.save(numerical_fixture(5, 200))
        original = baseline.read_bytes()
        self.command(["compare", baseline, candidate, "--json", baseline], expected=1)
        self.assertEqual(baseline.read_bytes(), original)
        destination = self.directory / "comparison.json"
        # Exclusive output creation also applies to comparisons.
        self.command(["compare", baseline, candidate, "--json", destination])
        output = destination.read_bytes()
        self.assertEqual(json.loads(output)["status"], "descriptive_comparison")
        self.command(["compare", baseline, candidate, "--json", destination], expected=1)
        self.assertEqual(destination.read_bytes(), output)
        missing_parent = self.directory / "missing-parent" / "output.json"
        self.command(["compare", baseline, candidate, "--json", missing_parent], expected=1)
        rejected = self.directory / "rejected.json"
        self.command(["compare", baseline, self.directory / "absent.json", "--json", rejected], expected=2)
        self.assertEqual(json.loads(rejected.read_text())["status"], "rejected")

    def test_cli_help_and_argument_validation(self):
        self.assertIn("spe compare", self.command(["--help"]).stdout)
        self.assertIn("100 files/group", self.command(["compare", "--help"]).stdout)
        self.command(["--version"])
        for args in ([], ["a"], ["a", "b", "c"], ["--baseline", "a"], ["--json"],
                     ["--baseline", "a", "--candidate"], ["--unknown"],
                     ["a", "b", "--json", "-", "--json", "-"], ["a", "b", "--json", ""],
                     ["a", "b", "--allow-origin-change", "--allow-origin-change"],
                     ["a", "--baseline", "b", "--candidate", "c"]):
            with self.subTest(args=args):
                self.command(["compare", *args], expected=2)
        args = ["compare"]
        for i in range(101):
            args.extend(["--baseline", f"run-{i}.json"])
        self.command([*args, "--candidate", "c.json"], expected=2)

    def test_real_http_saved_runs_and_error_results(self):
        # These are executed loopback requests, not mocked results. No performance threshold is inferred.
        for suffix, expected in (("/ok?delay_ms=5", 0), ("/ok?status=503", 3), ("/mixed", 3),
                                  ("/truncated", 3), ("/slow-body?delay_ms=80", 3)):
            with self.subTest(endpoint=suffix), RunningService() as service:
                paths = []
                for index in range(2):
                    path = self.directory / f"http-{len(list(self.directory.glob('http-*.json')))}.json"
                    args = [service.url + suffix, "--requests", "12", "--concurrency", "3", "--timeout-ms",
                            "20" if "slow-body" in suffix else "1000", "--json", path]
                    self.command(args, expected)
                    paths.append(path)
                report, _ = self.compare(*paths, expected=expected)
                received = service.idle()["received"]
                self.assertGreater(received, 0)
                if "slow-body" not in suffix:
                    self.assertEqual(received, 24)
                if expected == 0:
                    self.assertEqual(report["baseline"]["counts"]["successes"], 12)
                    self.assertEqual(report["candidate"]["counts"]["successes"], 12)
                else:
                    self.assertEqual(report["status"], "insufficient_evidence")
                self.observations.append(dict(test=self._testMethodName, kind="executed_loopback_traffic",
                                              endpoint=suffix, server_observed=service.idle(), inputs=list(map(str, paths))))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", type=Path, required=True)
    parser.add_argument("--artifacts", type=Path, required=True)
    args = parser.parse_args()
    started = datetime.datetime.now(datetime.timezone.utc).isoformat()
    ComparisonTests.engine = args.engine.resolve()
    ComparisonTests.artifacts = args.artifacts.resolve()
    ComparisonTests.artifacts.mkdir(parents=True, exist_ok=True)
    # A unique directory retains earlier executions and makes exclusive output tests repeatable.
    ComparisonTests.artifacts = Path(tempfile.mkdtemp(prefix="comparison-", dir=ComparisonTests.artifacts))
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(ComparisonTests)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    manifest = dict(kind="functional_comparison_tests", started_at_utc=started,
                    finished_at_utc=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    platform=platform.platform(), engine=str(ComparisonTests.engine),
                    engine_sha256=hashlib.sha256(ComparisonTests.engine.read_bytes()).hexdigest(),
                    test_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                    http_fixture_sha256=hashlib.sha256((ROOT / "tests/http_server.py").read_bytes()).hexdigest(),
                    tests_run=result.testsRun, successful=result.wasSuccessful(),
                    synthetic_inputs="Arithmetic and rejection fixtures; not benchmark evidence.",
                    real_traffic="Loopback HTTP checks; not representative application benchmarks.",
                    observations=ComparisonTests.observations)
    (ComparisonTests.artifacts / "observations.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"Comparison test artifacts: {ComparisonTests.artifacts}", flush=True)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
