# Benchmarking local Meilisearch and Gitea with SPE

These experiments exercise a federated catalog search and reads of real Git objects. The clients connect to loopback. Each run creates its own databases, repositories, configuration and evidence under the specified output directory. [Measured results](RESULTS.md)

## Run the experiments

Use Linux amd64, Python 3.10 or later, Git, taskset, a C++20 compiler, CMake 3.24 or later and the libcurl and OpenSSL development packages. Allow three CPUs in the process affinity mask and about 2 GiB of spare disk for the binaries, build and one campaign. The application and the client use different CPUs. Keep builds and other benchmarks outside the measurement windows.

Run these commands from the SPE repository root.

```sh
cmake -S . -B build/oss-release -DCMAKE_BUILD_TYPE=Release
cmake --build build/oss-release --parallel 2
python3 benchmarks/oss/fetch_tools.py --output build/oss/tools

python3 benchmarks/oss/meilisearch_bench.py prepare \
  --tools build/oss/tools --output build/oss/meili-1
python3 benchmarks/oss/meilisearch_bench.py measure \
  --tools build/oss/tools --engine build/oss-release/spe \
  --k6 build/oss/tools/k6 --output build/oss/meili-1

python3 benchmarks/oss/gitea_bench.py \
  --binary build/oss/tools/gitea-1.24.6 --engine build/oss-release/spe \
  --output build/oss/gitea-1
```

Use fresh output directories for each independent campaign. The fetcher checks published release artifact hashes and extracted executable hashes. Preparing the targets requires no Docker daemon or upstream account. Private state under `state/` includes the disposable Gitea credentials. Share the compact JSON under `evidence/`. Both servers have a four-minute lifetime limit per block, a 1.5 GiB sampled RSS guard and two assigned CPUs. Meilisearch indexes one batch of 5,000 documents at a time with one indexing thread and a 192 MiB indexing budget.

## Meilisearch

The upstream reporter supplied a seeded catalog benchmark after filing the issue. It uses two local indexes, 100,000 articles per index and federated searches with a quoted `articleNumber IN [...]` filter. The published Compose file compares 1.41.0 with 1.42.1. The original report identifies 1.42.0. This experiment pins 1.41.0 and 1.42.0, uses independently generated deterministic articles and exercises two of the reporter's filter sizes. [Issue and follow-up discussion](https://github.com/meilisearch/meilisearch/issues/6354) · [Reporter’s benchmark at the reviewed revision](https://github.com/apt-TebbeM/meilisearch-issue-benchmark/tree/7d136d9e15ca9addd6a97a4227d8818c77f66375)

Preparation waits for each indexing and settings task to succeed. It checks both document counts, indexing status, a full-text search, and the six hits from a three-value filter across the two indexes. Each timed request uses a 20-hit federated response. The app must report 200 total matches for the 100-value filter and 2,000 for the 1,000-value filter. Before and after each block, the harness checks returned fields, membership and uniqueness against the generator. k6 checks all its measured responses. SPE streams response bodies away, so its timed requests are verified through access-log receipts and separate semantic probes. [Official federated search guide](https://github.com/meilisearch/documentation/blob/55a65e53bda2af50146ff043e7c67c9bc45f615e/capabilities/multi_search/getting_started/federated_search.mdx) · [Filter syntax](https://github.com/meilisearch/documentation/blob/55a65e53bda2af50146ff043e7c67c9bc45f615e/capabilities/filtering_sorting_faceting/advanced/filter_expression_syntax.mdx) · [Task monitoring](https://github.com/meilisearch/documentation/blob/55a65e53bda2af50146ff043e7c67c9bc45f615e/capabilities/indexing/tasks_and_batches/monitor_tasks.mdx)

Each campaign runs sixteen fresh server processes, reopening the database for the assigned version. The four blocks of four are unchanged A–B–B–A, version-change B–A–A–B, unchanged B–A–A–B, and version-change A–B–B–A. Both labels use 1.41.0 in the unchanged controls. Case order alternates within each block and each case has a semantic warm-up. Each block exercises four cases.

- SPE sequential POST, 24 requests each for the 100-value and 1,000-value filters.
- SPE fixed-rate POST, 6 arrivals/s for three seconds, concurrency one, 1,000 values.
- k6 sequential POST, 24 requests and one VU, 1,000 values. A fresh connection per request matches SPE’s POST policy. k6 HTTP duration excludes connection establishment. Its server-time trend comes directly from Meilisearch’s integer-millisecond `processingTimeMs` field.

SPE uses one loopback origin throughout each campaign. The harness records and rechecks exact body and header file hashes before each run. It then uses SPE’s documented acknowledgements for redacted bodies and headers when comparing matching cases. Each `compare-{aa,ab}-{short,large,paced}.json` is a native SPE report. Independent campaign results are reported separately.

## Gitea

The fixture starts Gitea 1.24.6 with SQLite, local Git storage, disabled SSH and offline mode. It provisions two repositories through the API and pushes sixteen dated commits to each over local smart HTTP. Each contains 128 inventory files in sixteen directories and a README. The seed checks advertised commit IDs. The read checks compare decoded contents against `git show` and directory entries, types and object IDs against `git ls-tree`. [Gitea API specification for the pinned release](https://github.com/go-gitea/gitea/blob/v1.24.6/templates/swagger/v1_json.tmpl)

Eight fresh processes use the same binary and database in A–B–B–A / B–A–A–B order. SPE schedules 20 GETs/s for three seconds with concurrency two. A 3:2 mix reads a file from one repository and an immutable tree from the other. The harness reconciles each endpoint’s starts with the server access log. Git Trace2 is enabled only in a separate diagnostic containing an idle window and six reads per endpoint. Two additional controls exercise a missing object and a brief service pause at concurrency one. They retain native comparison output for incomplete workloads. [Upstream subprocess-pooling proposal](https://github.com/go-gitea/gitea/issues/33952) · [Git Trace2](https://git-scm.com/docs/api-trace2)

## Interpreting the evidence

Native successful throughput uses total successes divided by total engine elapsed time. Native mean latency weights each run by its successful sample count. Native percentile comparisons use the median of the run-level percentiles. Use the run ranges and unchanged-version controls when interpreting differences. A fixed offered rate can keep throughput stable while service latency changes.

The harness samples Linux RSS every 50 ms. For Gitea this is the sum for the server and its observed direct children, so shared pages may be counted more than once and short-lived processes may be missed. App CPU comes from the server’s user/system time plus waited-for children, divided by the external measurement window, in percent of one logical CPU. That window includes client startup and reporting. Short closed-loop runs have coarse CPU accounting. The Web server, logs, database and client all share one Linux guest.

## Attribution

Meilisearch Community Edition release binaries come from Meili SAS. The upstream repository provides MIT terms for the community portions and separate terms for enterprise portions. Gitea is distributed under the MIT license by the Gitea Authors and the Gogs contributors. Grafana k6 is distributed under AGPL-3.0. This repository supplies the experiment drivers and generated fixtures. The release binaries are downloaded locally. [Meilisearch license](https://github.com/meilisearch/meilisearch/blob/v1.42.0/LICENSE) · [Gitea license](https://github.com/go-gitea/gitea/blob/v1.24.6/LICENSE) · [k6 license](https://github.com/grafana/k6/blob/v1.0.0/LICENSE.md)
