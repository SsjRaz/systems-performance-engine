// Matched closed-loop reference. Latency is k6 HTTP duration, not SPE wall latency.
import http from 'k6/http';
import { Counter, Trend } from 'k6/metrics';

const names = ['successes', 'http_errors', 'timeouts', 'connection_errors', 'network_errors'];
const outcomes = Object.fromEntries(names.map(n => [n, new Counter(`outcome_${n}`)]));
const statusCodes = [200, 302, 403, 404, 503];
const statuses = Object.fromEntries(statusCodes.map(n => [n, new Counter(`status_${n}`)]));
const attempts = new Counter('attempts');
const starts = new Trend('window_start_ms');
const ends = new Trend('window_end_ms');
const successful = new Trend('successful_http_ms', true);
const completed = new Trend('completed_http_ms', true);
const wrongProtocol = new Counter('wrong_protocol');

export const options = {
  scenarios: { workload: { executor: 'shared-iterations', vus: Number(__ENV.CONCURRENCY),
    iterations: Number(__ENV.REQUESTS), maxDuration: '180s', gracefulStop: '5s' } },
  discardResponseBodies: true, noConnectionReuse: false, noVUConnectionReuse: false,
  maxRedirects: 0, summaryTrendStats: ['min', 'avg', 'med', 'p(95)', 'p(99)', 'max'],
};
http.setResponseCallback(http.expectedStatuses({min: 200, max: 299}));

export default function () {
  if (__ITER === 0) {
    Object.values(outcomes).forEach(c => c.add(0));
    Object.values(statuses).forEach(c => c.add(0));
    wrongProtocol.add(0);
    starts.add(Date.now());
  }
  attempts.add(1);
  const r = http.get(__ENV.TARGET, {timeout: `${__ENV.TIMEOUT_MS}ms`, redirects: 0,
    responseType: 'none', headers: {'Accept-Encoding': 'identity', 'Accept': '*/*',
      'User-Agent': 'systems-performance-engine/0.1.0'}});
  ends.add(Date.now());
  const code = r.error_code || 0;
  const completeHttp = code === 0 || (code >= 1400 && code < 1600);
  let outcome;
  if (completeHttp && r.status >= 200 && r.status < 300) outcome = 'successes';
  else if (code === 1050 || code === 1211) outcome = 'timeouts';
  else if ((code >= 1100 && code < 1200) || [1210, 1212, 1213].includes(code)) outcome = 'connection_errors';
  else if (completeHttp && r.status >= 100) outcome = 'http_errors';
  else outcome = 'network_errors';
  outcomes[outcome].add(1);
  completed.add(r.timings.duration);
  if (outcome === 'successes') successful.add(r.timings.duration);
  if (completeHttp && statuses[r.status]) statuses[r.status].add(1);
  if (completeHttp && r.proto !== 'HTTP/1.1') wrongProtocol.add(1);
}

export function handleSummary(data) {
  return {[__ENV.SUMMARY_PATH]: JSON.stringify(data, null, 2)};
}
