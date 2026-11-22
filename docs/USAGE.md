# Build and CLI reference

Paired manifests and advisory regression analysis are documented in the [regression analysis guide](REGRESSION.md).

## Untimed response verification

Use `--verify-response policy.json` to send one preflight request per endpoint. The owner-only file declares a public `policy_id` and rules in workload order. Choose an ID safe to publish and change it when assertions change.

```json
{"policy_id":"catalog-v1","endpoints":[{"fields":{"/estimatedTotalHits":2000,"/hits/0/available":true}}]}
```

Each rule accepts 1–16 JSON Pointers to exact scalar values, or a single `sha256` containing the lowercase SHA-256 of the response bytes. JSON checks both type and value, including integer versus floating point. A GET mix needs one rule per endpoint. Probes reuse the configured request, immutable body, private headers, timeout and TLS trust. GET and HEAD permit probes. POST requires `--replay-safe-post`, even with one timed request. The acknowledgement permits one additional replay-safe POST. PUT, PATCH and DELETE reject probes.

The policy is limited to 64 KiB and eight levels. Capture stops at the smaller of the configured body limit and 256 KiB. Response JSON allows 32 levels and 16,384 parser events and rejects duplicate keys. Only the public policy ID, counts and bounded outcome are exported. A failed probe stops before timing and exits 3. Probes use separate connections and counters and may warm the application cache. Their success establishes correctness for the preflight responses only.

Unprobed runs retain schema 6 and print correctness as unchecked. Probed runs use schema 7 and record `verification.scope=untimed_preflight`. Native comparison continues to read earlier schemas and requires matching contracts and policy IDs. ID equality records the experimenter's assertion that the private rules matched.

Commands below run from the repository root. Platform coverage is listed in the [README](../README.md#platforms).

## Build and test

Requirements: a C++20 compiler, CMake 3.24+, a build tool such as Make, and libcurl 7.85+ development headers/library with asynchronous DNS support. HTTPS requires a TLS-enabled libcurl. CA-bundle SHA-256 uses CommonCrypto on macOS and OpenSSL Crypto 1.1.1+ development headers/library on Linux. Python 3.10+ and the OpenSSL command-line tool are test dependencies; neither is an engine runtime dependency. Ubuntu packages include `libcurl4-openssl-dev`, `libssl-dev`, and `openssl`. Functional builds have been exercised on macOS arm64 (Apple Clang 16 / libcurl 8.7.1) and Ubuntu 24.04 arm64 in Docker (GCC 13.3 / libcurl 8.5.0).

An optional local CMake installation uses ignored build output:

```sh
python3 -m venv build/tools
build/tools/bin/python -m pip install cmake==3.31.6
. build/tools/bin/activate
```

Build and run all tests (including actual loopback traffic):

```sh
cmake -S . -B build/release -DCMAKE_BUILD_TYPE=Release
cmake --build build/release -j 4
ctest --test-dir build/release --output-on-failure
```

The tests require permission to listen and connect on loopback. They exercise a controlled local service. CTest runs each networking suite in an isolated directory under `test-results/managed/`, retains a compact latest `summary.json`, removes successful scratch output, and keeps the two latest completed failures per suite. CTest displays failure output. CI uploads the environment record, configure/build/CTest logs, and compact suite summaries. Raw fixture inputs and full failure directories stay on the machine that ran the suite.

Set `SPE_TEST_KEEP_SUCCESSES=1` for a detailed successful run or adjust `SPE_TEST_KEEP_FAILURES` (default 2; both limits allow 0–20 runs per suite). These limits apply only to completed functional-test directories created by the wrapper, within each build directory. Pruning verifies a completion inventory; modified, annotated, unrecognized, historical, and running/interrupted directories are untouched. Same-suite executions serialize initialization, execution and pruning with a process lock. Interrupted or modified directories require manual review. Reuse one Release and one sanitizer build directory rather than creating a new checkout for every test run. CTest's own latest logs remain in `Testing/Temporary/`.

The underlying Python suites remain available for deliberate evidence capture with `--artifacts PATH`; direct invocation retains all output and does not apply automatic retention. Application benchmark measurements, provenance and methodology are never automatically pruned. JVM GC/JIT/JFR profiling stays opt-in via the existing `--diagnostics` experiment option. Bulky unique profiling data should be archived losslessly with checksums before removing originals; reproducible build caches and downloaded archives can be cleaned separately after review.

The [local profiling archive manifest and restoration instructions](../benchmarks/evidence/profiling-20261115/README.md) are tracked outside `build/`. Large recordings remain local under the ignored `local-evidence/` directory, which must be included separately in personal backups. `python3 scripts/restore_profiles.py --restore` restores their original analysis paths after build cleanup.

The [CI workflow](../.github/workflows/ci.yml) defines separate Release and ASan/UBSan jobs on `ubuntu-24.04`, using GCC 13 and distribution packages for CMake, Make, libcurl, OpenSSL, and Python. Action versions are pinned to commit SHAs; exact package/compiler versions and test output are retained for seven days. CI requires no Petclinic checkout or private application artifacts. Shared-runner timings serve functional assertions, not performance comparisons.

For AddressSanitizer and UndefinedBehaviorSanitizer checks with Clang/GCC:

```sh
cmake -S . -B build/sanitize -DCMAKE_BUILD_TYPE=Debug -DSPE_ENABLE_SANITIZERS=ON
cmake --build build/sanitize -j 4
ctest --test-dir build/sanitize --output-on-failure
```

The JSON parser used by comparison is nlohmann/json 3.12.0. Its MIT notice and pinned source hashes are included in the repository, so the build can use it offline. [Dependency provenance](../third_party/nlohmann/SOURCE.json)

## Run locally

Start the controlled HTTP/1.1 fixture in one terminal:

```sh
python3 tests/http_server.py --port 8080
```

Run the engine in another terminal from this folder:

```sh
build/release/spe 'http://127.0.0.1:8080/ok' --requests 100 --concurrency 4 --timeout-ms 1000 --json build/fast.json
build/release/spe 'http://127.0.0.1:8080/ok?delay_ms=60' --requests 100 --concurrency 4 --timeout-ms 1000 --json build/slow.json
```

Use `--json -` for JSON on stdout with the human summary on stderr, and `--help` for all options. Output directories must already exist. A file destination must be new: the CLI reserves it before sending traffic and refuses to overwrite an earlier measurement. A process failure can leave an empty or partial new file; exit status indicates whether the run completed successfully. Defaults are 1000 requests, concurrency 10, a 1000 ms total timeout, and a 16 MiB per-response body limit. Request count is capped at 1,000,000 and concurrency at 1024; these are resource limits, not demonstrated capacity claims.

The fixture also provides `?status=503`, `?close=1`, `/slow-body?delay_ms=250`, `/mixed`, `/disconnect`, `/truncated`, `/malformed`, `/chunked`, and `/stats`. It is a functional test service, not a calibrated workload target.

## HTTP methods and request bodies

`--method` accepts GET, HEAD, POST, PUT, PATCH and DELETE. GET is the default. GET and HEAD reuse connections and default to 1,000 requests. Each write method defaults to **one logical request**. To repeat a write, set `--requests` or choose a fixed-rate window. Weighted endpoint mixes accept GET only.

This example sends one JSON document to a local test API that accepts POST at `/items`.

```sh
inputs=$(mktemp -d build/inputs.XXXXXX)
(
  umask 077
  printf '%s' '{"name":"sample"}' > "$inputs/payload"
  printf '%s' '[{"name":"Content-Type","value":"application/json"}]' > "$inputs/headers"
)
build/release/spe http://127.0.0.1:8081/items --method POST \
  --body-file "$inputs/payload" --headers-file "$inputs/headers" --json -
```

POST, PUT and PATCH accept a regular, owner-only body file up to 65,536 bytes. The engine reads it once before starting the run and sends its exact bytes on each dispatch. An omitted or empty file sends zero bytes. The default content type is `application/octet-stream`. Use the headers file to select another type. SPE controls framing and calculates Content-Length. `--max-body-bytes` limits the **response**.

Each write opens a fresh HTTP/1.1 connection. This avoids libcurl's stale-connection replay and includes connection setup in the measured latency. Redirect following, application retries and automatic Expect negotiation are disabled. A server may already have applied a write when its response is lost, the client times out, or the run is cancelled. The result records the outcome seen by the client. Check server records before deciding whether to submit that write again.

Schema 6 records the method, connection reuse policy and `request_body_bytes`. The payload, its hash and the private file path are omitted. Comparing nonempty bodies requires matching lengths and `--allow-redacted-body` to acknowledge that contents cannot be verified. Custom header values have their own acknowledgement. To exercise all methods against a controlled local server, run `ctest --test-dir build/release -R '^methods$' --output-on-failure`.

## Fixed-rate scheduling

With the loopback fixture running, this workload schedules 1,000 GETs in ten seconds with at most four transfers in flight.

```sh
build/release/spe http://127.0.0.1:8080/ok --rate 100 --duration-ms 10000 --concurrency 4 --json -
```

Supply both options together. `--rate` is a whole number from 1 to 1,000,000 requests per second. The arrival window is 1 to 3,600,000 ms and the planned count is capped at 1,000,000. The default request-count mode uses `--requests` instead. Both modes reuse the same HTTP/HTTPS handles, timeout, response limits and header policy.

Arrival zero is due at the start. Arrival *i* is due at *i / rate* seconds from the same monotonic epoch, rounded up to the next nanosecond when necessary. Only deadlines strictly before window end belong to the workload, giving `ceil(rate × duration_ms / 1000)` planned arrivals. After observing completions, the event loop evaluates the most recent due arrival. Older unprocessed arrivals count as `missed_late`. The current one counts as `missed_capacity` if all slots are occupied. At window end every remaining deadline expires and active transfers drain under their original timeouts. A signal stops admission and cancels active requests.

| Result | Meaning |
| --- | --- |
| `counts.requested` | Planned arrivals for the full window. |
| `scheduling.offered` | Arrivals evaluated, equal to `counts.started + missed_capacity + missed_late`. Includes missed opportunities. |
| `scheduling.not_evaluated` | Remaining budget at interruption or engine failure. `requested = offered + not_evaluated`. |
| `counts.not_started` | Both kinds of miss plus arrivals never evaluated. `started = completed + cancelled` still holds. |
| `scheduling.offered_rps`, `dispatched_rps` | Evaluated and started counts divided by `window_seconds`, the smaller of elapsed time and configured duration. The immediate first arrival and a partial final period can affect short-window averages. |
| `successful_throughput_rps` | Complete 2xx responses divided by total elapsed seconds, including drain time. |
| `scheduling.dispatch_delay_ms` | Intended deadline to the timestamp immediately before registration with libcurl, for every started request including failures and cancellations. Preemption after admission can extend this delay beyond one period. Missed arrivals receive no latency sample. |

Slower responses occupy slots longer. Late wakes can also occur because of timer resolution, host scheduling or generator work. The miss counters describe the observed condition. Attributing its cause requires server-side observations or profiling. With no transfers in flight the loop sleeps until the next monotonic deadline, checking for cancellation at least every 50 ms subject to OS scheduling. With active transfers, libcurl polls sockets with whole-millisecond timeouts. Achievable pacing depends on the machine and workload. These measurements provide no coordinated-omission correction.

Missed arrivals produce exit code 3. `spe compare` requires matching models, target rates, windows, concurrency and overload policies. It displays arrival totals before request metrics and withholds performance deltas for missed work. Its JSON includes each input's scheduling summary.

## Two-endpoint workloads

Use an HTTP or HTTPS origin and a workload file with two distinct paths. This example assigns 800 of 1,000 arrivals to `/items` and 200 to `/vets`. It runs against the loopback fixture described above.

```sh
results=$(mktemp -d build/mix.XXXXXX)
cat > "$results/workload.json" <<'JSON'
{"endpoints":[{"path":"/items","weight":4},{"path":"/vets","weight":1}]}
JSON
build/release/spe http://127.0.0.1:8080 --workload-file "$results/workload.json" \
  --rate 100 --duration-ms 10000 --concurrency 2 --json "$results/run.json"
```

The file is a regular JSON file of at most 64 KiB. Weights are positive integers up to 1,000,000. SPE reduces them to their lowest ratio. Paths start with one slash and use ASCII letters, digits, `-`, `.`, `_`, `~` and `/`. Empty interior segments, dot segments, query strings, fragments and percent escapes are rejected. The origin can end in a single slash. Use shareable route paths because the results identify them literally. Supply credentials through the existing private headers file. Both paths use the same TLS and header configuration.

For each prefix of the schedule, SPE rounds the first path's proportional share to the nearest integer, with ties going to the first path. The second path gets the remaining arrivals. The 4:1 sequence starts `A A B A A` and repeats. Dropping an arrival preserves its assigned path. A stop leaves an unevaluated suffix in each path's budget.

Each entry in `endpoint_results` includes the requested (planned), started, completed and successful counts, error categories, cancellations, misses at capacity, expired arrivals and unevaluated arrivals. `failed` counts completed errors. Both throughput rates use the full run elapsed time, including drain. Successful and completed latencies have separate exact nearest-rank summaries. Cancelled and undispatched arrivals contribute to neither distribution. Under overload the received mix can differ substantially from the planned mix.

## HTTPS and trust

HTTPS targets use libcurl's TLS implementation with certificate-chain and hostname verification enabled, and TLS 1.2 as the minimum. Default roots come from the trust store configured for the installed libcurl/TLS backend. There is no insecure option, plaintext fallback, or redirect following.

An explicit PEM CA bundle supports private services and controlled tests:

```sh
build/release/spe 'https://localhost:8443/health' \
  --ca-file /path/to/roots.pem --requests 100 --concurrency 4 \
  --json build/secure.json
```

The service must already be running with a certificate valid for the requested hostname or IP. `--ca-file` is HTTPS-only. The selected file must be regular, nonempty, and at most 1 MiB. The engine retains an immutable in-memory snapshot for the run and supplies it through `CURLOPT_CAINFO_BLOB`, clearing the default CA file/directory for custom trust. Unsupported custom-CA backends fail before dispatch. Custom trust is exercised on macOS SecureTransport/libcurl 8.7.1 and Linux OpenSSL/libcurl 8.5.0; other backends may reject this option. The engine does not install roots in the operating system or download a CA bundle.

Result schemas 2 through 6 record the verification policy, minimum TLS version, and CA source. Custom bundles are identified by SHA-256; file paths and certificate contents are omitted. Default trust-store contents are not fingerprinted. A fingerprint identifies the selected bytes; it does not authenticate a saved result or establish environmental equivalence.

Recognized libcurl certificate, TLS handshake, and TLS configuration failures increment `tls_errors`; generic transport failures remain `network_errors`. A timeout during TLS negotiation or a secure response increments `timeouts`, and interrupted active requests are cancelled. TLS failures have no successful-latency samples. Original libcurl codes and generic descriptions are retained without exporting detailed certificate/path error buffers. Missing/unreadable or invalid-sized CA files fail before traffic; malformed certificate data is rejected by the TLS backend.

The `https` CTest suite generates a local PKI and executes actual secure connections, including invalid-certificate cases and an attempted redirect to plaintext. The suite deletes its signing keys during teardown. Detailed certificate and measurement outputs follow the test retention settings above. These checks exercise protocol behavior. Mutual TLS, certificate pinning, custom cipher policies, and HTTP/2 are outside the current interface.

## Configurable request headers

`--headers-file PATH` loads a JSON array once before dispatch. A file such as `secrets/request-headers.json` can contain:

```json
[
  {"name": "Authorization", "value": "Bearer LOCAL_TEST_TOKEN"},
  {"name": "X-API-Key", "value": "LOCAL_TEST_KEY"},
  {"name": "Accept", "value": "application/json"}
]
```

The values above are placeholders. The existing `/secrets/` exclusion keeps that directory out of Git. Files require owner-only permissions, for example `chmod 600 secrets/request-headers.json`.

```sh
build/release/spe 'https://localhost:8443/health' \
  --ca-file /path/to/roots.pem --headers-file secrets/request-headers.json \
  --requests 100 --concurrency 4 --json build/authenticated.json
```

Headers apply to every request, including reused connections. Names are case-insensitive and normalized to lowercase. Duplicate names are rejected. Empty values send an actual empty field; they do not suppress a libcurl default. Values must be ASCII without control characters (including CR, LF, NUL and tabs) or surrounding spaces. Names must use HTTP token characters. Limits are 32 fields, 128 bytes per name, 4096 bytes per value, 16 KiB total configured wire size, and a 64 KiB regular input file with at most eight JSON nesting levels. Duplicate JSON keys and unknown entry fields are rejected.

Routing, framing and connection headers are controlled by the engine: `Host`, `Connection`, `Proxy-Connection`, `Keep-Alive`, `Content-Length`, `Transfer-Encoding`, `TE`, `Trailer`, `Upgrade`, `Expect`, `Accept-Encoding`, `User-Agent`, `Proxy-Authorization`, and `Proxy-Authenticate` are rejected. Other application headers, including `Authorization`, `X-API-Key`, `Accept`, and `Cookie`, are supported. There is no challenge-response authentication, token refresh, or cookie jar.

All configured values are treated as private, including custom names that are not recognized as authentication headers. Results retain sorted names only; values, value hashes and the input path are omitted. Parser errors do not quote input. Values remain in the selected local file and in process memory during execution. HTTPS verification stays enabled; HTTP transports header values in cleartext. Redirects and proxies remain disabled.

Because values are redacted, `spe compare` rejects custom-header workloads unless `--allow-redacted-headers` explicitly acknowledges that value equality and authorization context cannot be checked. Matching names alone do not establish equivalent requests. The flag cannot override changed names, TLS settings, endpoints, or other workload mismatches. The Petclinic helper supports empty header configurations only.

## Compare saved results

Compare measurements recorded with the same SPE build and workload settings:

```sh
build/release/spe compare baseline.json candidate.json
build/release/spe compare baseline.json candidate.json --json comparison.json
```

The command reads local files; it does not start applications or generate traffic. It accepts SPE 0.1.0/schema 1 HTTP results, SPE 0.2.0/schema 2 HTTP/HTTPS results, SPE 0.3.0/schema 3 results with header metadata, SPE 0.4.0/schema 4 results with explicit scheduling metadata, SPE 0.5.0/schema 5 results with endpoint mix metadata, and SPE 0.6.0/schema 6 results with HTTP method and payload length. It validates request accounting, status maps, sample counts, numeric bounds, latency ordering, and consistency with the run window and throughput. Contracts cannot be mixed. Schema-1 inputs imply zero TLS errors and no TLS policy; schemas 1 and 2 imply no custom headers. Unsupported schemas, duplicate JSON keys, malformed inputs, and incompatible engine or workload metadata are rejected. Matching metadata cannot establish hardware equivalence, application identity, or measurement authenticity. k6's normalized results have different semantics and are rejected. Comparison reports use their own schema version 6 and `kind: spe_comparison`.

Request budgets, concurrency, timeout, body limit, execution policies, and all recorded engine build fields must match. Encoded endpoint paths and queries must match. Mixed runs require the same assignment rule, ordered paths and reduced weights. The report includes each path's delivery and miss counts, successful throughput, weighted mean latency and median of its per-run successful p95 values. Different hosts, ports, or IPv6 scopes require `--allow-origin-change`; this acknowledges the difference without verifying equivalent networks or environments. A changed path or query is still rejected.

HTTP and HTTPS results cannot be compared as equivalent workloads. HTTPS inputs require the same verification/minimum-version policy and CA-source settings; custom bundles must have matching fingerprints. `--allow-origin-change` does not override these checks. Certificates served by application versions may differ if both validate under the same configured trust policy.

For several runs per version, repeat the named options:

```sh
build/release/spe compare \
  --baseline baseline-1.json --baseline baseline-2.json \
  --candidate candidate-1.json --candidate candidate-2.json \
  --json comparison.json
```

| Reported value | Definition |
| --- | --- |
| Successful throughput | Total successful responses / total measured seconds across the group. |
| Mean request latency | Mean weighted by successful sample count. |
| Median run p50/p95/p99 | Median of that percentile across input runs; **not** a pooled request percentile. |
| Run variability | Minimum, maximum, median, and sample standard deviation of per-run metrics; deviation is `null` with fewer than two observations. |
| Change | Candidate minus baseline, divided by baseline for percentages; a zero denominator produces `null` / `n/a`. |
| Completed error rate | Completed failures / completed requests; cancellations and undispatched requests are reported separately. |

Higher successful throughput and lower successful-request latency are generally preferable under comparable conditions. Incomplete, interrupted, or failed runs produce **insufficient evidence**, with absolute diagnostic values and no performance deltas. Error counts, cancellations, and undispatched work remain visible. A complete single run per version permits descriptive differences but cannot show repeatability.

Run independence is an experimental assumption. Files from the same JVM or overlapping measurement windows can be dependent; copying an artifact does not create another replicate. Repeated paths, symlinks, and hardlinks to the same artifact are rejected across both groups. Individual requests are never used as experimental replicates. No significance test, confidence interval, or regression verdict is computed.

`--json -` writes only versioned comparison JSON to stdout and sends the summary to stderr. File output uses exclusive creation and never overwrites an existing measurement. Limits are 100 files per group, 1 MiB per file, 32 MiB combined input, and 32 JSON nesting levels. Run windows above 10¹² seconds are unsupported.

Comparison exit codes: `0` descriptive comparison of complete successful runs; `3` insufficient evidence due to failed/incomplete runs; `2` invalid arguments, rejected input, or incompatible measurements; `1` output/system error. Input read failures are reported as rejected inputs. A nonzero exit does not indicate a statistical regression.

An executable example using recorded application measurements is in [measurement validation](VALIDATION.md#native-saved-result-comparison).

## Measurement and exit semantics

- Each logical request starts immediately before registration with libcurl and ends when the event loop observes a completed transfer after the response body is consumed/discarded, or a terminal failure. Durations use `std::chrono::steady_clock`; they include DNS, TCP connection establishment, TLS negotiation/certificate verification where needed, and local event-loop dispatch/observation overhead. Reused connections do not repeat full connection establishment for every request.
- Complete 2xx responses are successful. Complete non-2xx responses, including redirects, count as HTTP errors. Resolution/connect errors, other network/protocol errors, recognized TLS failures, timeouts, and body-limit errors have separate counters. An incomplete 2xx body is a network failure, not a success.
- `completed` includes success and terminal failures, and excludes cancellations. `requested = started + not_started`; `started = completed + cancelled`. On SIGINT/SIGTERM, known completions are collected, further dispatch stops, remaining active requests are cancelled, and unused budget is reported as `not_started`. Partial results are exported.
- Closed-loop elapsed time covers first dispatch through final completion/cancellation. Fixed-rate elapsed time covers the arrival window and any drain, or stops early on interruption or engine failure. It excludes request body file loading, CA preparation, header-file parsing/list preparation, handle initialization, percentile sorting, JSON formatting, and idle connection cleanup. No warm-up is excluded. Completed and successful throughputs use this same interval and distinct numerators.
- `success_latency_ms` contains only complete 2xx samples. `completed_latency_ms` additionally includes HTTP, network, TLS, timeout, and body-limit failures. Cancelled and unstarted requests have no percentile samples. Percentiles use the exact nearest-rank method; empty distributions contain `null`, never invented zero latency.
- Both models bound concurrency. Closed-loop dispatch is completion-driven. Proxies, redirects and application-level retries are disabled. GET and HEAD reuse connections and may encounter a transparent libcurl retry on a stale connection. Each write uses a fresh connection. Counts describe logical requests, not guaranteed wire attempts.
- Exit codes: `0` all requested responses successful; `3` request failures/incomplete run; `2` invalid configuration; `1` engine/output error; `130` SIGINT; `143` SIGTERM. Initialization/configuration failures occur before a measured run; no measurement JSON is available, although a reserved new output file can remain empty.

Response body bytes are streamed without retention. Two bounded sample arrays retain exact latency observations (up to roughly 16 MB combined at the request cap, excluding container/library overhead). Fixed-rate runs reserve up to another 8 MB for dispatch delays. Mixed runs reserve up to 16 MB more for per-endpoint successful and completed duration samples, distributed across paths by planned counts. Percentile sorting occurs after the measurement window. Response headers, TLS buffers, and parsed trust stores are managed by libcurl and its backend. JSON includes the target URL and query; exported evidence can therefore contain sensitive input values. Remote DNS, platform certificate validation, and system scheduling can affect timeout and cancellation observation; local tests do not establish hard real-time guarantees or representative throughput capacity.
