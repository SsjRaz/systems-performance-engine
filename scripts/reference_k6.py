"""k6 adapter. Normalized latency fields explicitly retain k6's distinct definition."""
import datetime
import json
import subprocess
import time
from pathlib import Path


def normalize(raw, url, requests, concurrency, timeout_ms, version):
    metrics = raw['metrics']
    def count(name):
        return int(metrics.get(name, {}).get('values', {}).get('count', 0))
    outcomes = {name: count('outcome_' + name) for name in
                ('successes', 'http_errors', 'timeouts', 'connection_errors', 'network_errors')}
    done = sum(outcomes.values())
    start = metrics['window_start_ms']['values']['min']
    end = metrics['window_end_ms']['values']['max']
    elapsed = (end - start) / 1000
    if elapsed <= 0:
        raise RuntimeError('Reference active window is below its millisecond clock resolution')
    def latency(name, samples):
        values = metrics.get(name, {}).get('values', {})
        return dict(samples=samples, **{key: values.get(field) if samples else None
            for key, field in [('min','min'), ('mean','avg'), ('p50','med'), ('p95','p(95)'), ('p99','p(99)'), ('max','max')]})
    started = count('attempts')
    return dict(schema_version=1, engine=dict(tool='k6', version=version,
        latency_definition='http_req_duration: sending+waiting+receiving; excludes initial connection/DNS',
        elapsed_definition='first VU start to last request end; Date.now millisecond resolution',
        percentile_method='k6 trend interpolation; not SPE nearest-rank'),
        workload=dict(url=url, requests=requests, concurrency=concurrency, timeout_ms=timeout_ms,
            method='GET', http_version='1.1', model='closed_loop', redirects=False, proxy=False,
            connection_reuse=True, success_policy='complete_2xx', body_retention='discard',
            body_size_limit='not implemented by reference adapter'),
        counts=dict(requested=requests, started=started, completed=done, **outcomes,
            response_limit_errors=0, cancelled=started-done, not_started=requests-started),
        elapsed_seconds=elapsed, successful_throughput_rps=outcomes['successes']/elapsed,
        completed_throughput_rps=done/elapsed, peak_in_flight=min(requests,concurrency),
        peak_in_flight_definition='configured VUs upper bound; not independently observed',
        interrupted=done != requests, engine_error=None,
        http_status_counts={str(s):count('status_'+str(s)) for s in (200,302,403,404,503) if count('status_'+str(s))},
        success_latency_ms=latency('successful_http_ms',outcomes['successes']),
        completed_latency_ms=latency('completed_http_ms',done), wrong_protocol=count('wrong_protocol'))


def run(binary, url, requests, concurrency, timeout_ms, prefix, environment):
    script = Path(__file__).with_name('reference_k6.js').resolve()
    raw_path = prefix.with_suffix('.k6-summary.json')
    for suffix in ('.k6-summary.json', '.json', '.stdout', '.stderr', '.command.json', '.process.json'):
        path = prefix.with_suffix(suffix)
        if path.exists(): raise RuntimeError(f'Refusing to overwrite {path}')
    env = dict(environment, TARGET=url, REQUESTS=str(requests), CONCURRENCY=str(concurrency),
               TIMEOUT_MS=str(timeout_ms), SUMMARY_PATH=str(raw_path.resolve()))
    command = [str(binary), 'run', '--quiet', '--no-usage-report', '--address=127.0.0.1:0', str(script)]
    prefix.with_suffix('.command.json').write_text(json.dumps(command,indent=2)+'\n')
    with prefix.with_suffix('.stdout').open('x') as out, prefix.with_suffix('.stderr').open('x') as err:
        started_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
        started_clock = time.monotonic()
        process = subprocess.Popen(command,env=env,stdout=out,stderr=err,start_new_session=True)
        try:
            code = process.wait(timeout=195)
        finally:
            if process.poll() is None:
                process.terminate()
                try: process.wait(timeout=5)
                except subprocess.TimeoutExpired: process.kill(); process.wait()
            prefix.with_suffix('.process.json').write_text(json.dumps(dict(started_at_utc=started_at,
                finished_at_utc=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                process_wall_seconds=time.monotonic()-started_clock, returncode=process.returncode,
                note='Outer process interval includes runtime startup/summary; not the active request window.'),indent=2)+'\n')
    if code: raise RuntimeError(f'k6 exited with {code}; inspect {prefix}.stderr')
    version = subprocess.check_output([str(binary),'version'],text=True).strip()
    result = normalize(json.loads(raw_path.read_text()),url,requests,concurrency,timeout_ms,version)
    if result['wrong_protocol']: raise RuntimeError('Reference negotiated unexpected HTTP protocol')
    prefix.with_suffix('.json').write_text(json.dumps(result,indent=2,allow_nan=False)+'\n')
    return result
