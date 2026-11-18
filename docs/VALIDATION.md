# Measurement validation

Methodology, tooling, and recorded findings for the controlled Petclinic experiments. Commands run from the repository root.

## Scope and platform requirements

The workflow uses a Petclinic checkout and preserved JARs with runtime metadata. The repository includes a pinned source specification, application patches, and a builder; compiled JARs and the historical raw archive remain outside Git.

| Component | Requirements and coverage |
| --- | --- |
| Petclinic comparison runner | Python 3.10+, Java from the recorded build, and POSIX `pgrep`, `lsof`, `ps`, and file locking. The application setup/smoke workflow has been exercised on macOS arm64; native engine tests also passed on Ubuntu arm64. Linux application setup remains unverified. |
| k6 reference | Optional k6 executable; the default local path selects the recorded macOS arm64 build. `compare_petclinic.py --k6 PATH` accepts another installation. |
| JVM diagnostics | JDK tools: `jcmd`, `jstat`, and `jfr`. |
| Host diagnostics | The current collector uses macOS `pmset`, `sample`, and `top` options. It is not a validated Linux collector. |

Consistent workload settings, power policy, warm-up, and background load matter on every platform. macOS-specific commands are identified below. These helpers add no runtime dependency to the native engine.

## Reproducible target setup

`scripts/prepare_petclinic.py` fetches public Spring Petclinic revision `500158f732419217507c7656904b8e6aa1bcc0d6` and verifies the source patches in [target.json](../benchmarks/petclinic/target.json). It builds baseline, owner-reuse candidate, and opt-in validation JARs sequentially. Each build executes the selected controller/integration tests; the validation build also runs the actual-HTTP control test.

Preparation requires Git, a JDK 21 installation, the POSIX tools listed above, and network access for the pinned checkout and Maven dependencies. The upstream Maven wrapper selects its distribution. `JAVA_HOME` or `--java-home PATH` selects the JDK when it is not the default on `PATH`.

After building the native engine:

```sh
python3 scripts/prepare_petclinic.py
python3 scripts/compare_petclinic.py --smoke \
  --petclinic build/targets/petclinic/source --port 8081
python3 scripts/compare_petclinic.py --smoke --scenario cpu-large \
  --petclinic build/targets/petclinic/source --port 8081
```

The default output `build/targets/petclinic/` must not already exist. `--output PATH` selects another new directory. Preparation preserves logs, test reports, patch identities, tool paths, and JAR hashes, then captures the reference owner page over actual HTTP. Maven and test JVM heaps are limited to 512 MiB; trial server heaps use the same limit. Interrupted preparation stops its owned build processes and retains partial artifacts. It does not modify the existing sibling checkout or require a previous server-run record.

Both patches retain the Spring Petclinic source notices and upstream license. [Attribution](../benchmarks/petclinic/NOTICE.txt) · [Petclinic license](../benchmarks/petclinic/PETCLINIC-LICENSE.txt)

The full suites accept `--petclinic PATH`, `--engine PATH`, `--k6 PATH`, and `--port`. For example, `python3 scripts/validate_measurements.py --suite smoke --petclinic build/targets/petclinic/source --k6 /path/to/k6` exercises SPE and k6 against the generated target. k6 is optional for the native-only commands above. The prior preserved-JAR workflow remains supported.

Local verification built a new checkout with 21 baseline, 22 candidate, and 23 validation tests passing, then completed four smoke experiments (16 fresh JVMs / 320 measured successful requests) across SPE and k6. These are functional/code-path checks, not a rerun of the historical performance study. Fresh builds record their own JAR hashes; identical historical hashes or timing results are not expected.

## Petclinic comparison runner

`scripts/compare_petclinic.py` is a local experiment helper for the separately installed Spring Petclinic target. It requires the preserved baseline/candidate JARs, their runtime metadata, the candidate patch, and the reference owner page in the sibling repository's `target/local-run/owner-reuse/` directory. It uses those built artifacts directly; it does not check out code or rebuild either application version.

Input verification and experiment execution:

```sh
python3 scripts/compare_petclinic.py
python3 scripts/compare_petclinic.py --run
```

The full protocol is four blocks in baseline–candidate–candidate–baseline order. Every block starts a fresh JVM, checks health and the seeded owner page, performs a separate 50,000-request warm-up, and executes five measured 50,000-request runs at concurrency 4 with a 2,000 ms timeout. Each JVM uses the same Java executable, fixed 512 MiB initial/maximum heap, packaged H2 configuration, restricted child environment, and logging settings. There is a fixed five-second rest between blocks. Runs with different JVM settings belong to separate comparison groups.

The runner verifies JAR hashes against their preserved metadata, records the engine hash, prevents overlapping runner instances, and checks for active `spe` and `k6` processes before and after workload runs. It stops an existing server on the chosen port only when its PID and JAR match the recorded managed server. An unknown listener causes an error. Trial JVMs are stopped on completion, failure, or interruption; the previous server is not automatically restarted. Trials do not modify source files or seed data.

Each execution creates a unique directory under `build/petclinic-results/`. It retains per-block server commands/logs, warm-up files, measured JSON/stdout/stderr, and `manifest.json` with provenance and descriptive block summaries. Errors stop the experiment and retain partial artifacts. There is no automatic significance test, percentile pooling, or selective removal of slower runs. A fixed warm-up does not prove steady-state behavior; block ordering and per-run measurements are retained for trend analysis.

Completed experiments automatically produce `comparison.txt` and `comparison.json`. The report revalidates block order, recorded artifact identities, JVM settings, workload/engine compatibility, successful outcomes, and accounting. It reports combined throughput, request-weighted mean latency, and the median of per-run p50/p95/p99 values with candidate-versus-baseline differences. The percentile summaries are explicitly not pooled request percentiles. Invalid or incomplete input produces failed checks and no performance deltas; smoke workloads produce functional checks only. No result is labeled statistically significant.

To generate or refresh the report for an existing experiment without sending traffic:

```sh
python3 scripts/compare_petclinic.py --report build/petclinic-results/petclinic-abba-<experiment-id>
```

### Experiment environment

Trials run sequentially to avoid competition between baseline and candidate processes. Comparable measurements require a consistent power policy, controlled background load, and uninterrupted execution. Process checks detect some overlapping work; they do not provide system-wide isolation.

Smoke mode exercises startup, HTTP traffic, artifact creation, and shutdown with small request budgets. Its results are functional checks and are excluded from performance analysis:

```sh
python3 scripts/compare_petclinic.py --smoke --port 8081
```

The selected port must be free or owned by the recognized managed server.

### macOS sleep prevention

On macOS, the optional `caffeinate` wrapper prevents idle sleep for the duration of a run:

```sh
caffeinate -i python3 scripts/compare_petclinic.py --run
```

`caffeinate` is not required by the engine or runner. Sleep prevention on other systems is managed through the host's power settings or experiment environment.

## Controls and reference tool

The validation helpers reuse this runner and the native engine. They keep raw outputs in unique, ignored directories and produce descriptive changes without statistical verdicts. The observational unit is a fresh JVM block; multiple requests or consecutive measured runs in one JVM are not independent experiments. Even fresh JVMs on one host can share temporal drift.

An unchanged-code experiment maps both labels to the exact same preserved JAR and verifies its SHA-256:

```sh
python3 scripts/compare_petclinic.py --run --port 8081 \
  --same-artifact baseline --warmup-requests 50000 --requests 20000 --runs 2
```

The separately installed Petclinic target also contains an opt-in `measurement-validation` profile. Its interceptor executes actual bounded work on `/owners/1`: no additional work (`control`), a 5 ms sleep (`delay`), or 10,000 / 1,000,000 arithmetic iterations (`cpu-small` / `cpu-large`). All four scenarios use the same validation JAR, containing the owner-reuse edit under both labels; only configured extra work differs. Controls are configured after a common warm-up and followed by an excluded conditioning run. Per-run server counters verify request counts, delay calls, CPU calls, and executed iterations; the resulting checksum prevents the CPU result from being discarded. Readiness and post-configuration checks compare the owner HTML byte for byte with the reference page. A configured sleep is a minimum requested delay, not an exact increase in observed latency.

These scenarios require `target/local-run/measurement-validation/validation.jar`, `build.json`, `source.patch`, and `baseline-owner.html` in the sibling Petclinic repository. The archive also retains the Maven build/test log. It was built with Java 21 from the recorded Petclinic revision plus the archived source patch. The profile is inactive during normal application operation. This helper is specific to the preserved local target; it is not a general service launcher or a public-network benchmark.

```sh
python3 scripts/compare_petclinic.py --run --port 8081 \
  --scenario delay --warmup-requests 50000 --requests 10000 --runs 2
```

For an independent reference, `--tool k6 --k6 /path/to/k6` selects the adapter in `scripts/reference_k6.js` / `scripts/reference_k6.py`. The local validation used the official k6 v2.4.0 macOS arm64 release, verified against the release SHA-256 checksums; its installation metadata remains under `build/tools/k6-v2.4.0/`. k6 is an optional experiment tool, not an engine dependency.

| Setting | SPE | k6 reference |
| --- | --- | --- |
| Workload | Fixed request count, closed-loop concurrency 4 | Shared iterations, four VUs, one synchronous GET per iteration |
| HTTP policy | HTTP/1.1, reuse, identity encoding, no redirects or proxies, complete 2xx success | Same policy for these local targets; negotiated protocol is checked |
| Timeout | 2,000 ms total transfer/connect budget | 2,000 ms request timeout; separate executor duration cap |
| Request latency | Monotonic registration through completion observation, including connection work | High-resolution `http_req_duration`: sending + waiting + receiving, excluding initial DNS/connect |
| Throughput window | First dispatch through final completion observation | First VU start through final HTTP return; millisecond-resolution wall clock extrema |
| Percentiles | Exact nearest rank | k6 trend interpolation |
| Body limit | Configurable, 16 MiB default | Not implemented by this adapter; Petclinic response is below the native limit |

Cross-tool analysis compares effect directions, outcome accounting, and repeatability within each tool. Absolute request latencies and percentile values across these tools are not equivalent measurements. The normalized reference JSON explicitly records the different definitions; original k6 summaries remain alongside it. k6's reported concurrency is the configured VU upper bound; functional fixture tests separately check server-observed concurrency. See the [k6 metric definitions](https://grafana.com/docs/k6/latest/using-k6/metrics/reference/) and [shared-iterations executor](https://grafana.com/docs/k6/latest/using-k6/scenarios/executors/shared-iterations/).

The validation sequence runs a smoke suite before the timing and diagnostic suites:

```sh
python3 tests/validate_reference.py
python3 scripts/validate_measurements.py --suite smoke
python3 scripts/validate_measurements.py --suite timing
python3 scripts/validate_measurements.py --suite controls
python3 scripts/validate_measurements.py --suite diagnostics
python3 scripts/summarize_validation.py
```

The reference check sends real traffic for success, HTTP errors, redirects, delay, timeout, truncation, disconnect, and refused connections. The timing matrix repeats identical-JAR controls for each tool, then tests the original owner lookup edit, delay, and both CPU doses. The controls suite adds identical settings on the validation-profile JAR. Request budgets are recorded in every manifest. Only one JVM and one load generator run at a time, with a fixed 512 MiB heap and rest between blocks.

Diagnostics run separately with JFR, GC/safepoint logs, JIT compilation logs, compiler/heap snapshots, process CPU/RSS samples, short macOS stack samples of the generators, and available load/power/memory information. They cover unchanged baseline code with each tool and the large CPU-work scenario with SPE. Profiler and attachment overhead affect these runs; they are excluded from performance comparisons. macOS power warnings do not establish core frequency or temperature, and sampled CPU time alone cannot rule out generator saturation. A fixed warm-up does not demonstrate a steady-state JVM.

## Executed local timing observations

The initial validation matrix executed on 2026-11-15 UTC used an Apple M3 / 8 GiB Mac on AC power, macOS 26.0.1, Temurin 21.0.7, the seeded in-memory H2 database, and `/owners/1`. All comparisons used four fresh JVMs in A–B–B–A order, concurrency 4, a 512 MiB heap, 50,000 excluded warm-up requests, two measured runs per JVM, and five seconds between JVMs. Each measured run contained 20,000 requests for original-artifact comparisons and 10,000 for injected-work cases; the latter also used 2,000 excluded conditioning requests. This matrix contains 48 JVM blocks and 1,440,000 measured requests, with all measured responses recorded as successful HTTP 200.

Two repeated identical-baseline-JAR comparisons per tool produced the following differences between labels with **no application change**:

| Tool | Throughput change, comparison 1 | Comparison 2 | Eight-block throughput range | Sample CV across blocks | Block mean-latency range |
| --- | ---: | ---: | ---: | ---: | ---: |
| SPE | −6.81% | +8.18% | 2,514.6–4,056.8 req/s | 17.01% | 0.985–1.590 ms |
| k6 | +1.94% | +2.34% | 3,487.4–4,118.2 req/s | 7.28% | 0.940–1.109 ms |

These ranges include temporal drift across separate experiments; they do not measure an intrinsic difference in tool stability or define a universal noise threshold. JVM blocks share a host and can remain temporally dependent.

A later unchanged-code control using the validation-profile JAR added eight JVM blocks and 160,000 measured requests, bringing unprofiled validation to **56 JVM blocks / 1,600,000 measured requests**. With identical application settings, SPE's label-to-label throughput difference was −0.28% (four-block throughput CV 3.12%; block mean latency 1.029–1.110 ms), and k6's was −5.27% (CV 4.63%; mean latency 1.015–1.129 ms). These controls use different application instrumentation and run budgets from the original-JAR controls, so they remain separate groups in the analysis.

Observed changes within each tool, candidate relative to its baseline:

| Application change | SPE throughput | k6 throughput | SPE mean latency | k6 mean latency |
| --- | ---: | ---: | ---: | ---: |
| Reuse owner model attribute | +2.76% | −1.95% | −2.69% | +1.85% |
| Requested 5 ms processing sleep | −85.35% | −85.19% | +582.86% | +595.12% |
| 10,000 CPU iterations/request | +1.52% | −1.60% | −1.49% | +1.84% |
| 1,000,000 CPU iterations/request | −36.12% | −43.90% | +56.58% | +82.21% |

Both candidate JVM blocks were slower than both baseline blocks in throughput and mean latency for the large delay and CPU cases under both tools. The small CPU dose and original edit were not reliably distinguished from variation. These are descriptive observations, not established false-positive rates, detection guarantees, or statistical verdicts. The large CPU case even reduced SPE's median run p99 by 18.98% while increasing mean latency: unusually slow baseline tails show why one percentile is insufficient evidence.

A separate SQL diagnostic sent five real owner requests to each original JAR with SQL logging enabled. Every baseline request logged four SELECT statements; every candidate request logged two, and all ten HTML bodies were byte-identical. This verifies that the owner-reuse edit removes database work without establishing a reliable net performance benefit. Optional Hibernate session metrics were not emitted and are recorded as unavailable. `python3 scripts/inspect_petclinic_sql.py` reproduces the logging diagnostic in a separate run; its measurements are excluded from latency benchmarks.

The tools agree on these substantial trends, but their effect sizes are not identical. The large CPU case's throughput reduction was 36.12% with SPE and 43.90% with k6. Sequential experiments encounter different host/JVM states, and the adapters have distinct timing boundaries and overhead; this validation does not causally separate those contributions. Absolute latency values across tools remain incompatible.

Three separate diagnostic A–B–B–A experiments completed (12 JVMs, 24 successful generator stack captures, readable JFR/GC/JIT artifacts). In unchanged-code diagnostics, 116–165 further compilations occurred between the post-warm-up and post-measurement snapshots, adding 0.28–0.95 seconds to jstat's compiler `Time` counter. GC pauses inside the outer measured-client process intervals ranged up to 84.104 ms. Sampling across all diagnostic cases showed SPE at approximately 0.09–0.23 CPU cores and k6 at 0.48–0.61 cores, while JVM CPU use was roughly 2.98–3.67 cores. These are sampled CPU-time deltas, not peak utilization or a capacity test. JVM sampled RSS reached 581 MiB despite the separate 512 MiB heap limit; heap limits do not cap total process memory.

macOS snapshots recorded compression/decompression, page-in/out, and swap-in activity. Power snapshots stayed on AC and reported no recorded thermal warning, but did not provide actual core frequency/temperature. Native stack samples commonly waited in libcurl polling; JFR execution samples in both CPU-work candidate JVMs captured the injected `preHandle` method. This supports actual work execution and offers plausible contributors to variation; it does not causally explain the earlier unprofiled differences, rule out transient contention, or establish a fully warmed JVM. Diagnostic attachment and sampling overhead are excluded from the timing matrix, and first-sample `top` process CPU percentages are not used as utilization estimates.

All 16 actual-HTTP reference accounting checks passed (eight cases per tool, 80 requests each). Server observations confirmed four reused connections for successful and delayed cases and peak server concurrency 4 for the delayed case under each tool. The fast k6 fixture case peaked at 2 server requests despite four configured VUs; the configured concurrency is a limit rather than a guarantee of simultaneous server execution.

A small [historical evidence excerpt](../benchmarks/evidence/petclinic-delay/provenance.json) contains the first measured baseline/candidate run from the delay experiment for each tool. The four result files are unchanged copies, with source hashes and selection provenance. They illustrate the recorded output and are not a complete comparison dataset.

The raw matrix is retained locally under `build/validation/timing-20261115T035625Z-ik0xzh0l/`; matching controls, diagnostics, and successful SQL checks are under `controls-20261115T043205Z-0qv5bzzq/`, `diagnostics-20261115T043601Z-l1c_qizt/`, and `sql-check-dtarb4tj/` in the same root. Reference checks are under `build/validation/reference-accounting-7d3aol25/`. Source snapshots and binary hashes accompany the experiments. `scripts/summarize_validation.py` produces `build/validation/analysis.json` and `blocks.csv` from completed archives. An initial SQL diagnostic aborted because it required the unavailable session metrics; its raw output is retained in `sql-check-e2av0kao/`. Corrected validation-build provenance is retained in sidecars without rewriting original timing manifests. The native request samples themselves are not exported; the retained native JSON contains the engine's original per-run aggregates. All completed runs are included. These local application experiments establish neither production capacity nor general timing accuracy.

## Native saved-result comparison

The native command can reproduce a descriptive comparison directly from the checked-in SPE excerpt:

```sh
build/release/spe compare \
  benchmarks/evidence/petclinic-delay/spe-baseline.json \
  benchmarks/evidence/petclinic-delay/spe-candidate.json
```

Executed against these original saved measurements, it reports 2,960.699 → 472.800 successful req/s (−84.03%) and mean latency 1.350 → 8.458 ms (+526.33%), with 10,000 successful responses and no errors per file. This is one selected run per version from the historical 5 ms processing-delay experiment; it is not a repeatability study or a new benchmark execution.

Comparing all eight measured native files from that same A–B–B–A experiment reports 3,245.560 → 475.382 req/s (−85.35%) and mean latency 1.232 → 8.413 ms (+582.86%), matching the earlier descriptive analysis. Each version has four files from two JVM blocks, with 40,000 successful responses. Baseline per-run throughput spans 2,960.699–3,400.094 req/s; candidate spans 472.091–479.379 req/s. Files within a JVM block do not establish independent replication. Original files are unchanged; command, input hashes, reports, and binary identity are retained locally under `build/validation/native-compare-hks6mkx9/`.

Comparison tests also execute real loopback traffic, save native results, and consume those files through `spe compare` for success, HTTP errors, mixed outcomes, truncated bodies, and timeouts. Separate numerical fixtures test arithmetic and rejection rules; they are synthetic correctness inputs, not performance evidence. Artifacts are retained in each build's `test-results/comparison/` directory. The command rejects k6-normalized results rather than comparing incompatible latency definitions. No statistical verdict is computed.

## Owner-reuse experiment with a larger generated dataset

A separate local study on 2026-11-16 reused the genuine owner-controller edit with a deterministic H2 dataset of 10,000 owners, 20,000 pets, and 132,044 visits. The selected page contained three pets and 33 visits. Five diagnostic requests per version logged four SELECTs for baseline and two for candidate; their 9,196-byte HTML responses were identical. All 93 dependency JARs were byte-identical; only the controller class and Git build metadata differed.

Sixteen fresh, nonoverlapping JVM/database blocks used 384 MiB maximum heaps and concurrency two. Each block excluded 20,000 warm-up requests and measured two 10,000-request runs. Separate identical-code controls and code-change sequences used balanced A–B–B–A or B–A–A–B ordering with SPE and k6. All 320,000 measured requests succeeded; 320,000 warm-up requests were excluded.

| Tool | Code-change throughput | Mean latency | Identical-code label throughput difference |
| --- | ---: | ---: | ---: |
| SPE | +2.03% | −1.99% | +27.18% |
| k6 | +3.21% | −3.13% | −4.56% |

Individual paired code-change contrasts changed direction for both tools. Identical-code block throughput CVs were 16.64% for SPE and 17.33% for k6. Paging and continued JVM compilation were observed without establishing their causal contributions. The SQL reduction is verified; a reliable end-to-end speedup is not established. These are generated-data, local H2 hot-endpoint observations, not production capacity measurements or statistical verdicts. Sampled process-memory observations do not establish a general efficiency advantage for either tool.

Raw inputs, source/bytecode checks, command lines, response checks, SQL traces, measurements, resource samples, native comparison reports, and block-level analysis remain local under `build/application-experiments/owner-reuse-20261116T184843Z-ubig3a6z/`. Temporary scripts and archives from that directory are not part of the repository.
