"""Exercise spe study through the public CLI, real Git commits and loopback HTTP."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest


EXAMPLE = Path(__file__).resolve().parents[1] / "examples/git-study"
RECORD_START = """
import json, os, sys
with open(sys.argv[1], 'a') as record:
    print(json.dumps([os.getpid(), os.getcwd(), os.environ['SPE_INSTANCE_DIR'], sys.argv[2]]), file=record)
os.execv(sys.executable, [sys.executable, 'catalog.pyz', sys.argv[2]])
"""


class RevisionStudy(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.repo = self.root / "working copy"
        shutil.copytree(EXAMPLE, self.repo)
        self.config = json.loads((self.repo / "spe.json").read_text())
        self.config["measurement"].update(pairs=6, requests=8, seed=581, meaningful_change_percent=40)
        self.starts = self.root / "instance record"
        self.config["start"] = [sys.executable, "-c", RECORD_START, str(self.starts), "{port}"]
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "Study fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        self.git("add", ".")
        self.git("commit", "-qm", "Baseline lookup")
        self.base = self.git("rev-parse", "HEAD")
        self.git("switch", "-qc", "slower")
        cost = self.repo / "service/cost.py"
        cost.write_text("DELAY_MS = 18\n")
        # Only the caller's explicit config should supply commands for either arm.
        (self.repo / "spe.json").write_text("{}\n")
        self.git("commit", "-qam", "Add a controlled lookup delay")
        self.candidate = self.git("rev-parse", "HEAD")
        cost.write_text("raise RuntimeError('uncommitted source')\n")
        (self.repo / "staged").touch()
        self.git("add", "staged")
        (self.repo / "untracked").touch()
        self.snapshot = [self.git(*args) for args in self.state_commands()]
        self.out = self.root / "results"
        self.config_path = self.root / "local config.json"
        self.scratch = self.root / "scratch"
        self.scratch.mkdir()
        self.env = dict(os.environ, TMPDIR=str(self.scratch))

    def git(self, *args):
        return subprocess.check_output(
            ["git", "-c", "maintenance.auto=false", *args], cwd=self.repo, text=True
        ).strip()

    def state_commands(self):
        return [
            ("rev-parse", "HEAD"),
            ("status", "--porcelain"),
            ("diff",),
            ("diff", "--cached"),
            ("worktree", "list", "--porcelain"),
        ]

    def command(self, baseline="main", candidate="HEAD"):
        self.config_path.write_text(json.dumps(self.config))
        return [
            self.engine,
            "study",
            "--baseline",
            baseline,
            "--candidate",
            candidate,
            "--config",
            self.config_path,
            "--output",
            self.out,
        ]

    def invoke(self, expected=0, **revs):
        result = subprocess.run(
            self.command(**revs), cwd=self.repo, env=self.env, capture_output=True, text=True, timeout=60
        )
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        self.assert_clean()
        return result

    def assert_clean(self):
        self.assertEqual([self.git(*args) for args in self.state_commands()], self.snapshot)
        self.assertEqual(list(self.scratch.iterdir()), [])
        if self.starts.exists():
            for pid, checkout, instance, port in self.records():
                self.assertFalse(Path(checkout).exists())
                self.assertFalse(Path(instance).exists())
                with self.assertRaises(ProcessLookupError):
                    os.kill(pid, 0)
                with socket.socket() as probe:
                    self.assertNotEqual(probe.connect_ex(("127.0.0.1", int(port))), 0)

    def records(self):
        return [json.loads(line) for line in self.starts.read_text().splitlines()]

    def read(self, name):
        return json.loads((self.out / name).read_text())

    def test_regression_and_replay(self):
        run = self.invoke()
        report = self.read("report.json")
        self.assertEqual(
            (report["verdict"], report["baseline_version"], report["candidate_version"]),
            ("regression", self.base, self.candidate),
        )
        self.assertIn("median paired change", run.stdout)
        self.assertFalse(report["ci_gate"])
        self.assertGreater(report["interval_percent"][0], 40)
        self.assertEqual(self.read("status.json")["state"], "complete")
        self.assertEqual(len(list(self.out.glob("build-*.log"))), 2)
        blocks = self.read("manifest.json")["blocks"]
        self.assertEqual(sum(block["runs"][0]["arm"] == "baseline" for block in blocks), 3)
        records = self.records()
        self.assertEqual(len({pid for pid, *_ in records}), 12)
        self.assertEqual(len({instance for _, _, instance, _ in records}), 12)
        self.assertEqual(len({port for *_, port in records}), 1)
        for row, record in zip((row for block in blocks for row in block["runs"]), records, strict=True):
            self.assertEqual(Path(record[1]).name, report[row["arm"] + "_version"])
            native = self.read(row["result"])
            self.assertEqual(native["verification"]["status"], "passed")
            self.assertEqual(native["counts"]["successes"], 8)
        replay = subprocess.check_output(
            [self.engine, "analyze", self.out / "manifest.json", "--json", "-"],
            stderr=subprocess.DEVNULL,
        )
        self.assertEqual(json.loads(replay), report)

    def test_same_commit(self):
        self.config["expected_response"] = {"sha256": hashlib.sha256(b'{"answer":42}').hexdigest()}
        self.invoke(baseline="slower")
        report = self.read("report.json")
        self.assertEqual(report["baseline_version"], report["candidate_version"])
        self.assertEqual(report["verdict"], "inconclusive")
        self.assertEqual(len(list(self.out.glob("build-*.log"))), 1)
        self.assertEqual(len(self.records()), 12)

    def test_wrong_response(self):
        self.config["expected_response"] = {"fields": {"/answer": 43}}
        result = self.invoke(3)
        self.assertIn("content_mismatch", result.stderr)
        self.assertEqual(self.read("status.json")["state"], "failed")
        files = list(self.out.glob("pair-*.json"))
        self.assertEqual(len(files), 1)
        self.assertEqual(json.loads(files[0].read_text())["counts"]["started"], 0)
        self.assertFalse((self.out / "report.json").exists())

    def test_failed_build_and_existing_output(self):
        self.config["build"].append([sys.executable, "-c", "raise SystemExit(17)"])
        self.assertIn("build command exited with status 17", self.invoke(1).stderr)
        self.assertFalse(self.starts.exists())
        before = (self.out / "status.json").read_bytes()
        self.invoke(1)
        self.assertEqual((self.out / "status.json").read_bytes(), before)

    def test_readiness_timeout(self):
        self.config["readiness"] = dict(path="/absent", timeout_ms=400)
        self.assertIn("readiness timed out", self.invoke(1).stderr)
        self.assertEqual(len(self.records()), 1)
        self.assertFalse(list(self.out.glob("pair-*.json")))

    def test_slow_healthy_readiness(self):
        self.config.update(
            build=[],
            start=[sys.executable, str(Path(__file__).with_name("http_server.py")), "--port", "{port}"],
            readiness=dict(path="/ok?delay_ms=500", timeout_ms=3000),
            endpoint="/ok",
            expected_response={"sha256": "0" * 64},
        )
        # The slow health check should succeed and reach the intentionally failing content check.
        self.assertIn("content_mismatch", self.invoke(3).stderr)

    def test_occupied_port(self):
        with socket.socket() as occupied:
            occupied.bind(("127.0.0.1", 0))
            occupied.listen()
            self.config["port"] = occupied.getsockname()[1]
            self.invoke(1)
            self.assertFalse(self.starts.exists())
            occupied.setblocking(False)
            with self.assertRaises(BlockingIOError):
                occupied.accept()

    def test_invalid_revision_and_config(self):
        self.invoke(1, candidate="unresolved-revision")
        self.assertFalse(list(self.out.glob("build-*.log")))
        self.out = self.root / "other-results"
        self.config["endpoint"] = "https://example.invalid/lookup"
        self.invoke(2)
        self.assertFalse(self.out.exists())
        self.config["endpoint"] = "/lookup"
        self.config["start"] = "python3 catalog.pyz"
        self.invoke(2)
        self.assertFalse(self.out.exists())

    def test_config_must_be_a_regular_file(self):
        args = self.command()
        fifo = self.root / "config-pipe"
        os.mkfifo(fifo)
        args[args.index("--config") + 1] = fifo
        result = subprocess.run(
            args, cwd=self.repo, env=self.env, capture_output=True, text=True, timeout=5
        )
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("regular JSON file", result.stderr)
        self.assertFalse(self.out.exists())

    def interrupt_at(self, marker, signum):
        child = subprocess.Popen(
            self.command(),
            cwd=self.repo,
            env=self.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        try:
            deadline = time.monotonic() + 10
            while not marker() and time.monotonic() < deadline and child.poll() is None:
                time.sleep(0.02)
            self.assertTrue(marker(), "study failed to reach the planned interruption point")
            child.send_signal(signum)
            log, _ = child.communicate(timeout=8)
            self.assertEqual(child.returncode, 128 + signum, log)
        finally:
            if child.poll() is None:
                child.terminate()
                child.wait(timeout=8)
        self.assertEqual(self.read("status.json")["state"], "interrupted")
        self.assertFalse((self.out / "report.json").exists())
        self.assert_clean()

    def test_sigint_during_measurement(self):
        self.config["measurement"]["requests"] = 100000

        def measuring():
            status = self.out / "status.json"
            return (
                status.exists()
                and " measurement " in self.read("status.json")["stage"]
                and any(self.out.glob("pair-*.json"))
            )

        self.interrupt_at(measuring, signal.SIGINT)

    def test_sigterm_during_build_cleans_descendants(self):
        # The builder exits on TERM. Its worker ignores TERM and owns a socket.
        marker = self.root / "worker"
        worker = (
            "import json,os,signal,socket,sys,time\nfrom pathlib import Path\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "s=socket.socket(); s.bind(('127.0.0.1',0)); s.listen()\n"
            "Path(sys.argv[1]).write_text(json.dumps([os.getpid(),s.getsockname()[1]])); time.sleep(30)"
        )
        builder = "import subprocess,sys,time; subprocess.Popen([sys.executable,'-c',*sys.argv[1:]]); time.sleep(30)"
        self.config["build"] = [[sys.executable, "-c", builder, worker, str(marker)]]
        self.interrupt_at(lambda: marker.exists() and bool(marker.read_text()), signal.SIGTERM)
        pid, port = json.loads(marker.read_text())
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", port))
        if sys.platform.startswith("linux"):
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--engine", required=True, type=Path)
    RevisionStudy.engine = parser.parse_args().engine.resolve()
    unittest.main(argv=[__file__])
