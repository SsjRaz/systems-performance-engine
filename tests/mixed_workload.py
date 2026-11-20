"""Exercise the weighted GET workload against a live HTTP/1.1 server."""
import argparse
import copy
import json
import signal
import subprocess
import threading
import time
import unittest
from collections import Counter
from pathlib import Path

from http_server import Handler
from integration import RunningService

PATHS = ('/items', '/vets')


class MixHandler(Handler):
    def do_GET(self):
        server = self.server
        status = server.statuses.get(self.path, 404)
        with server.condition:
            server.receipts.append((self.path, status, time.monotonic_ns(), self.client_address[1]))
            server.active += 1
            server.peak = max(server.peak, server.active)
            server.condition.notify_all()
        try:
            time.sleep(server.delays.get(self.path, 0))
            self.respond(status, b'ok')
        finally:
            with server.condition:
                server.active -= 1
                server.condition.notify_all()


class MixService(RunningService):
    def __enter__(self):
        super().__enter__()
        self.server.condition = threading.Condition()
        self.server.active = self.server.peak = 0
        self.server.delays = {}
        self.server.statuses = dict.fromkeys(PATHS, 200)
        self.server.receipts = []
        self.server.RequestHandlerClass = MixHandler
        return self

    def wait_for_path(self, path):
        with self.server.condition:
            return self.server.condition.wait_for(lambda: any(r[0] == path for r in self.server.receipts), timeout=5)


class MixTests(unittest.TestCase):
    def definition(self, name, weights=(4, 1), paths=PATHS):
        path = self.artifacts/(name + '-workload.json')
        path.write_text(json.dumps(dict(endpoints=[dict(path=p, weight=w) for p, w in zip(paths, weights)])))
        return path

    def command(self, svc, definition, rate, duration, concurrency, timeout=1000):
        return [str(self.engine), svc.url, '--workload-file', str(definition), '--rate', str(rate),
                '--duration-ms', str(duration), '--concurrency', str(concurrency), '--timeout-ms', str(timeout), '--json', '-']

    def check_result(self, svc, name, result, receipts, *, check_status=True):
        e = result['endpoint_results']
        self.assertEqual(result['schema_version'], 5)
        self.assertEqual([x['path'] for x in e], [x['path'] for x in result['workload']['endpoint_mix']['endpoints']])
        counts, scheduling = result['counts'], result['scheduling']
        for k in counts:
            self.assertEqual(sum(x['counts'][k] for x in e), counts[k], k)
        for k in ('missed_capacity', 'missed_late', 'not_evaluated'):
            self.assertEqual(sum(x[k] for x in e), scheduling[k], k)
        for x in e:
            c = x['counts']
            observed = Counter(r[1] for r in receipts if r[0] == x['path'])
            self.assertEqual(c['started'], sum(observed.values()))
            self.assertEqual(c['requested'], c['started'] + x['missed_capacity'] + x['missed_late'] + x['not_evaluated'])
            self.assertEqual(c['started'], c['completed'] + c['cancelled'])
            self.assertEqual(c['completed'], c['successes'] + x['failed'])
            self.assertEqual(x['success_latency_ms']['samples'], c['successes'])
            self.assertEqual(x['completed_latency_ms']['samples'], c['completed'])
            for label, n in [('completed', c['completed']), ('successful', c['successes'])]:
                self.assertAlmostEqual(x[label+'_throughput_rps'], n / result['elapsed_seconds'])
            if check_status:
                self.assertEqual(c['successes'], observed[200])
                self.assertEqual(c['http_errors'], observed[503])
        self.assertLessEqual(result['peak_in_flight'], result['workload']['concurrency'])
        self.assertIsNone(result['engine_error'])
        (self.artifacts/(name+'.json')).write_text(json.dumps(result, indent=2)+'\n')
        # Relative handler-entry timestamps, path, selected status and connection identifier.
        audit = dict(server_peak=svc.server.peak,
                     receipts=[[p, status, (ns-receipts[0][2])//1000, peer] for p, status, ns, peer in receipts])
        (self.artifacts/(name+'-receipts.json')).write_text(json.dumps(audit)+'\n')

    def execute(self, svc, name, definition, rate, duration, concurrency, timeout=1000, check_status=True):
        start = len(svc.server.receipts)
        process = subprocess.run(self.command(svc, definition, rate, duration, concurrency, timeout),
                                 capture_output=True, text=True, timeout=20)
        self.assertIn(process.returncode, (0, 3), process.stderr)
        self.assertNotIn(str(definition), process.stdout + process.stderr)
        result = json.loads(process.stdout)
        counts = result['counts']
        self.assertEqual(process.returncode, 0 if counts['successes'] == counts['requested'] else 3)
        self.check_result(svc, name, result, svc.server.receipts[start:], check_status=check_status)
        return result

    def compare_files(self, args, code):
        p = subprocess.run([self.engine, 'compare', *args, '--json', '-'], cwd=self.artifacts,
                           capture_output=True, text=True, timeout=10)
        self.assertEqual(p.returncode, code, p.stderr)
        return json.loads(p.stdout)

    def compare(self, left, right, code, extra=()):
        return self.compare_files([left + '.json', right + '.json', *extra], code)

    def test_thousand_arrivals_fast_then_slow(self):
        definition = self.definition('reference', (80, 20))
        with MixService() as svc:
            fast = self.execute(svc, 'fast', definition, 100, 10000, 2)
            svc.server.delays['/vets'] = 0.180
            slow = self.execute(svc, 'slow', definition, 100, 10000, 2)
            for r in (fast, slow):
                self.assertEqual([x['counts']['requested'] for x in r['endpoint_results']], [800, 200])
                self.assertEqual(r['scheduling']['offered'], 1000)
                self.assertEqual(r['scheduling']['not_evaluated'], 0)
            self.assertGreater(fast['counts']['successes'], slow['counts']['successes'])
            self.assertEqual(slow['peak_in_flight'], 2)
            self.assertLessEqual(svc.server.peak, 2)
            for e in slow['endpoint_results']:
                self.assertGreater(e['missed_capacity'], 0)
                self.assertGreater(e['counts']['successes'], 0)
            self.assertGreaterEqual(slow['endpoint_results'][1]['success_latency_ms']['min'], 170)
            report = self.compare('fast', 'slow', 3)
            self.assertEqual(report['candidate']['inputs'][0]['endpoint_results'], slow['endpoint_results'])
            self.assertTrue(all(v['change_percent'] is None for v in report['metrics'].values()))

    def test_order_reuse_failures_and_comparison(self):
        with MixService() as svc:
            expected = ['/items', '/items', '/vets', '/items', '/items'] * 2
            for name, weights in [('repeat-1', (4, 1)), ('repeat-2', (80, 20))]:
                start = len(svc.server.receipts)
                self.execute(svc, name, self.definition(name, weights), 10, 1000, 1)
                receipts = svc.server.receipts[start:]
                self.assertEqual([r[0] for r in receipts], expected)
                self.assertEqual(len({r[3] for r in receipts}), 1)
            report = self.compare('repeat-1', 'repeat-2', 0)
            self.assertEqual(report['status'], 'descriptive_comparison')
            for group in ('baseline', 'candidate'):
                for totals, run in zip(report[group]['endpoints'], report[group]['inputs'][0]['endpoint_results']):
                    self.assertEqual(totals['successes'], run['counts']['successes'])
                    self.assertAlmostEqual(totals['successful_throughput_rps'], run['successful_throughput_rps'])
                    self.assertEqual(totals['median_run_p95_ms'], run['success_latency_ms']['p95'])
            svc.server.statuses['/vets'] = 503
            r = self.execute(svc, 'errors', self.definition('errors'), 10, 1000, 1)
            self.assertEqual(r['http_status_counts'], {'200': 8, '503': 2})
            self.assertEqual(r['endpoint_results'][1]['failed'], 2)
            self.assertIsNone(r['endpoint_results'][1]['success_latency_ms']['p95'])
            self.compare('repeat-1', 'errors', 3)
            self.execute(svc, 'errors-2', self.definition('errors-2'), 10, 1000, 1)
            grouped = self.compare_files(['--baseline', 'repeat-1.json', '--baseline', 'repeat-2.json',
                                          '--candidate', 'errors.json', '--candidate', 'errors-2.json'], 3)
            for group in ('baseline', 'candidate'):
                g = grouped[group]
                self.assertEqual(g['runs'], 2)
                for i, totals in enumerate(g['endpoints']):
                    stats = [run['endpoint_results'][i]['success_latency_ms'] for run in g['inputs']]
                    samples = sum(s['samples'] for s in stats)
                    self.assertEqual(totals['successes'], samples)
                    self.assertAlmostEqual(totals['successful_throughput_rps'], samples / g['elapsed_seconds'])
                    if samples:
                        self.assertAlmostEqual(totals['mean_success_latency_ms'],
                                               sum(s['mean'] * s['samples'] for s in stats) / samples)
                        self.assertAlmostEqual(totals['median_run_p95_ms'], (stats[0]['p95'] + stats[1]['p95']) / 2)
                    else:
                        self.assertEqual(totals['failed'], 4)
                        self.assertIsNone(totals['mean_success_latency_ms'])
                        self.assertIsNone(totals['median_run_p95_ms'])
            self.assertTrue(all(m['change_percent'] is None for m in grouped['metrics'].values()))

            for name, weights, paths in [('weights', (1, 1), PATHS), ('order', (4, 1), PATHS[::-1]),
                                         ('path', (4, 1), ('/items', '/other'))]:
                svc.server.statuses['/other'] = 200
                self.execute(svc, name, self.definition(name, weights, paths), 10, 1000, 1)
                self.compare('repeat-1', name, 2)

            # Valid syntax with inconsistent counters, latency or identity must still be rejected.
            good = json.loads((self.artifacts/'repeat-1.json').read_text())
            for field, val in [('failed', 1), ('missed_late', 1), ('not_evaluated', 1), ('path', '/vets'),
                               ('successful_throughput_rps', 0)]:
                bad = copy.deepcopy(good)
                bad['endpoint_results'][0][field] = val
                (self.artifacts/'bad.json').write_text(json.dumps(bad))
                self.compare('repeat-1', 'bad', 2)
            for field, val in [('assignment', 'random'), ('endpoints', [{'path': '/items', 'weight': 80}, {'path': '/vets', 'weight': 20}])]:
                bad = copy.deepcopy(good)
                bad['workload']['endpoint_mix'][field] = val
                (self.artifacts/'bad.json').write_text(json.dumps(bad))
                self.compare('repeat-1', 'bad', 2)
            for key in ('completed_latency_ms', 'success_latency_ms'):
                bad = copy.deepcopy(good)
                bad['endpoint_results'][0][key]['samples'] += 1
                (self.artifacts/'bad.json').write_text(json.dumps(bad))
                self.compare('repeat-1', 'bad', 2)

            # For this numerical check each endpoint has one 3 ms sample and the rest are 1 ms.
            example = copy.deepcopy(good)
            distributions = ('success_latency_ms', 'completed_latency_ms')
            sections = [(example, 2), (example['endpoint_results'][0], 1), (example['endpoint_results'][1], 1)]
            for section, slow_samples in sections:
                for key in distributions:
                    stats = section[key]
                    stats.update(min=1, mean=1 + 2 * slow_samples / stats['samples'], p50=1, p95=3, p99=3, max=3)
            (self.artifacts/'quantiles.json').write_text(json.dumps(example))
            self.compare('repeat-1', 'quantiles', 0)
            for percentile in ('p50', 'p95'):
                bad = copy.deepcopy(example)
                for key in distributions:
                    bad[key][percentile] = 2  # Above both endpoint medians or below both endpoint p95s.
                (self.artifacts/'bad.json').write_text(json.dumps(bad))
                report = self.compare('quantiles', 'bad', 2)
                self.assertEqual(report['metrics'], {})

    def test_window_drain_and_timeout(self):
        definition = self.definition('drain', (1, 3))
        with MixService() as svc:
            svc.server.delays['/vets'] = 0.18
            r = self.execute(svc, 'drain', definition, 1, 25, 1)
            self.assertEqual([x['counts']['requested'] for x in r['endpoint_results']], [0, 1])
            self.assertGreater(r['elapsed_seconds'], 0.16)
            self.assertIsNone(r['endpoint_results'][0]['completed_latency_ms']['p95'])
            r = self.execute(svc, 'timeout', definition, 1, 25, 1, timeout=60, check_status=False)
            self.assertEqual(r['endpoint_results'][1]['counts']['timeouts'], 1)

    def test_process_pause_and_interruption(self):
        for pause in (False, True):
            name = 'pause' if pause else 'interrupt'
            with self.subTest(name=name), MixService() as svc:
                if not pause:
                    svc.server.delays['/vets'] = 0.6
                args = self.command(svc, self.definition(name), 10, 1500, 1)
                with subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as child:
                    try:
                        self.assertTrue(svc.wait_for_path('/items' if pause else '/vets'))
                        if pause:
                            child.send_signal(signal.SIGSTOP)
                            time.sleep(0.55)
                            resumed = time.monotonic_ns()
                            child.send_signal(signal.SIGCONT)
                        else:
                            child.send_signal(signal.SIGTERM)
                        stdout, stderr = child.communicate(timeout=5)
                    finally:
                        if child.poll() is None:
                            child.kill()
                            child.communicate()
                self.assertEqual(child.returncode, 3 if pause else 128+signal.SIGTERM, stderr)
                r = json.loads(stdout)
                self.check_result(svc, name, r, svc.server.receipts, check_status=pause)
                if pause:
                    self.assertFalse(r['interrupted'])
                    self.assertTrue(all(e['missed_late'] > 0 for e in r['endpoint_results']))
                    self.assertLessEqual(sum(resumed <= ts < resumed+90_000_000 for _, _, ts, _ in svc.server.receipts), 2)
                else:
                    self.assertTrue(r['interrupted'])
                    self.assertEqual(r['endpoint_results'][1]['counts']['cancelled'], 1)
                    self.assertTrue(all(e['not_evaluated'] > 0 for e in r['endpoint_results']))

        report = self.compare('pause', 'interrupt', 3, ['--allow-origin-change'])
        self.assertEqual(report['candidate']['inputs'][0]['state'], 'incomplete_or_interrupted')

    def test_configuration_rejected_before_traffic(self):
        path = self.artifacts/'private-input-filename.json'
        marker = 'private-test-marker'
        valid = [{'path': '/a', 'weight': 4}, {'path': '/b', 'weight': 1}]
        with MixService() as svc:
            examples = [dict(endpoints=valid + valid), dict(endpoints=valid[:1]), dict(endpoints=[]),
                        dict(endpoints=valid, headers=marker), {'endpoints': [valid[0], valid[0]]}]
            for invalid in (0, -1, 2.3, True, '1', 1000001):
                examples.append(dict(endpoints=[dict(path='/a', weight=invalid), valid[1]]))
            for bad in ('', 'relative', '//other', 'http://other/a', '/x?'+marker, '/x#fragment',
                        '/a/../b', '/%2fhost', '/x\\path', '/a\r\nb', '/a//b'):
                examples.append(dict(endpoints=[dict(path=bad, weight=1), valid[1]]))
            inputs = [json.dumps(x) for x in examples] + [
                '{"endpoints":[],"endpoints":[]}', '['*12+marker, marker*6000]
            inputs.append(json.dumps(dict(endpoints=valid)) + '\x00' + marker)
            for data in inputs:
                path.write_text(data)
                p = subprocess.run(self.command(svc, path, 1, 10, 1), capture_output=True, text=True, timeout=4)
                self.assertEqual(p.returncode, 2)
                self.assertNotIn(marker, p.stdout+p.stderr)
                self.assertNotIn(str(path), p.stdout+p.stderr)
            path.write_text(json.dumps(dict(endpoints=valid)))
            for url, flags in [(svc.url, []), (svc.url+'/base', ['--rate','1','--duration-ms','1']),
                               (svc.url+'?'+marker, ['--rate','1','--duration-ms','1'])]:
                p = subprocess.run([self.engine,url,'--workload-file',path,*flags], capture_output=True,text=True,timeout=4)
                self.assertEqual(p.returncode, 2)
                self.assertNotIn(marker, p.stdout+p.stderr)
            self.assertEqual(svc.server.receipts, [])


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--engine', type=Path, required=True)
    parser.add_argument('--artifacts', type=Path, required=True)
    args = parser.parse_args()
    MixTests.engine = args.engine.resolve()
    MixTests.artifacts = args.artifacts.resolve()
    MixTests.artifacts.mkdir(parents=True, exist_ok=True)
    unittest.main(argv=[__file__], verbosity=2)
