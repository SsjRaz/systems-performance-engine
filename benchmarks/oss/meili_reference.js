// Independent HTTP timings and semantic checks for the 1,000-value federated search.
import http from 'k6/http';
import { Counter, Trend } from 'k6/metrics';

const payload = open(__ENV.BENCH_BODY);
const valid = new Counter('valid_searches');
const processing = new Trend('server_processing_ms', true);
const kinds = ['tea', 'coffee', 'pasta', 'granola', 'biscuits', 'jam', 'rice'];
export const options = {
  vus: 1, iterations: 24, noConnectionReuse: true, maxRedirects: 0,
  summaryTrendStats: ['avg','min','med','p(95)','p(99)','max'],
};

export default function () {
  const res = http.post('http://127.0.0.1:' + __ENV.BENCH_PORT + '/multi-search', payload,
    { headers: {'Content-Type':'application/json'}, timeout: '4s' });
  const doc = res.json();
  const seen = new Set();
  let ok = res.status === 200 && doc.estimatedTotalHits === 2000 && doc.hits.length === 20;
  for (const hit of doc.hits) {
    const n = Number(hit.articleNumber);
    const uid = hit._federation.indexUid;
    ok = ok && Number.isInteger(n) && n >= 1 && n <= 1000 && ['catalog_a','catalog_b'].includes(uid)
         && hit.title === kinds[n % 7] + ' package ' + n;
    seen.add(uid + '/' + n);
  }
  valid.add(ok && seen.size === 20 ? 1 : 0);
  processing.add(doc.processingTimeMs);
}

export function handleSummary(data) {
  const m = data.metrics;
  return { [__ENV.BENCH_SUMMARY]: JSON.stringify({
    requests: m.http_reqs.values.count, valid: m.valid_searches.values.count,
    http_duration_ms: m.http_req_duration.values, server_processing_ms: m.server_processing_ms.values,
    http_rps: m.http_reqs.values.rate,
  }) };
}
