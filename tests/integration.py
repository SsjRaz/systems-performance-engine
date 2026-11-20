"""Run real HTTP experiments against a loopback service and retain raw results."""

import argparse
import datetime
import hashlib
import json
import math
import os
import platform
import signal
import socket
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path

from http_server import Handler, Server


class RunningService:
    def __enter__(self):
        self.server = Server(("127.0.0.1", 0))
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02})
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        return self

    def idle(self):
        deadline = time.monotonic() + 5
        while self.server.state.snapshot()["active"] and time.monotonic() < deadline:
            time.sleep(0.01)
        return self.server.state.snapshot()

    def __exit__(self, *_):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.idle()


class TimingService(RunningService):
    """Observe receipt on the server's monotonic clock, independently of SPE."""
    class TimedHandler(Handler):
        def do_GET(self):
            with self.server.state.lock:
                self.server.receipts.append(time.monotonic_ns())
            if self.server.processing_delay:
                time.sleep(self.server.processing_delay)
            super().do_GET()

    def __enter__(self):
        super().__enter__()
        self.server.receipts = []
        self.server.processing_delay = 0
        self.server.RequestHandlerClass = self.TimedHandler
        return self


class HttpIntegration(unittest.TestCase):
    engine = None
    artifacts = None
    observations = []

    def command(self, url, requests=12, concurrency=3, timeout=1000, extra=()):
        budget = [] if requests is None else ["--requests", str(requests)]
        return [str(self.engine), url, *budget, "--concurrency", str(concurrency),
                "--timeout-ms", str(timeout), "--json", "-", *extra]

    def retain(self, name, command, process, stdout, stderr, wall_seconds, expected):
        (self.artifacts / f"{name}.stdout").write_text(stdout)
        (self.artifacts / f"{name}.stderr").write_text(stderr)
        self.assertEqual(process.returncode, expected, (command, stdout, stderr))
        result = json.loads(stdout, parse_constant=lambda value: self.fail(f"nonfinite JSON: {value}"))
        (self.artifacts / f"{name}.json").write_text(json.dumps(result, indent=2) + "\n")
        observation = dict(name=name, command=command, returncode=process.returncode,
                           process_wall_seconds=wall_seconds, result=result)
        self.observations.append(observation)
        self.check_accounting(result)
        if result["workload"]["model"] == "fixed_arrival_rate":
            self.check_schedule(result)
        self.assertLessEqual(result["elapsed_seconds"], wall_seconds + 0.1)
        self.assertIsNone(result["engine_error"])
        return result

    def execute(self, name, url, *, requests=12, concurrency=3, timeout=1000, expected=0, extra=()):
        command = self.command(url, requests, concurrency, timeout, extra)
        # Proxies in the environment must not affect the engine's local workload.
        environment = dict(os.environ, http_proxy="http://127.0.0.1:1", HTTP_PROXY="http://127.0.0.1:1",
                           ALL_PROXY="http://127.0.0.1:1", all_proxy="http://127.0.0.1:1", NO_PROXY="", no_proxy="")
        start = time.monotonic()
        process = subprocess.run(command, text=True, capture_output=True, timeout=20, env=environment)
        result = self.retain(name, command, process, process.stdout, process.stderr,
                             time.monotonic() - start, expected)
        print(f"\n{name}: completed={result['counts']['completed']} "
              f"elapsed={result['elapsed_seconds']:.6f}s "
              f"success_p50_ms={result['success_latency_ms']['p50']}", flush=True)
        return result

    def observed(self, name, state):
        next(item for item in reversed(self.observations) if item["name"] == name)["server_observed"] = state

    def check_accounting(self, result):
        c = result["counts"]
        self.assertEqual(c["requested"], c["started"] + c["not_started"])
        self.assertEqual(c["started"], c["completed"] + c["cancelled"])
        self.assertEqual(c["completed"], sum(c[key] for key in
            ("successes", "http_errors", "connection_errors", "network_errors", "tls_errors", "timeouts", "response_limit_errors")))
        self.assertEqual(result["success_latency_ms"]["samples"], c["successes"])
        self.assertEqual(result["completed_latency_ms"]["samples"], c["completed"])
        self.assertLessEqual(result["peak_in_flight"], min(result["workload"]["concurrency"], c["requested"]))
        self.assertEqual(sum(result["http_status_counts"].values()), c["successes"] + c["http_errors"])
        self.assertGreater(result["elapsed_seconds"], 0)
        self.assertTrue(math.isclose(result["completed_throughput_rps"], c["completed"] / result["elapsed_seconds"], rel_tol=1e-12))
        self.assertTrue(math.isclose(result["successful_throughput_rps"], c["successes"] / result["elapsed_seconds"], rel_tol=1e-12))
        for key in ("success_latency_ms", "completed_latency_ms"):
            stats = result[key]
            if stats["samples"] == 0:
                self.assertTrue(all(value is None for name, value in stats.items() if name != "samples"))
            else:
                ordered = [stats[name] for name in ("min", "p50", "p95", "p99", "max")]
                self.assertEqual(ordered, sorted(ordered))
                self.assertGreaterEqual(stats["min"], 0)
                self.assertLessEqual(stats["max"], result["elapsed_seconds"] * 1000 + 1)

    def fixed_run(self, name, service, rate, duration, *, path="/ok", **opts):
        return self.execute(name, service.url + path, requests=None,
                            extra=("--rate", str(rate), "--duration-ms", str(duration)), **opts)

    def check_schedule(self, result):
        c, s, spec = result["counts"], result["scheduling"], result["workload"]["arrival_schedule"]
        self.assertEqual(c["requested"], (spec["target_rps"] * spec["duration_ms"] + 999) // 1000)
        self.assertEqual(s["offered"], c["started"] + s["missed_capacity"] + s["missed_late"])
        self.assertEqual(c["requested"], s["offered"] + s["not_evaluated"])
        self.assertEqual(s["dispatch_delay_ms"]["samples"], c["started"])
        window = min(result["elapsed_seconds"], spec["duration_ms"] / 1000)
        self.assertEqual(s["window_seconds"], window)
        self.assertAlmostEqual(s["offered_rps"], s["offered"] / window)
        self.assertAlmostEqual(s["dispatched_rps"], c["started"] / window)
        if not result["interrupted"]:
            self.assertEqual(s["not_evaluated"], 0)
            self.assertEqual(s["window_seconds"], spec["duration_ms"] / 1000)
        if c["started"]:
            self.assertLessEqual(s["dispatch_delay_ms"]["max"], result["elapsed_seconds"] * 1000 + 1e-6)

    def test_fixed_rate_idle_pacing_and_server_receipts(self):
        with TimingService() as svc:
            r = self.fixed_run("paced", svc, 8, 1000, concurrency=1)
            self.assertEqual(r["counts"]["successes"], 8)
            times = svc.server.receipts
            self.assertEqual(len(times), 8)
            offsets = [(t - times[0]) / 1e6 for t in times]
            # Functional tolerance for shared runners; exercise spacing as well as count.
            self.assertLess(max(abs(t - i * 125) for i, t in enumerate(offsets)), 100)
            self.observed("paced", dict(receipt_offsets_ms=offsets, state=svc.idle()))

    def test_fixed_rate_overload_and_comparison(self):
        with TimingService() as svc:
            fast = self.fixed_run("paced-fast", svc, 20, 1000, concurrency=1)
            self.assertEqual(fast["counts"]["successes"], 20)
            svc.server.receipts.clear()
            svc.server.processing_delay = 0.18
            slow = self.fixed_run("paced-slow", svc, 20, 1000, concurrency=1, expected=3)
            self.assertGreater(slow["scheduling"]["missed_capacity"], 0)
            self.assertEqual(slow["counts"]["successes"], len(svc.server.receipts))
            self.assertLessEqual(len(svc.server.receipts), 6)
            self.assertEqual(slow["peak_in_flight"], 1)
            process = subprocess.run([str(self.engine), "compare", self.artifacts/"paced-fast.json",
                self.artifacts/"paced-slow.json", "--json", "-"], text=True, capture_output=True, timeout=10)
            self.assertEqual(process.returncode, 3, process.stderr)
            report = json.loads(process.stdout)
            self.assertEqual(report["candidate"]["arrivals"]["missed_capacity"], slow["scheduling"]["missed_capacity"])
            self.assertTrue(all(m["change_percent"] is None for m in report["metrics"].values()))
            self.assertLess(process.stderr.index("Candidate arrivals:"), process.stderr.index("Mean request latency"))
            (self.artifacts/"paced-compare.json").write_text(process.stdout)

    def test_fixed_rate_drain_timeout_and_errors(self):
        with RunningService() as svc:
            drained = self.fixed_run("drain", svc, 1, 25, path="/ok?delay_ms=160")
            self.assertGreaterEqual(drained["elapsed_seconds"], 0.15)
            timeout = self.fixed_run("paced-timeout", svc, 5, 500, path="/ok?delay_ms=160", timeout=50, expected=3)
            self.assertEqual(timeout["counts"]["timeouts"], 3)
            failed = self.fixed_run("paced-http-error", svc, 5, 500, path="/ok?status=503", expected=3)
            self.assertEqual(failed["counts"]["http_errors"], 3)

    def test_fixed_rate_signals_while_idle_and_draining(self):
        for idle, signum in ((True, signal.SIGINT), (False, signal.SIGTERM)):
            with self.subTest(idle=idle), TimingService() as svc:
                svc.server.processing_delay = 0 if idle else 1
                duration = 10000 if idle else 50
                args = self.command(svc.url + "/ok", requests=None, extra=("--rate", "1", "--duration-ms", str(duration)))
                start = time.monotonic()
                with subprocess.Popen(args, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as child:
                    try:
                        while not svc.server.receipts and time.monotonic() - start < 4:
                            time.sleep(0.005)
                        self.assertTrue(svc.server.receipts)
                        time.sleep(0.12)
                        sent = time.monotonic()
                        child.send_signal(signum)
                        stdout, stderr = child.communicate(timeout=2)
                        self.assertLess(time.monotonic() - sent, 1.5)
                        r = self.retain("paced-" + signum.name, args, child, stdout, stderr,
                                        time.monotonic()-start, 128+signum)
                        self.assertTrue(r["interrupted"])
                        self.assertEqual(r["counts"]["cancelled"], 0 if idle else 1)
                        self.assertEqual(r["scheduling"]["not_evaluated"], 9 if idle else 0)
                    finally:
                        if child.poll() is None:
                            child.kill()
                            child.communicate()

    def test_concurrency_and_reuse(self):
        for concurrency in (1, 2, 5):
            with self.subTest(concurrency=concurrency), RunningService() as service:
                name = f"concurrency_{concurrency}"
                result = self.execute(name, service.url + "/ok?delay_ms=20", requests=30, concurrency=concurrency)
                state = service.idle()
                self.observed(name, state)
                self.assertEqual(result["counts"]["successes"], 30)
                self.assertEqual(state["received"], 30)
                self.assertEqual(state["finished"], 30)
                self.assertLessEqual(state["peak"], concurrency)
                self.assertGreaterEqual(state["peak"], min(concurrency, 2))
                self.assertLessEqual(state["connections"], concurrency)

    def test_fast_and_delayed_real_measurements(self):
        fast, slow = [], []
        # Alternate ordering to reduce a systematic first-run effect.
        for repeat in range(3):
            for kind in (("fast", "slow") if repeat % 2 == 0 else ("slow", "fast")):
                with RunningService() as service:
                    delay = 0 if kind == "fast" else 60
                    name = f"{kind}_{repeat}"
                    result = self.execute(name, service.url + f"/ok?delay_ms={delay}", requests=24, concurrency=4)
                    self.observed(name, service.idle())
                    (fast if kind == "fast" else slow).append(result["success_latency_ms"]["p50"])
        self.assertGreater(min(slow), max(fast) + 35)

    def test_elapsed_scales_with_concurrency(self):
        results = []
        for concurrency in (1, 4):
            with RunningService() as service:
                name = f"elapsed_c{concurrency}"
                result = self.execute(name, service.url + "/ok?delay_ms=30", requests=16, concurrency=concurrency)
                self.observed(name, service.idle())
                results.append(result)
        self.assertLess(results[1]["elapsed_seconds"], results[0]["elapsed_seconds"] * 0.75)

    def test_small_request_count(self):
        with RunningService() as service:
            result = self.execute("small_count", service.url + "/ok", requests=3, concurrency=10)
            self.assertEqual(result["counts"]["completed"], 3)
            self.assertEqual(service.idle()["received"], 3)

    def test_http_errors_and_redirects(self):
        for status in (302, 403, 503):
            with self.subTest(status=status), RunningService() as service:
                name = f"status_{status}"
                result = self.execute(name, service.url + f"/ok?status={status}", expected=3)
                self.observed(name, service.idle())
                self.assertEqual(result["counts"]["http_errors"], 12)
                self.assertEqual(result["http_status_counts"], {str(status): 12})
                self.assertEqual(service.server.state.snapshot()["received"], 12)

    def test_mixed_outcomes(self):
        with RunningService() as service:
            result = self.execute("mixed", service.url + "/mixed", requests=20, expected=3)
            self.assertEqual(result["counts"]["successes"], 10)
            self.assertEqual(result["counts"]["http_errors"], 10)

    def test_timeout(self):
        with RunningService() as service:
            result = self.execute("timeouts", service.url + "/ok?delay_ms=250", requests=9, timeout=50, expected=3)
            self.observed("timeouts", service.idle())
            self.assertEqual(result["counts"]["timeouts"], 9)
            self.assertGreaterEqual(result["completed_latency_ms"]["p50"], 35)
            self.assertLess(result["completed_latency_ms"]["p99"], 1500)

    def test_body_completion_and_body_timeout(self):
        with RunningService() as service:
            result = self.execute("slow_body", service.url + "/slow-body?delay_ms=80", requests=6)
            self.assertGreaterEqual(result["success_latency_ms"]["p50"], 60)
        with RunningService() as service:
            result = self.execute("slow_body_timeout", service.url + "/slow-body?delay_ms=250",
                                  requests=6, timeout=50, expected=3)
            self.assertEqual(result["counts"]["timeouts"], 6)

    def test_refused_connections(self):
        # Select an OS-allocated port, then release it before connecting. macOS
        # can silently drop SYNs to a bound non-listening socket instead of RST.
        with socket.socket() as reserved:
            reserved.bind(("127.0.0.1", 0))
            url = f"http://127.0.0.1:{reserved.getsockname()[1]}/"
            reserved.close()
            result = self.execute("connection_refused", url, requests=9, expected=3)
            self.assertEqual(result["counts"]["connection_errors"], 9)

    def test_broken_responses(self):
        for endpoint in ("disconnect", "truncated", "malformed"):
            with self.subTest(endpoint=endpoint), RunningService() as service:
                result = self.execute(endpoint, service.url + "/" + endpoint, requests=6, expected=3)
                self.observed(endpoint, service.idle())
                self.assertEqual(result["counts"]["network_errors"], 6)

    def test_response_body_limit_and_chunked(self):
        with RunningService() as service:
            result = self.execute("body_limit", service.url + "/ok?bytes=1024", requests=6,
                                  expected=3, extra=("--max-body-bytes", "32"))
            self.assertEqual(result["counts"]["response_limit_errors"], 6)
        with RunningService() as service:
            result = self.execute("chunked", service.url + "/chunked", requests=9)
            self.assertEqual(result["counts"]["successes"], 9)
        with RunningService() as service:
            result = self.execute("chunked_limit", service.url + "/chunked", requests=6,
                                  expected=3, extra=("--max-body-bytes", "5"))
            self.assertEqual(result["counts"]["response_limit_errors"], 6)

    def test_connection_close_policy(self):
        with RunningService() as service:
            result = self.execute("server_closes", service.url + "/ok?close=1", requests=9)
            state = service.idle()
            self.observed("server_closes", state)
            self.assertEqual(result["counts"]["successes"], 9)
            self.assertEqual(state["received"], 9)
            self.assertEqual(state["connections"], 9)

    def test_signal_shutdown(self):
        for signum in (signal.SIGINT, signal.SIGTERM):
            with self.subTest(signal=signum), RunningService() as service:
                name = f"shutdown_{signum.name}"
                completed_before_signal = 0 if signum == signal.SIGINT else 4
                endpoint = "/ok?delay_ms=2000" if signum == signal.SIGINT else "/phased"
                command = self.command(service.url + endpoint, requests=100, concurrency=3, timeout=10000)
                start = time.monotonic()
                process = subprocess.Popen(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                try:
                    deadline = time.monotonic() + 5
                    while time.monotonic() < deadline:
                        state = service.server.state.snapshot()
                        if state["active"] == 3 and state["finished"] >= completed_before_signal:
                            break
                        self.assertIsNone(process.poll(), "engine exited before requests reached the server")
                        time.sleep(0.005)
                    self.assertEqual(service.server.state.snapshot()["active"], 3)
                    signal_at = time.monotonic()
                    process.send_signal(signum)
                    stdout, stderr = process.communicate(timeout=5)
                    result = self.retain(name, command, process, stdout, stderr, time.monotonic() - start, 128 + signum)
                    self.assertLess(time.monotonic() - signal_at, 1.5)
                    self.assertTrue(result["interrupted"])
                    self.assertEqual(result["counts"]["completed"], completed_before_signal)
                    self.assertEqual(result["counts"]["successes"], completed_before_signal)
                    self.assertEqual(result["counts"]["cancelled"], 3)
                    self.assertEqual(result["counts"]["not_started"], 97 - completed_before_signal)
                    self.observed(name, service.server.state.snapshot())
                finally:
                    if process.poll() is None:
                        process.kill()
                        process.communicate()

    def test_json_file_and_cli_errors(self):
        with RunningService() as service:
            path = self.artifacts / f"file_output-{time.monotonic_ns()}.json"
            command = [str(self.engine), service.url + "/ok", "--requests", "5", "--json", str(path)]
            process = subprocess.run(command, text=True, capture_output=True, timeout=10)
            self.assertEqual(process.returncode, 0, process.stderr)
            self.assertIn("Completed 5/5", process.stdout)
            self.check_accounting(json.loads(path.read_text()))
            self.assertEqual(service.idle()["received"], 5)
            original = path.read_bytes()
            repeated = subprocess.run(command, text=True, capture_output=True, timeout=10)
            self.assertEqual(repeated.returncode, 1)
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(service.idle()["received"], 5, "existing output must fail before sending requests")
        with RunningService() as service:
            process = subprocess.run([str(self.engine), service.url, "--json", str(self.artifacts / "missing" / "output.json")],
                                     text=True, capture_output=True, timeout=10)
            self.assertEqual(process.returncode, 1)
            self.assertEqual(service.server.state.snapshot()["received"], 0)
        for arguments in (("ftp://localhost/",), ("http://localhost/", "--concurrency", "0"), ("--unknown",)):
            process = subprocess.run([str(self.engine), *arguments], text=True, capture_output=True, timeout=10)
            self.assertEqual(process.returncode, 2)
        for option in ("--version", "--help"):
            process = subprocess.run([str(self.engine), option], text=True, capture_output=True, timeout=10)
            self.assertEqual(process.returncode, 0)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", type=Path, required=True)
    parser.add_argument("--artifacts", type=Path, required=True)
    args = parser.parse_args()
    HttpIntegration.engine = args.engine.resolve()
    HttpIntegration.artifacts = args.artifacts.resolve()
    HttpIntegration.artifacts.mkdir(parents=True, exist_ok=True)
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(HttpIntegration)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    evidence = dict(scope="controlled loopback HTTP/1.1 experiments, not representative application benchmarks",
                    recorded_at_utc=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    environment=dict(platform=platform.platform(), machine=platform.machine(),
                                     python=platform.python_version(), logical_cpus=os.cpu_count()),
                    tests_run=result.testsRun, failures=len(result.failures), errors=len(result.errors),
                    engine_sha256=hashlib.sha256(HttpIntegration.engine.read_bytes()).hexdigest(),
                    fixture_sha256=hashlib.sha256(Path(__file__).with_name("http_server.py").read_bytes()).hexdigest(),
                    observations=HttpIntegration.observations)
    (HttpIntegration.artifacts / "observations.json").write_text(json.dumps(evidence, indent=2) + "\n")
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
