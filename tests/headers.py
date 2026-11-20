"""Real authenticated HTTP/HTTPS requests with dummy credentials; no capacity claims."""
import argparse
import datetime
import hashlib
import json
import os
import signal
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path

from http_server import Handler
from integration import HttpIntegration, RunningService
from https import RunningTLS, certificates


AUTH = 'Bearer spe-dummy-token-for-local-tests-only'
API_KEY = 'spe-dummy-api-key-for-local-tests-only'
EXPECTED = {'authorization': AUTH, 'x-api-key': API_KEY, 'accept': 'application/json',
            'x-application': 'example: local test', 'x-empty': ''}


class HeaderHandler(Handler):
    def do_GET(self):
        expected = self.server.expected_headers
        counts = {name: len(self.headers.get_all(name, [])) for name in expected}
        matched = all(self.headers.get_all(name, []) == [value] for name, value in expected.items())
        with self.server.header_lock:
            self.server.header_checks.append(dict(matched=matched, counts=counts, method=self.command, path=self.path))
        if not matched:
            self.respond(401, b'unauthorized')  # No reflection of credentials, even on failure.
            return
        if self.path == '/echo-private':
            self.respond(200, self.headers['authorization'].encode())
            return
        super().do_GET()


def configure(service, expected=EXPECTED):
    service.server.expected_headers = dict(expected)
    service.server.header_lock = threading.Lock()
    service.server.header_checks = []
    service.server.RequestHandlerClass = HeaderHandler
    return service


def audit(service):
    with service.server.header_lock:
        checks = list(service.server.header_checks)
    return dict(requests=len(checks), matched=sum(r['matched'] for r in checks),
                all_single_fields=all(all(n == 1 for n in r['counts'].values()) for r in checks),
                methods=sorted({r['method'] for r in checks}))


class HeadersIntegration(unittest.TestCase):
    engine = artifacts = certs = None
    observations = []
    sequence = 0

    def private(self, headers=None, raw=None):
        type(self).sequence += 1
        path = self.artifacts/'dummy-inputs'/f'headers-{self.sequence}.json'
        path.parent.mkdir(exist_ok=True)
        data = raw if raw is not None else json.dumps(
            [dict(name=name, value=value) for name, value in (EXPECTED if headers is None else headers).items()]).encode()
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'wb') as f:
            f.write(data)
        return path

    def command(self, url, header_file, requests=20, concurrency=3, timeout=2000, output='-', fixed_rate=None, workload_file=None):
        workload = (['--requests', str(requests)] if fixed_rate is None else
                    ['--rate', str(fixed_rate[0]), '--duration-ms', str(fixed_rate[1])])
        args = [str(self.engine), url, *workload, '--concurrency', str(concurrency),
                '--timeout-ms', str(timeout), '--headers-file', str(header_file), '--json', str(output)]
        if workload_file is not None:
            args += ['--workload-file', str(workload_file)]
        if url.startswith('https:'):
            args += ['--ca-file', str(self.certs/'root.pem')]
        self.assertNotIn(AUTH, args)
        self.assertNotIn(API_KEY, args)
        return args

    def retain(self, name, command, process, stdout, stderr, expected=0, output=None):
        for private in (AUTH, API_KEY, hashlib.sha256(AUTH.encode()).hexdigest(), hashlib.sha256(API_KEY.encode()).hexdigest()):
            self.assertNotIn(private, stdout+stderr)
        prefix = self.artifacts/name
        prefix.with_suffix('.stdout').write_text(stdout)
        prefix.with_suffix('.stderr').write_text(stderr)
        prefix.with_suffix('.command.json').write_text(json.dumps(command, indent=2)+'\n')
        self.assertEqual(process.returncode, expected, (name, stdout, stderr))
        result = json.loads(stdout if output is None else output.read_text())
        exported = json.dumps(result)
        for private in (AUTH, API_KEY, str(command[command.index('--headers-file')+1])):
            self.assertNotIn(private, exported)
        self.assertEqual(result['schema_version'], 5)
        self.assertEqual(result['workload']['headers'], dict(names=sorted(EXPECTED), values='redacted'))
        HttpIntegration.check_accounting(self, result)
        if output is None:
            prefix.with_suffix('.json').write_text(json.dumps(result, indent=2)+'\n')
        self.observations.append(dict(name=name, returncode=process.returncode, result=result))
        return result

    def execute(self, name, url, header_file, expected=0, output=None, **options):
        command = self.command(url, header_file, output='-' if output is None else output, **options)
        process = subprocess.run(command, text=True, capture_output=True, timeout=15)
        return self.retain(name, command, process, process.stdout, process.stderr, expected, output)

    def test_authenticated_http_and_https_concurrency_and_reuse(self):
        private = self.private()
        for secure in (False, True):
            for concurrency in (1, 4):
                service = RunningTLS(self.certs) if secure else RunningService()
                with self.subTest(secure=secure, concurrency=concurrency), service:
                    configure(service)
                    name = f'authenticated-{"https" if secure else "http"}-c{concurrency}'
                    result = self.execute(name, service.url+'/ok?delay_ms=15', private, requests=40, concurrency=concurrency)
                    state = service.idle()
                    checked = audit(service)
                    self.assertEqual(result['counts']['successes'], 40)
                    self.assertEqual(checked, dict(requests=40, matched=40, all_single_fields=True, methods=['GET']))
                    self.assertEqual(state['connections'], concurrency)
                    self.assertLessEqual(state['peak'], concurrency)
                    if secure:
                        self.assertEqual(len(service.observed()['tls_handshakes']), concurrency)
                    self.observations[-1]['server_observed'] = dict(http=state, headers=checked)

    def test_fixed_rate_authenticated_https_reuses_connection(self):
        with RunningTLS(self.certs) as service:
            configure(service)
            result = self.execute('paced-https', service.url+'/ok', self.private(),
                                  fixed_rate=(5, 850), concurrency=1)
            self.assertEqual(result['counts']['successes'], 5)
            self.assertEqual(result['scheduling']['offered'], 5)
            self.assertEqual(audit(service), dict(requests=5, matched=5, all_single_fields=True, methods=['GET']))
            self.assertEqual(len(service.observed()['tls_handshakes']), 1)

    def test_weighted_paths_share_tls_connection_and_private_headers(self):
        definition = self.artifacts/'private-workload-name.json'
        definition.write_text(json.dumps(dict(endpoints=[dict(path='/one', weight=4), dict(path='/two', weight=1)])))
        with RunningTLS(self.certs) as svc:
            configure(svc)
            result = self.execute('weighted-https', svc.url, self.private(), fixed_rate=(8, 1250),
                                  concurrency=1, workload_file=definition)
            self.assertEqual([e['counts']['successes'] for e in result['endpoint_results']], [8, 2])
            self.assertEqual([x['path'] for x in svc.server.header_checks],
                             ['/one', '/one', '/two', '/one', '/one'] * 2)
            self.assertEqual(audit(svc)['matched'], 10)
            self.assertEqual(len(svc.observed()['tls_handshakes']), 1)
            self.assertNotIn(str(definition), json.dumps(result))

    def test_invalid_credentials_are_http_failures_and_exports_are_private(self):
        changed = dict(EXPECTED, authorization='Bearer spe-dummy-invalid-credential')
        for secure in (False, True):
            service = RunningTLS(self.certs) if secure else RunningService()
            with self.subTest(secure=secure), service:
                configure(service)
                name = 'invalid-'+('https' if secure else 'http')
                result = self.execute(name, service.url+'/ok', self.private(changed), expected=3, requests=8)
                self.assertEqual(result['counts']['http_errors'], 8)
                self.assertEqual(result['http_status_counts'], {'401': 8})
                self.assertEqual(result['counts']['successes'], 0)
                self.assertEqual(audit(service)['matched'], 0)
                self.observations[-1]['server_observed'] = audit(service)
        with RunningTLS(self.certs) as service:
            configure(service)
            path = self.artifacts/'saved-authenticated.json'
            result = self.execute('saved-result', service.url+'/ok', self.private(), requests=8, output=path)
            self.assertEqual(result['counts']['successes'], 8)
            self.assertNotIn(AUTH, path.read_text())
            self.assertNotIn(API_KEY, path.read_text())
            echoed = self.execute('discarded-private-response', service.url+'/echo-private', self.private(), requests=4)
            self.assertEqual(echoed['counts']['successes'], 4)

    def test_header_file_is_snapshotted_before_dispatch(self):
        private = self.private()
        with RunningTLS(self.certs) as service:
            configure(service)
            command = self.command(service.url+'/ok?delay_ms=15&close=1', private, requests=20, concurrency=1)
            process = subprocess.Popen(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                deadline = time.monotonic()+5
                while not audit(service)['requests'] and process.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertGreater(audit(service)['requests'], 0)
                private.write_text('[{"name":"Authorization","value":"changed-after-dispatch"}]')
                stdout, stderr = process.communicate(timeout=10)
                result = self.retain('immutable-snapshot', command, process, stdout, stderr)
                self.assertEqual(result['counts']['successes'], 20)
                self.assertEqual(audit(service)['matched'], 20)
                self.assertEqual(service.idle()['connections'], 20)
            finally:
                if process.poll() is None:
                    process.kill(); process.communicate()

    def test_tls_rejection_and_redirect_do_not_send_headers_elsewhere(self):
        with RunningTLS(self.certs, 'expired') as service:
            configure(service)
            result = self.execute('expired-before-auth', service.url+'/ok', self.private(), expected=3, requests=4)
            self.assertEqual(result['counts']['tls_errors'], 4)
            self.assertEqual(audit(service)['requests'], 0)
        with RunningTLS(self.certs) as source, RunningService() as destination:
            configure(source); configure(destination)
            source.server.redirect_to = destination.url+'/ok'
            result = self.execute('redirect-confined', source.url+'/ok?status=302', self.private(), expected=3, requests=4)
            self.assertEqual(result['counts']['http_errors'], 4)
            self.assertEqual(audit(source)['matched'], 4)
            self.assertEqual(audit(destination)['requests'], 0)

    def test_secure_timeouts_and_cancellation_keep_accounting(self):
        with RunningTLS(self.certs) as service:
            configure(service)
            result = self.execute('authenticated-timeout', service.url+'/slow-body?delay_ms=600', self.private(),
                                  expected=3, requests=4, concurrency=2, timeout=250)
            self.assertEqual(result['counts']['timeouts'], 4)
            self.assertEqual(audit(service)['matched'], 4)
        for interrupt in (signal.SIGINT, signal.SIGTERM):
            with self.subTest(signal=interrupt), RunningTLS(self.certs) as service:
                configure(service)
                command = self.command(service.url+'/slow-body?delay_ms=2000', self.private(), requests=100, concurrency=2, timeout=5000)
                process = subprocess.Popen(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                try:
                    deadline = time.monotonic()+5
                    while audit(service)['requests'] < 2 and process.poll() is None and time.monotonic() < deadline:
                        time.sleep(0.01)
                    self.assertEqual(audit(service)['matched'], 2)
                    process.send_signal(interrupt)
                    stdout, stderr = process.communicate(timeout=5)
                    result = self.retain('authenticated-cancel-'+interrupt.name, command, process, stdout, stderr, 128+interrupt)
                    self.assertEqual(result['counts']['cancelled'], 2)
                    self.assertEqual(result['counts']['not_started'], 98)
                finally:
                    if process.poll() is None:
                        process.kill(); process.communicate()

    def test_private_comparisons_require_acknowledgement(self):
        with RunningTLS(self.certs) as service:
            configure(service)
            private = self.private()
            for name in ('comparison-a', 'comparison-b'):
                self.execute(name, service.url+'/ok', private, requests=8)
            for flag, expected in (([], 2), (['--allow-redacted-headers'], 0)):
                command = [str(self.engine), 'compare', str(self.artifacts/'comparison-a.json'),
                           str(self.artifacts/'comparison-b.json'), *flag, '--json', '-']
                result = subprocess.run(command, text=True, capture_output=True, timeout=10)
                self.assertEqual(result.returncode, expected)
                self.assertNotIn(AUTH, result.stdout+result.stderr)
                self.assertNotIn(API_KEY, result.stdout+result.stderr)
                name = 'acknowledged' if flag else 'rejected'
                (self.artifacts/(name+'.comparison.stdout')).write_text(result.stdout)
                (self.artifacts/(name+'.comparison.stderr')).write_text(result.stderr)
                report = json.loads(result.stdout)
                if flag:
                    self.assertTrue(report['redacted_headers_acknowledged'])
                    self.assertEqual(report['statistical_verdict'], 'not_assessed')

    def test_unsafe_and_malformed_files_fail_before_traffic_without_echo(self):
        cases = [b'', b'{'+AUTH.encode(), b'null', b'{}', b'[1]', b'[[[]]]',
                 b'[{"name":"authorization","name":"x-other","value":"'+AUTH.encode()+b'"}]',
                 b'[{"name":"authorization","value":"\\ud800"}]', b'['*10+b'0'+b']'*10,
                 json.dumps([dict(name='Authorization', value=AUTH), dict(name='AUTHORIZATION', value=AUTH)]).encode(),
                 b'[]'+b' '*(64*1024),
                 json.dumps([dict(name=f'x-{i}', value='v') for i in range(33)]).encode(),
                 json.dumps([dict(name=f'x-{i}', value='v'*4096) for i in range(5)]).encode()]
        for name in ('x bad', 'x:bad', 'x\r\nbad', '', 'x'*129, 'Host', 'Connection', 'Proxy-Connection',
                     'Content-Length', 'Transfer-Encoding', 'Accept-Encoding', 'User-Agent', 'TE', 'Trailer',
                     'Upgrade', 'Expect', 'Keep-Alive', 'Proxy-Authorization', 'Proxy-Authenticate'):
            cases.append(json.dumps([dict(name=name, value=AUTH)]).encode())
        for value in (AUTH+'\r\nx-injected: true', AUTH+'\n', AUTH+'\0', AUTH+'\t', ' '+AUTH,
                      AUTH+' ', 'v'*4097, 'caf\u00e9', 123, None):
            cases.append(json.dumps([dict(name='authorization', value=value)]).encode())
        with RunningService() as service:
            configure(service)
            for number, raw in enumerate(cases):
                with self.subTest(case=number):
                    command = self.command(service.url+'/ok', self.private(raw=raw), requests=1)
                    result = subprocess.run(command, text=True, capture_output=True, timeout=5)
                    self.assertEqual(result.returncode, 2, (number, result.stdout, result.stderr))
                    self.assertEqual(result.stdout, '')
                    self.assertNotIn(AUTH, result.stderr)
                    self.assertNotIn(API_KEY, result.stderr)
                    self.assertNotIn(command[command.index('--headers-file')+1], result.stderr)
                    (self.artifacts/f'rejected-config-{number}.stderr').write_text(result.stderr)
            public = self.private(); public.chmod(0o644)
            fifo = self.artifacts/'header-pipe'; os.mkfifo(fifo)
            for path, code in ((public, 2), (fifo, 2), (self.artifacts, 2), (self.artifacts/'missing', 1)):
                result = subprocess.run(self.command(service.url+'/ok', path, requests=1), text=True, capture_output=True, timeout=5)
                self.assertEqual(result.returncode, code)
                self.assertEqual(result.stdout, '')
                self.assertNotIn(str(path), result.stderr)
            self.assertEqual(audit(service)['requests'], 0)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--engine', type=Path, required=True)
    parser.add_argument('--openssl', type=Path, required=True)
    parser.add_argument('--artifacts', type=Path, required=True)
    args = parser.parse_args()
    args.artifacts.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix='headers-', dir=args.artifacts)).resolve()
    started = datetime.datetime.now(datetime.timezone.utc).isoformat()
    try:
        certificates(directory/'certificates', args.openssl)
        HeadersIntegration.engine = args.engine.resolve()
        HeadersIntegration.artifacts = directory
        HeadersIntegration.certs = directory/'certificates'
        suite = unittest.defaultTestLoader.loadTestsFromTestCase(HeadersIntegration)
        result = unittest.TextTestRunner(verbosity=2).run(suite)
    finally:
        for key in (directory/'certificates').glob('*.key'):
            key.unlink()
    (directory/'observations.json').write_text(json.dumps(dict(
        started_at_utc=started, finished_at_utc=datetime.datetime.now(datetime.timezone.utc).isoformat(),
        successful=result.wasSuccessful(), tests_run=result.testsRun,
        engine_sha256=hashlib.sha256(args.engine.read_bytes()).hexdigest(),
        scope='Executed local HTTP/HTTPS correctness checks using dummy credentials only; not performance evidence.',
        observations=HeadersIntegration.observations), indent=2)+'\n')
    print('Header test artifacts:', directory, flush=True)
    return 0 if result.wasSuccessful() else 1


if __name__ == '__main__':
    raise SystemExit(main())
