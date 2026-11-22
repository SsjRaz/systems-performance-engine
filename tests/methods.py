"""Verify HTTP verbs, request framing, replay policy and saved contracts on loopback."""
import argparse
import hashlib
import json
import os
import signal
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path

from http_server import Handler
from https import RunningTLS, certificates
import integration
from integration import RunningService


class WireHandler(Handler):
    def receive(self):
        body = self.rfile.read(int(self.headers.get('Content-Length', '0')))
        with self.server.state.lock:
            self.server.receipts.append(dict(method=self.command, path=self.path, body=body, port=self.client_address[1],
                lengths=self.headers.get_all('Content-Length', []), types=self.headers.get_all('Content-Type', []),
                expect=self.headers.get('Expect'), transfer=self.headers.get('Transfer-Encoding'),
                auth=self.headers.get('Authorization')))
            ordinal = len(self.server.receipts)
        if self.path == '/hold-response':
            self.server.body_received.set()
            self.server.release_response.wait(10)
            self.close_connection = True
        elif self.path == '/lost-response' or (self.path == '/lose-second-response' and ordinal == 2):
            self.close_connection = True  # The server has already consumed the write.
        elif self.command == 'HEAD':
            self.send_response(200)
            self.send_header('Content-Length', '1234')
            self.end_headers()
        else:
            self.respond({'/redirect': 307, '/expectation-failed': 417}.get(self.path, 200), b'ok')

    do_HEAD = do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = receive


def observe(service):
    service.server.receipts = []
    service.server.body_received = threading.Event()
    service.server.release_response = threading.Event()
    service.server.RequestHandlerClass = WireHandler
    return service.server.receipts


class Methods(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(tmp.cleanup)
        cls.files = Path(tmp.name)
        cls.certs = cls.files / 'pki'
        certificates(cls.certs, str(cls.openssl))

    def private_file(self, name, data):
        path = self.files / name
        path.touch(mode=0o600, exist_ok=False)
        path.write_bytes(data)
        return path

    def report(self, returncode, stdout, stderr, code, private):
        for value in private:
            self.assertNotIn(str(value), stdout + stderr)
        self.assertEqual(returncode, code, (stdout, stderr))
        return json.loads(stdout) if stdout else None

    def cli(self, args, code, private=()):
        run = subprocess.run([self.engine, *args], capture_output=True, text=True, timeout=10)
        return self.report(run.returncode, run.stdout, run.stderr, code, private)

    def workload(self, url, *args, code=0, private=()):
        result = self.cli([url, *args, '--json', '-'], code, private)
        if result is not None:
            integration.HttpIntegration.check_accounting(self, result)
        return result

    def test_binary_methods_and_default_write_budget(self):
        data = bytes(range(256)) * 256  # Exactly 64 KiB, including embedded NULs.
        body = self.private_file('payload', data)
        with RunningService() as svc:
            wire = observe(svc)
            for method in ('POST', 'PUT', 'PATCH'):
                wire.clear()
                r = self.workload(svc.url, '--method', method, '--body-file', body, private=[body])
                self.assertEqual(r['counts']['requested'], 1)
                self.assertEqual(r['workload']['method'], method)
                self.assertEqual(r['workload']['request_body_bytes'], len(data))
                self.assertFalse(r['workload']['connection_reuse'])
                self.assertEqual(len(wire), 1)
                self.assertEqual((wire[0]['method'], wire[0]['body']), (method, data))
                self.assertEqual(wire[0]['lengths'], ['65536'])
                self.assertEqual(wire[0]['types'], ['application/octet-stream'])
                self.assertIsNone(wire[0]['expect'])
                self.assertIsNone(wire[0]['transfer'])
            wire.clear()
            empty = self.private_file('empty-body', b'')
            self.workload(svc.url, '--method', 'POST', '--body-file', empty)
            self.workload(svc.url, '--method', 'DELETE')
            self.assertEqual([(x['method'], x['body']) for x in wire], [('POST', b''), ('DELETE', b'')])
            self.assertEqual(wire[0]['lengths'], ['0'])

    def test_head_reuse_and_response_framing(self):
        with RunningService() as svc:
            wire = observe(svc)
            r = self.workload(svc.url, '--method', 'HEAD', '--requests', '3', '--concurrency', '1',
                              '--max-body-bytes', '1')
            self.assertTrue(r['workload']['connection_reuse'])
            self.assertEqual([x['method'] for x in wire], ['HEAD'] * 3)
            self.assertEqual(len({x['port'] for x in wire}), 1)

    def test_body_snapshot_survives_file_change(self):
        body = self.private_file('snapshot', b'original')
        pair = threading.Barrier(2, timeout=4)

        class RewriteAfterDispatch(WireHandler):
            def do_POST(self):
                if pair.wait() == 0:
                    body.write_bytes(b'changed before next dispatch')
                self.receive()

        with RunningService() as svc:
            wire = observe(svc)
            svc.server.RequestHandlerClass = RewriteAfterDispatch
            self.workload(svc.url, '--method', 'POST', '--body-file', body, '--requests', '4',
                          '--concurrency', '2', '--timeout-ms', '5000')
            self.assertEqual([x['body'] for x in wire], [b'original'] * 4)

    def test_no_write_replay_after_lost_response_redirect_or_417(self):
        body = self.private_file('write', b'dummy transaction')
        with RunningService() as svc:
            wire = observe(svc)
            for path, failure in (('/ok', None), ('/lost-response', 'network_errors'),
                                  ('/redirect', 'http_errors'), ('/expectation-failed', 'http_errors')):
                wire.clear()
                r = self.workload(svc.url + path, '--method', 'POST', '--requests', '3',
                                  '--concurrency', '1', '--body-file', body, code=3 if failure else 0)
                self.assertEqual(r['counts'][failure or 'successes'], 3)
                self.assertEqual([(x['method'], x['path'], x['body']) for x in wire],
                                 [('POST', path, b'dummy transaction')] * 3)
                self.assertEqual(len({x['port'] for x in wire}), 3)

    def test_private_https_and_fixed_rate(self):
        payload = 'dummy-private-payload'
        token = 'dummy-private-authorization'
        body = self.private_file('private-body', payload.encode())
        headers = self.private_file('private-headers', json.dumps([
            dict(name='Content-Type', value='application/json'), dict(name='Authorization', value=token)
        ]).encode())
        with RunningTLS(self.certs) as svc:
            wire = observe(svc)
            flags = ['--method', 'PATCH', '--body-file', body, '--headers-file', headers]
            hidden = (body, payload, token, headers)
            failed = self.workload(svc.url, *flags, code=3, private=hidden)
            self.assertEqual(failed['counts']['tls_errors'], 1)
            self.assertEqual(wire, [])
            r = self.workload(svc.url, *flags, '--ca-file', self.certs/'root.pem',
                              '--rate', '4', '--duration-ms', '450', private=hidden)
            self.assertEqual(r['scheduling']['offered'], 2)
            self.assertEqual([(x['types'], x['body'], x['auth']) for x in wire],
                             [(['application/json'], payload.encode(), token)] * 2)

    def test_second_write_loses_response_after_success(self):
        data = b'wire-test\x00\xff'
        body = self.private_file('two-writes', data)
        for method in ('POST', 'PUT', 'PATCH', 'DELETE'):
            with self.subTest(method=method), RunningService() as svc:
                wire = observe(svc)
                payload = [] if method == 'DELETE' else ['--body-file', body]
                r = self.workload(svc.url + '/lose-second-response', '--method', method,
                                  '--requests', '2', '--concurrency', '1', *payload, code=3)
                self.assertEqual(r['counts']['successes'], 1)
                self.assertEqual(r['counts']['network_errors'], 1)
                self.assertEqual([(x['method'], x['body']) for x in wire],
                                 [(method, b'' if method == 'DELETE' else data)] * 2)
                self.assertEqual(len({x['port'] for x in wire}), 2)

    def test_https_timeout_after_body_received(self):
        marker = 'timeout-private-payload'
        data = marker.encode() + b'\x00\xff'
        body = self.private_file('timeout-body', data)
        ca = self.certs/'root.pem'
        with RunningTLS(self.certs) as svc:
            wire = observe(svc)
            try:
                r = self.workload(svc.url + '/hold-response', '--method', 'PUT', '--body-file', body,
                                  '--ca-file', ca, '--timeout-ms', '750', code=3,
                                  private=(marker, body, ca, hashlib.sha256(data).hexdigest()))
                self.assertTrue(svc.server.body_received.is_set())
                self.assertEqual([(x['method'], x['body']) for x in wire], [('PUT', data)])
                self.assertFalse(r['interrupted'])
                self.assertEqual(r['counts']['requested'], 1)
                self.assertEqual(r['counts']['timeouts'], 1)
                self.assertEqual(r['counts']['completed'], 1)
                self.assertEqual(r['workload']['request_body_bytes'], len(data))
            finally:
                svc.server.release_response.set()

    def test_cancel_after_body_received(self):
        marker = 'cancel-private-payload'
        data = marker.encode() + b'\x00\xff'
        body = self.private_file('cancel-body', data)
        for budget in (['--requests', '4'], ['--rate', '1', '--duration-ms', '4000']):
            with self.subTest(budget=budget), RunningService() as svc:
                wire = observe(svc)
                args = [self.engine, svc.url + '/hold-response', '--method', 'PATCH', '--body-file', body,
                        *budget, '--concurrency', '1', '--timeout-ms', '8000', '--json', '-']
                with subprocess.Popen(args, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as child:
                    try:
                        self.assertTrue(svc.server.body_received.wait(5))
                        child.send_signal(signal.SIGTERM)
                        stdout, stderr = child.communicate(timeout=3)
                        r = self.report(child.returncode, stdout, stderr, 128 + signal.SIGTERM,
                                        (marker, body, hashlib.sha256(data).hexdigest()))
                    finally:
                        svc.server.release_response.set()
                        if child.poll() is None:
                            child.kill()
                            child.communicate()
                integration.HttpIntegration.check_accounting(self, r)
                if r['workload']['model'] == 'fixed_arrival_rate':
                    integration.HttpIntegration.check_schedule(self, r)
                self.assertTrue(r['interrupted'])
                self.assertEqual(r['counts']['requested'], 4)
                self.assertEqual(r['counts']['cancelled'], 1)
                self.assertEqual(r['counts']['completed'], 0)
                self.assertEqual(r['counts']['not_started'], 3)
                self.assertEqual([(x['method'], x['body']) for x in wire], [('PATCH', data)])

    def test_invalid_inputs_are_rejected_before_dispatch(self):
        oversized = self.private_file('oversized', b'x' * 65537)
        broad = self.private_file('broad', b'dummy')
        broad.chmod(0o644)
        fifo = self.files/'pipe'
        os.mkfifo(fifo, 0o600)
        definition = self.private_file('mix', b'{"endpoints":[{"path":"/a","weight":1},{"path":"/b","weight":1}]}')
        with RunningService() as svc:
            wire = observe(svc)
            for flags in (['--method', 'TRACE'], ['--method', 'post'], ['--method', 'HEAD', '--body-file', oversized],
                          ['--body-file', oversized], ['--method', 'DELETE', '--body-file', oversized],
                          ['--method', 'POST', '--workload-file', definition, '--rate', '1', '--duration-ms', '5'],
                          *(['--method', 'PUT', '--body-file', path] for path in (oversized, broad, fifo))):
                self.workload(svc.url, *flags, code=2, private=(oversized, broad, fifo, definition))
            self.assertEqual(wire, [])

    def test_compare_redacted_bodies(self):
        old = self.files/'baseline.json'
        new = self.files/'candidate.json'
        with RunningService() as svc:
            observe(svc)
            body = self.private_file('redacted', b'1111')
            for path, data in ((old, b'1111'), (new, b'2222')):
                body.write_bytes(data)
                run = self.workload(svc.url, '--method', 'POST', '--body-file', body)
                path.write_text(json.dumps(run))
        args = ['compare', old, new, '--json', '-']
        self.cli(args, 2)
        self.assertTrue(self.cli([*args, '--allow-redacted-body'], 0)['redacted_body_acknowledged'])
        for field, value in (('request_body_bytes', 3), ('method', 'PUT'), ('method', 'GET'),
                             ('connection_reuse', True), ('request_body_bytes', 65537)):
            new.write_text(json.dumps(dict(run, workload=dict(run['workload'], **{field: value}))))
            self.cli([*args, '--allow-redacted-body'], 2)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--engine', type=Path, required=True)
    parser.add_argument('--openssl', type=Path, required=True)
    parser.add_argument('--artifacts', type=Path, required=True)
    args = parser.parse_args()
    Methods.engine = args.engine.resolve()
    Methods.openssl = args.openssl.resolve()
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(Methods))
    args.artifacts.mkdir(parents=True, exist_ok=True)
    (args.artifacts/'observations.json').write_text(json.dumps(dict(tests_run=result.testsRun,
        successful=result.wasSuccessful()), indent=2) + '\n')
    raise SystemExit(0 if result.testsRun and result.wasSuccessful() else 1)
