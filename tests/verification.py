"""Exercise untimed native response probes on real loopback HTTP and TLS."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import time
import unittest

from http_server import Handler
from https import RunningTLS, certificates
from integration import RunningService


class ProbeHandler(Handler):
    def handle_request(self):
        data = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self.server.receipts.append((self.command, self.path, data, self.headers.get("Authorization")))
        if len(self.server.receipts) == 1:
            time.sleep(self.server.probe_delay)
        status, response = self.server.responses[self.path]
        self.respond(status, response)

    do_GET = do_POST = handle_request


class VerificationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.rule = self.root / "private-rule"
        self.rule.touch(mode=0o600)

    def policy(self, *endpoints, name="fixture-v1"):
        self.rule.write_text(json.dumps(dict(policy_id=name, endpoints=endpoints)))
        return ["--verify-response", self.rule]

    def cli(self, args, code=0):
        p = subprocess.run([self.engine, *args, "--json", "-"], capture_output=True, text=True, timeout=10)
        self.assertEqual(p.returncode, code, (p.stdout, p.stderr))
        for secret in (str(self.rule), "dummy-private", "private-pointer"):
            self.assertNotIn(secret, p.stdout + p.stderr)
        return json.loads(p.stdout) if p.stdout else None

    def observe(self, svc, responses, delay=0):
        svc.server.responses = responses
        svc.server.receipts = []
        svc.server.probe_delay = delay
        svc.server.RequestHandlerClass = ProbeHandler
        return svc.server.receipts

    def test_exact_http_and_https_request_untimed(self):
        body = self.root / "payload"
        body.touch(mode=0o600)
        body.write_bytes(b"dummy-private\x00\xff")
        headers = self.root / "headers"
        headers.touch(mode=0o600)
        headers.write_text('[{"name":"Authorization","value":"Bearer dummy-private"}]')
        checks = self.policy({"fields": {"/private-pointer/0": "dummy-private", "/empty": None}})
        pki = self.root / "pki"
        certificates(pki, self.openssl)
        for service in (RunningService(), RunningTLS(pki)):
            with service as svc:
                wire = self.observe(svc, {"/v": (200, b'{"private-pointer":["dummy-private"],"empty":null}')}, delay=0.25)
                args = [svc.url+"/v", "--method", "POST", "--body-file", body, "--headers-file", headers,
                        "--requests", "2", "--concurrency", "1", *checks, "--replay-safe-post"]
                if isinstance(service, RunningTLS):
                    bad = self.cli(args + ["--ca-file", pki / "other-root.pem"], 3)
                    self.assertEqual(bad["verification"]["failure"], "tls_error")
                    self.assertFalse(wire)
                    args += ["--ca-file", pki / "root.pem"]
                start = time.monotonic()
                run = self.cli(args)
                self.assertGreater(time.monotonic() - start - run["elapsed_seconds"], .20)
                self.assertEqual(wire, [("POST", "/v", body.read_bytes(), "Bearer dummy-private")] * 3)
                self.assertEqual(run["verification"], dict(policy_id="fixture-v1", scope="untimed_preflight",
                                 status="passed", attempted=1, passed=1, failure=None))
                self.assertEqual((run["schema_version"], run["counts"]["started"], run["success_latency_ms"]["samples"]), (7, 2, 2))

    def test_bounds_redaction_and_replay(self):
        with RunningService() as svc:
            url = svc.url + "/x"
            wire = self.observe(svc, {"/x": (200, b"\x00\xff")})
            digest = hashlib.sha256(b"\x00\xff").hexdigest()
            flags = self.policy({"sha256": digest})
            for method in ("POST", "PUT", "PATCH", "DELETE"):
                self.cli([url, "--method", method, *flags], 2)
                if method != "POST":
                    self.cli([url, "--method", method, *flags, "--replay-safe-post"], 2)
            self.assertFalse(wire)
            self.cli([url, "--requests", "1", *flags])
            self.assertEqual(len(wire), 2)
            self.assertEqual(self.cli([url, "--requests", "1"])["schema_version"], 6)
            cases = [(200, b"wrong", {"sha256": digest}, "content_mismatch"),
                     (200, b'{"key":1}', {"fields": {"/private-pointer": 2}}, "content_mismatch"),
                     (200, b'{"key":1,"key":1}', {"fields": {"/key": 1}}, "invalid_json"),
                     (200, b'1\x00x', {"fields": {"": 1}}, "invalid_json"),
                     (200, b"["*40+b"0"+b"]"*40, {"fields": {"/0": 1}}, "invalid_json"),
                     (200, b"x"*(256*1024+1), {"sha256": digest}, "response_limit"),
                     (503, b"dummy-private", {"sha256": digest}, "http_error")]
            for status, payload, rule, failure in cases:
                svc.server.responses["/x"] = (status, payload)
                before = len(wire)
                doc = self.cli([url, "--rate", "5", "--duration-ms", "200", *self.policy(rule)], 3)
                self.assertEqual(len(wire), before + 1)
                self.assertEqual(doc["verification"]["failure"], failure)
                self.assertEqual(doc["elapsed_seconds"], 0)
                self.assertEqual(doc["scheduling"]["offered"], 0)
                self.assertEqual(doc["counts"]["not_started"], 1)
            a = self.root/"a.json"
            b = self.root/"b.json"
            a.write_text(json.dumps(doc))
            b.write_text(json.dumps(doc))
            self.assertEqual(self.cli(["compare", a, b], 3)["status"], "insufficient_evidence")

    def test_mix_and_private_input(self):
        manifest = self.root / "mix"
        manifest.write_text('{"endpoints":[{"path":"/a","weight":1},{"path":"/b","weight":1}]}')
        with RunningService() as svc:
            wire = self.observe(svc, {"/a": (200, b'{"id":"a"}'), "/b": (200, b'{"id":"b"}')})
            args = [svc.url, "--workload-file", manifest, "--rate", "8", "--duration-ms", "500"]
            flags = self.policy({"fields": {"/id": "a"}}, {"fields": {"/id": "b"}})
            self.rule.chmod(0o644)
            self.cli([*args, *flags], 2)
            self.rule.chmod(0o600)
            self.assertFalse(wire)
            first = self.cli([*args, *flags])
            self.assertEqual(first["verification"]["passed"], 2)
            self.assertEqual([item[1] for item in wire[:2]], ["/a", "/b"])
            self.assertEqual(len(wire), 6)
            flags = self.policy({"fields": {"/id": "a"}}, {"fields": {"/id": "wrong"}})
            result = self.cli([*args, *flags], 3)
            self.assertEqual((result["verification"]["passed"], result["counts"]["started"]), (1, 0))
            paths = [self.root / "left.json", self.root / "right.json"]
            for path in paths:
                path.write_text(json.dumps(first))
            self.assertEqual(self.cli(["compare", *paths])["status"], "descriptive_comparison")
            first["verification"]["policy_id"] = "different-fields"
            paths[1].write_text(json.dumps(first))
            self.assertEqual(self.cli(["compare", *paths], 2)["status"], "rejected")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", required=True)
    parser.add_argument("--openssl", required=True)
    args = parser.parse_args()
    VerificationTests.engine, VerificationTests.openssl = args.engine, args.openssl
    unittest.main(argv=[__file__])
