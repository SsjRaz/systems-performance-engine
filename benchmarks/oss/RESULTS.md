# Real-application results on Linux

On one Linux guest, SPE distinguished a change in Meilisearch federated search from variation measured on the same release. At 1,000 filter values, mean successful latency rose **50.9%** in the first campaign and **63.2%** after recreating the indexes. The unchanged-version differences were **+0.8%** and **−7.5%**. Independent k6 measurements and Meilisearch’s own processing times moved in the same direction. Gitea exercised a second use case. Both the Git objects returned and the Git processes that served them could be checked independently of the load generator.

SPE 0.6.0 at `f2f48d2` generated the traffic. The guest ran GCC 14.2 Release, libcurl 8.14.1 and OpenSSL 3.5.7. The client used CPU 0 and the application used CPUs 1–2. The guest exposed five CPUs with a four-CPU quota. There was one application process at a time. Both campaigns used the same VM. [Run the experiments and review the upstream sources](README.md)

## Meilisearch

The reporter’s follow-up identifies the federated route, local indexes, filter grammar, seeded benchmark and server processing-time metric. We matched the two indexes of 100,000 articles, a filtered string article number, an empty text query and common 100- and 1,000-value cases. Our generator produced deterministic titles, descriptions and allergens. Both instances received byte-identical batches, and all 44 tasks per version succeeded in each campaign. The setup checked an ordinary full-text search and independently enumerated all six expected cross-index hits for a small filter. The timed 1,000-value query returned 20 hits from 2,000 eligible documents. [Upstream issue and later benchmark details](https://github.com/meilisearch/meilisearch/issues/6354) · [Reporter’s source](https://github.com/apt-TebbeM/meilisearch-issue-benchmark/blob/7d136d9e15ca9addd6a97a4227d8818c77f66375/App.cs)

Each row below covers four independently started server processes per label and 24 sequential POSTs per process. A/A used v1.41.0 on both sides. The version comparison used v1.41.0 then v1.42.0. The two campaigns recreated their databases independently. Native mean is weighted by successful requests, throughput includes connection setup and drain, and each percentile is the median of its four run-level values.

| Campaign | 1,000-value comparison | Mean ms | p50 ms | p95 ms | p99 ms | Successful requests/s |
|---|---|---:|---:|---:|---:|---:|
| 1 | Unchanged | 3.48 → 3.50 | 3.33 → 3.33 | 4.18 → 4.27 | 4.75 → 4.45 | 287.56 → 285.26 |
| 1 | Version change | 3.71 → 5.59 | 3.38 → 5.36 | 4.04 → 6.42 | 4.45 → 7.07 | 269.56 → 178.77 |
| 2 | Unchanged | 3.82 → 3.54 | 3.59 → 3.47 | 4.42 → 4.13 | 4.79 → 4.23 | 261.37 → 282.65 |
| 2 | Version change | 3.53 → 5.77 | 3.46 → 5.60 | 4.11 → 6.58 | 4.39 → 7.27 | 282.80 → 173.36 |

Every candidate process mean exceeded every baseline process mean within each campaign. The first campaign’s ranges were 3.41–4.49 and 5.41–5.84 ms. The repeat’s ranges were 3.48–3.59 and 5.59–6.08 ms. The 100-value case gave smaller native changes of +11.2% and +8.9%, with overlapping individual run means and unchanged controls of −8.5% and −3.7%. These observations provide weaker evidence for an effect at the smaller size.

| Campaign | k6 HTTP mean ms | Meilisearch processing mean ms | SPE trial app CPU, % of one core | SPE trial sampled app RSS, MiB |
|---|---:|---:|---:|---:|
| 1 | 3.47 → 5.97 | 2.38 → 4.92 | 83.64 → 92.08 | 60.84 → 77.51 |
| 2 | 3.63 → 6.37 | 2.58 → 5.23 | 88.46 → 88.23 | 59.94 → 77.58 |

These resource and k6 columns are medians across process runs. k6 validated response status, total matches, returned identifiers, index membership and generated titles on all 768 of its timed responses. Meilisearch exposes integer-millisecond processing time. Its timing excludes client and connection work.

At 6 scheduled arrivals/s and concurrency one, SPE and the server accounted for all 576 fixed-rate arrivals with zero late or capacity misses. Version-pair native means were 4.77 → 8.06 ms and 5.19 → 8.23 ms, while both versions delivered 6.00 successful requests/s. The controls’ latency changed +13.0% and +21.2%. The small fixed budget exposes a latency change while leaving delivered throughput nearly constant. Across both campaigns the access logs independently matched **2,112 SPE searches and 768 k6 searches**. SPE’s timed HTTP successes have separate before/after semantic checks because the engine discards response content.

**Reproduction boundary.** We reproduced the direction of the reported slowdown on this workload. The reporter’s opening observation was approximately 200 → 1,800 ms. The linked benchmark pins the candidate to v1.42.1, uses Bogus-generated content and explores larger filters. The original deployment, exact input behind that timing and cache conditions are unavailable. Our v1.42.0 tests use independently generated content and a 5,000-hit pagination setting. The 5,000-value request in our equivalent two-index encoding is 77,947 bytes. SPE rejected it before dispatch under its 65,536-byte body limit. It was excluded from timings and comparisons. No code-level cause, original slowdown magnitude or independent-host performance follows from these data.

## Gitea

Both campaigns recreated the same two Git histories on Gitea 1.24.6 with SQLite and system Git 2.56.1. Each repository contained 16 commits, 128 inventory files across 16 directories, and a README. Smart HTTP advertised the expected commit IDs after each push. The file endpoint returned the exact local blob bytes and SHA. The tree endpoint matched all 17 immediate Git entries and their object IDs. A preliminary setup probe of branch-detail metadata returned HTTP 500 after a push. The final integrity checks used advertised Git refs and the two measured object APIs. The metadata error was outside the measured paths and its cause was unverified.

Each campaign used eight fresh processes on unchanged code. A 3:2 file/tree mix at 20/s for three seconds delivered 36 file reads and 24 tree reads per run. Across both campaigns the access logs and SPE agreed on **576 file reads and 384 tree reads**, all successful, with zero missed arrivals. Each row compares four processes per label.

| Campaign | Mean ms | p50 ms | p95 ms | p99 ms | Successful requests/s | App CPU, % of one core | App + observed child RSS, MiB |
|---|---:|---:|---:|---:|---:|---:|---:|
| 1 | 15.16 → 14.44 | 14.40 → 14.08 | 23.84 → 19.12 | 43.06 → 44.02 | 20.00 → 20.00 | 33.18 → 31.33 | 192.94 → 193.09 |
| 2 | 14.48 → 14.49 | 14.34 → 14.40 | 19.13 → 20.41 | 37.39 → 32.91 | 20.00 → 20.00 | 31.19 → 31.53 | 195.66 → 191.15 |

The unchanged-version native mean differences were −4.73% and +0.03%. A separate Git Trace2 observation in each campaign saw zero Git starts in the idle window. Six file reads produced six `cat-file --batch` and six `cat-file --batch-check` starts. Six tree reads produced the same counts. This directly establishes that the mix exercises the subprocess path discussed upstream. Measuring the latency attributable to process creation or the benefit of pooling would require a separately validated implementation or profiler. [Gitea proposal #33952](https://github.com/go-gitea/gitea/issues/33952)

Two separate diagnostics checked negative outcomes. A missing object returned HTTP 404 in all four requests per campaign, with zero successful samples. Holding only the Gitea process for about 320 ms at 20 scheduled arrivals/s and concurrency one caused six and five capacity misses respectively. The service received the remaining 34 and 35 requests out of 40. The unpaused controls received all 40. Each native error or pause comparison reported `insufficient_evidence` and omitted percentage changes. This diagnostic validates accounting under controlled unavailability. It does not establish a production overload threshold.

## Product feedback

| Engineering question | Meilisearch | Gitea |
|---|---|---|
| Setup and useful work | Reusing one private POST body per search is simple. Index creation, task polling and semantic checks require an external driver. | The two-path GET mix needed no engine change. Reproducible Git history, setup credentials and offline configuration account for most setup work. |
| Workload identity | Both versions consumed identical input hashes. The native contract records byte length. The driver also checks hashes before acknowledging redaction. | Independently recreated commit, tree and blob IDs match. A weighted mix names an immutable tree and checks the file served by the seeded branch. |
| Distinguishing change from noise | The 1,000-value version difference exceeded both unchanged controls and separated all measured process means. | Only unchanged software was tested. Its measured variation sets context for future evaluations of a pooling change. |
| Scheduling and comparison | The access log reconciles arrivals. Native comparison can show stable throughput together with higher latency. | Independent per-path receipts reconcile normal and interrupted service. Native comparison withheld deltas for failures and missed work. |
| Capability limiting practical use | SPE cannot establish that a successful search returned eligible documents. The body limit also excludes the larger reporter case. | SPE cannot establish blob identity from a 200 response or attribute latency to Git subprocesses. The fixture and Trace2 supplied those checks. |

**Highest-priority next feature.** Add a bounded, untimed response-validation step that reuses the exact configured request and checks a few JSON fields or a content digest. Both benchmarks needed this before interpreting a 2xx response as useful work. Keep assertion results compact and retain existing timed latency and privacy semantics. A separately configurable bounded payload size would then extend coverage of large federated search bodies.

The new code consists of application fixtures, resource observation and reproducibility tooling. The SPE engine and its existing compatibility rules were sufficient for these supported workloads. CPU and memory use depend on this VM and the short run windows. Larger installations, real network latency, cold filesystem caches and a Gitea pooling comparison remain unmeasured.
