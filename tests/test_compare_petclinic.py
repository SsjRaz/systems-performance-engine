"""Deterministic runner checks. Numerical fixtures below are not benchmark evidence."""

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import compare_petclinic as runner
import reference_k6


def result():
    return dict(schema_version=1, counts=dict(requested=8, started=8, completed=8, successes=8,
                           http_errors=0, connection_errors=0, network_errors=0, timeouts=0,
                           response_limit_errors=0, cancelled=0, not_started=0),
                workload=dict(url="http://127.0.0.1:8080/owners/1", requests=8, concurrency=4, timeout_ms=2000),
                interrupted=False, engine_error=None, http_status_counts={"200": 8}, peak_in_flight=4,
                elapsed_seconds=2.0, successful_throughput_rps=4.0, completed_throughput_rps=4.0,
                success_latency_ms=dict(samples=8, min=1, mean=2, p50=2, p95=3, p99=3, max=3))


class ResultChecks(unittest.TestCase):
    def validate(self, value):
        runner.validate_result(value, 8, 4, 2000, "http://127.0.0.1:8080/owners/1")

    def test_success_and_invalid_accounting(self):
        self.validate(result())
        for key in ("successes", "started", "completed", "requested"):
            with self.subTest(counter=key):
                value = result()
                value["counts"][key] -= 1
                with self.assertRaises(RuntimeError):
                    self.validate(value)
        for key in ("http_errors", "connection_errors", "tls_errors", "timeouts", "cancelled", "not_started"):
            with self.subTest(counter=key):
                value = result()
                value["counts"][key] = 1
                with self.assertRaises(RuntimeError):
                    self.validate(value)

    def test_header_schema_requires_empty_configuration_in_helper(self):
        value = result()
        value["schema_version"] = 3
        value["workload"]["headers"] = {"names": [], "values": "redacted"}
        self.validate(value)
        value["workload"]["headers"]["names"] = ["authorization"]
        with self.assertRaises(RuntimeError):
            self.validate(value)

    def test_schema_four_accepts_only_the_closed_loop_protocol(self):
        value = result()
        value.update(schema_version=4, scheduling=None)
        value["workload"].update(model="closed_loop", arrival_schedule=None, headers={"names": [], "values": "redacted"})
        self.validate(value)
        value["workload"]["model"] = "fixed_arrival_rate"
        with self.assertRaises(RuntimeError):
            self.validate(value)

    def test_wrong_workload_and_invalid_observations(self):
        variants = []
        value = result(); value["workload"]["concurrency"] = 8; variants.append(value)
        value = result(); value["workload"]["url"] += "?other=1"; variants.append(value)
        value = result(); value["interrupted"] = True; variants.append(value)
        value = result(); value["successful_throughput_rps"] = 8; variants.append(value)
        value = result(); value["http_status_counts"] = {"204": 8}; variants.append(value)
        value = result(); value["peak_in_flight"] = 5; variants.append(value)
        value = result(); value["peak_in_flight"] = 0; variants.append(value)
        value = result(); value["success_latency_ms"]["p99"] = float("nan"); variants.append(value)
        for value in variants:
            with self.subTest(value=value), self.assertRaises(RuntimeError):
                self.validate(value)

    def test_block_summary_keeps_run_percentiles(self):
        first, second = result(), copy.deepcopy(result())
        second["elapsed_seconds"] = 4
        second["success_latency_ms"].update(mean=6, p99=9)
        summary = runner.describe_block(1, "baseline", [first, second])
        self.assertEqual(summary["measured_runs"], 2)
        self.assertEqual(summary["combined_successful_throughput_rps"], 16 / 6)
        self.assertEqual(summary["per_run_p99_ms"], [3, 9])
        self.assertEqual(summary["mean_request_latency_ms"], 4)

    def test_summed_request_time_cannot_exceed_concurrency_window(self):
        value = result()
        value['success_latency_ms'].update(min=5000, mean=5000, p50=5000, p95=5000, p99=5000, max=5000)
        with self.assertRaises(RuntimeError):
            self.validate(value)


class SafetyChecks(unittest.TestCase):
    def test_refuses_unrecognized_listener_and_reused_pid(self):
        metadata = dict(pid=123, url="http://127.0.0.1:8080", command=["/jdk/bin/java", "-jar", "/target/candidate.jar"])
        with mock.patch.object(runner, "pids", return_value={"999"}), mock.patch.object(runner.subprocess, "check_output") as inspect:
            with self.assertRaises(RuntimeError):
                runner.managed_listener(8080, metadata)
            inspect.assert_not_called()
        with mock.patch.object(runner, "pids", return_value={"123"}), mock.patch.object(runner.subprocess, "check_output", return_value="unrelated-command"):
            with self.assertRaises(RuntimeError):
                runner.managed_listener(8080, metadata)
        with mock.patch.object(runner, "pids", return_value={"123"}), mock.patch.object(runner.subprocess, "check_output", return_value="/jdk/bin/java -jar /target/candidate.jar"):
            self.assertEqual(runner.managed_listener(8080, metadata), 123)

    def test_refuses_overlapping_load_generators(self):
        with mock.patch.object(runner, "pids", return_value={"456"}), self.assertRaises(RuntimeError):
            runner.engine_idle(Path("spe"))

    def test_exclusive_runner_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "lock"
            with runner.exclusive_run(path):
                with self.assertRaises(RuntimeError):
                    with runner.exclusive_run(path):
                        self.fail("second runner acquired an active lock")
            with runner.exclusive_run(path):
                pass

    def test_existing_output_is_never_truncated(self):
        with tempfile.TemporaryDirectory() as directory:
            prefix = Path(directory) / "run-01"
            target = prefix.with_suffix(".json")
            target.write_text("previous measurement")
            with mock.patch.object(runner, "engine_idle"), mock.patch.object(runner.subprocess, "Popen") as launch:
                with self.assertRaises(RuntimeError):
                    runner.workload(Path("spe"), "http://localhost/", 8, prefix, {}, {})
                launch.assert_not_called()
            self.assertEqual(target.read_text(), "previous measurement")


class ComparisonChecks(unittest.TestCase):
    def artifact(self, directory, scope="controlled local A-B-B-A experiment; descriptive results only"):
        # Synthetic arithmetic fixtures, retained only in a temporary unit-test directory.
        manifest = dict(status="complete", scope=scope, engine_sha256="0" * 64,
            jar_sha256=dict(baseline="1" * 64, candidate="2" * 64),
            source_metadata=dict(baseline=dict(jar_sha256="1" * 64), candidate=dict(jar_sha256="2" * 64)),
            protocol=dict(measured_requests=8, warmup_requests=8, measured_runs_per_block=1,
                          concurrency=4, timeout_ms=2000, heap_mb=512), blocks=[])
        for i, variant in enumerate(runner.ORDER, 1):
            block = directory / f"{i:02d}-{variant}"
            block.mkdir()
            command = ["/java", "-Xms512m", "-Xmx512m", "-jar", f"/{variant}.jar", "--server.port=8080"]
            (block / "server-command.json").write_text(json.dumps(command))
            value = result()
            value.update(schema_version=1, engine=dict(version="unit-test-fixture"))
            seconds, mean, p99 = {1: (2, 1, 2), 2: (1, 1, 4), 3: (1, 1, 8), 4: (4, 3, 10)}[i]
            value.update(elapsed_seconds=seconds, successful_throughput_rps=8 / seconds, completed_throughput_rps=8 / seconds)
            value["success_latency_ms"].update(min=0, mean=mean, p50=1, p95=2, p99=p99, max=p99)
            (block / "run-01.json").write_text(json.dumps(value))
            warmup = copy.deepcopy(value)
            warmup["success_latency_ms"].update(min=100, mean=100, p50=100, p95=100, p99=100, max=100)
            (block / "warmup.json").write_text(json.dumps(warmup))
            manifest["blocks"].append(dict(block=i, variant=variant, measured_runs=1))
        (directory / "manifest.json").write_text(json.dumps(manifest))

    def test_weighted_metrics_and_percentiles(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            self.artifact(directory)
            report = runner.compare_results(directory)
            self.assertEqual(report["status"], "descriptive_comparison")
            self.assertEqual(set(report["checks"].values()), {"pass"})
            throughput = report["metrics"]["successful_throughput_rps"]
            self.assertAlmostEqual(throughput["baseline"], 16 / 6)
            self.assertAlmostEqual(throughput["change_percent"], 200)
            self.assertEqual(report["metrics"]["mean_request_latency_ms"]["baseline"], 2)
            self.assertEqual(report["metrics"]["mean_request_latency_ms"]["change_percent"], -50)
            self.assertEqual(report["metrics"]["median_run_p99_ms"]["baseline"], 6)
            self.assertEqual(report["totals"]["baseline"]["successful_requests"], 16)
            self.assertEqual(report["statistical_significance"], "not_assessed")
            with mock.patch("builtins.print"):
                runner.write_comparison(directory)
            self.assertEqual(json.loads((directory / "comparison.json").read_text())["status"], "descriptive_comparison")
            text = (directory / "comparison.txt").read_text()
            self.assertIn("+200.00%", text)
            self.assertIn("not pooled request percentiles", text)
            self.assertIn("Statistical significance: NOT ASSESSED", text)

    def test_incompatible_or_failed_inputs_produce_no_deltas(self):
        for kind in ("jvm", "workload", "engine", "requests", "missing_file", "fingerprint", "running"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as temporary:
                directory = Path(temporary)
                self.artifact(directory)
                if kind == "missing_file":
                    (directory / "02-candidate/run-01.json").unlink()
                elif kind in ("fingerprint", "running"):
                    path = directory / "manifest.json"
                    value = json.loads(path.read_text())
                    if kind == "fingerprint": value["jar_sha256"]["baseline"] = "3" * 64
                    else: value["status"] = "running"
                    path.write_text(json.dumps(value))
                elif kind == "jvm":
                    path = directory / "02-candidate/server-command.json"
                    value = json.loads(path.read_text()); value[1] = "-Xms256m"
                    path.write_text(json.dumps(value))
                else:
                    path = directory / "02-candidate/run-01.json"
                    value = json.loads(path.read_text())
                    if kind == "workload": value["workload"]["concurrency"] = 8
                    if kind == "engine": value["engine"]["version"] = "different"
                    if kind == "requests": value["counts"]["successes"] -= 1
                    path.write_text(json.dumps(value))
                report = runner.compare_results(directory)
                self.assertEqual(report["status"], "invalid")
                self.assertEqual(report["metrics"], {})
                self.assertIn("fail", report["checks"].values())

    def test_smoke_workloads_are_not_reported_as_performance_comparisons(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            self.artifact(directory, scope="runner lifecycle verification only")
            report = runner.compare_results(directory)
            self.assertEqual(report["status"], "functional_check_only")
            self.assertEqual(report["metrics"], {})

    def test_zero_reference_has_no_relative_percentage(self):
        self.assertIsNone(runner.percentage_change(0, 1))
        self.assertEqual(runner.percentage_change(2, 1), -50)

    def test_identical_jar_can_use_the_same_path_for_both_labels(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            self.artifact(directory)
            path = directory / 'manifest.json'
            manifest = json.loads(path.read_text())
            manifest['protocol']['same_artifact'] = 'baseline'
            manifest['same_artifact_verified'] = True
            manifest['jar_sha256']['candidate'] = manifest['jar_sha256']['baseline']
            manifest['source_metadata']['candidate'] = manifest['source_metadata']['baseline']
            manifest['artifact_paths'] = dict(baseline='/baseline.jar', candidate='/baseline.jar')
            path.write_text(json.dumps(manifest))
            for block in directory.glob('[0-9][0-9]-*'):
                command_path = block / 'server-command.json'
                command = json.loads(command_path.read_text())
                command[command.index('-jar') + 1] = '/baseline.jar'
                command_path.write_text(json.dumps(command))
            report = runner.compare_results(directory)
            self.assertEqual(report['status'], 'descriptive_comparison')

    def test_false_identical_artifact_declaration_produces_no_deltas(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            self.artifact(directory)
            path = directory / 'manifest.json'
            manifest = json.loads(path.read_text())
            manifest['protocol']['same_artifact'] = 'baseline'
            manifest['same_artifact_verified'] = True
            path.write_text(json.dumps(manifest))
            report = runner.compare_results(directory)
            self.assertEqual(report['status'], 'invalid')
            self.assertEqual(report['metrics'], {})

    def test_instrumented_experiments_have_no_performance_deltas(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            self.artifact(directory)
            path = directory / 'manifest.json'
            manifest = json.loads(path.read_text())
            manifest['diagnostic_only'] = True
            path.write_text(json.dumps(manifest))
            report = runner.compare_results(directory)
            self.assertEqual(report['status'], 'diagnostic_only')
            self.assertEqual(report['metrics'], {})

    def test_controlled_work_is_revalidated_from_server_counter_differences(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            self.artifact(directory)
            path = directory/'manifest.json'
            manifest = json.loads(path.read_text())
            manifest['jar_sha256']['candidate'] = manifest['jar_sha256']['baseline']
            manifest['source_metadata']['candidate'] = manifest['source_metadata']['baseline']
            manifest['protocol'].update(scenario='delay', conditioning_requests=8,
                variant_settings=dict(baseline=[0,0], candidate=[5_000_000,0]))
            path.write_text(json.dumps(manifest))
            for block in directory.glob('[0-9][0-9]-*'):
                delay = 5_000_000 if block.name.endswith('candidate') else 0
                (block/'applied-controls.json').write_text(json.dumps(dict(delay_nanos=delay,cpu_iterations=0)))
                (block/'conditioning.json').write_bytes((block/'warmup.json').read_bytes())
                before = dict(requests=100,delayCalls=0,cpuCalls=0,cpuIterationsExecuted=0,
                    delay_nanos=delay,cpu_iterations=0,checksum=0)
                delta = dict(requests=8,delayCalls=8 if delay else 0,cpuCalls=0,cpuIterationsExecuted=0)
                after = dict(before, **{key:before[key]+value for key,value in delta.items()})
                (block/'run-01.server-stats.json').write_text(json.dumps(dict(before=before,after=after,deltas=delta,expected=delta)))
            report = runner.compare_results(directory)
            self.assertEqual(report['status'], 'descriptive_comparison')
            self.assertEqual(report['checks']['controlled_application_work_verified'], 'pass')
            path = directory/'02-candidate/run-01.server-stats.json'
            stats = json.loads(path.read_text())
            stats['after']['delayCalls'] -= 1
            path.write_text(json.dumps(stats))
            report = runner.compare_results(directory)
            self.assertEqual(report['status'], 'invalid')
            self.assertEqual(report['metrics'], {})


class ReferenceNormalization(unittest.TestCase):
    def test_retains_reference_metric_definitions_and_empty_success_distribution(self):
        # Arithmetic fixture only; tests/validate_reference.py exercises actual HTTP.
        raw = dict(metrics={
            'attempts': dict(values=dict(count=8)),
            'outcome_http_errors': dict(values=dict(count=8)),
            'status_503': dict(values=dict(count=8)),
            'window_start_ms': dict(values=dict(min=1000)),
            'window_end_ms': dict(values=dict(max=3000)),
            'completed_http_ms': dict(values={'min': 1, 'avg': 2, 'med': 2, 'p(95)': 3, 'p(99)': 3, 'max': 3})})
        value = reference_k6.normalize(raw, 'http://localhost/', 8, 4, 2000, 'fixture')
        self.assertEqual(value['counts']['http_errors'], 8)
        self.assertEqual(value['successful_throughput_rps'], 0)
        self.assertEqual(value['completed_throughput_rps'], 4)
        self.assertEqual(value['http_status_counts'], {'503': 8})
        self.assertIsNone(value['success_latency_ms']['p99'])
        self.assertIn('excludes initial connection/DNS', value['engine']['latency_definition'])
        self.assertIn('interpolation', value['engine']['percentile_method'])
        raw['metrics']['window_end_ms']['values']['max'] = 1000
        with self.assertRaises(RuntimeError):
            reference_k6.normalize(raw, 'http://localhost/', 8, 4, 2000, 'fixture')


if __name__ == "__main__":
    unittest.main(verbosity=2)
