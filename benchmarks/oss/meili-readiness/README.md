# Meilisearch shared filter context on two Linux hosts

The v1.42.0 ownership intervention reduced latency for the two-index, 1,000-value federated search on AMD EPYC and Intel Xeon. The Intel study also tested the normal feature configuration. Before this study, Meilisearch had already shipped a broader range-based representation in v1.56.1 through PR #6695. It is present in v1.58.0. The historical patch and measurements are suitable as retrospective evidence. A duplicate implementation PR would add little. [Maintainer note](UPSTREAM-NOTE.md) · [Upstream PR #6695](https://github.com/meilisearch/meilisearch/pull/6695)

## Primary timing

Each entry is the median paired percentage change in mean request latency, with an exact sign-test interval. Negative values are faster. The unchanged comparison used eight fresh process pairs. Each patch and residual comparison used twelve. Each process handled 384 timed searches and one identical verified preflight. The practical threshold was 10%. The intervals have at least 95% coverage per comparison under independent-pair assumptions.

| Host and source configuration | Stock v1.42 vs itself | Patched vs stock v1.42 | Patched v1.42 vs stock v1.41 |
| --- | --- | --- | --- |
| AMD EPYC 9V74, reduced, original study | — | −42.1% [−49.5%, −38.2%] | +19.6% [+13.7%, +28.2%] |
| Intel Xeon 8573C, same reduced ELFs | +1.6% [−5.4%, +9.0%] | −27.9% [−29.5%, −25.3%] | +14.6% [+11.2%, +18.8%] |
| Intel Xeon 8573C, normal defaults | +0.3% [−3.6%, +5.9%] | −28.5% [−30.2%, −19.3%] | +12.3% [+8.7%, +18.2%] |

On Intel with normal defaults, the unprofiled stock and patched arm medians were 6.87 and 4.80 ms. Median user CPU per confirmed search was 6.26 → 4.44 ms. Median process lifetime high-water RSS was 86.6 → 59.8 MiB. The corresponding reduced-feature RSS was 73.3 → 47.7 MiB. RSS includes the server, mapped indexes, warm-up and timed workload. Both VMs assigned the client a separate CPU and the server two CPUs. They used the same kernel version, CPU quota and fixture. [Original measurements](../MEILI-PROFILE.md)

## Correctness and other workloads

Stock and patched v1.42.0 agreed on 34 reduced-feature checks, plus two localized search subterms and two byte-verified dashboard assets with normal features. The two official release binaries matched their respective normal source builds on all 38 checks. Filters covered empty, singleton and repeated values, quoted and escaped text, Unicode, nested boolean conditions, array syntax, ranges, and two invalid requests on single and federated routes. Every indexing window recovered both full 512-document corpora. Two federated error strings had already changed between official v1.41.0 and v1.42.0. One lost the query-position prefix and one lost the source-column range. The intervention preserved v1.42.0's status, code, type, link and message.

| Patched vs stock v1.42, unprofiled | Reduced features | Normal defaults |
| --- | --- | --- |
| Three-value federated list | −1.6% [−23.7%, +8.6%] | +0.2% [−10.6%, +7.7%] |
| Single-index 1,000-value filter | +3.2% [−4.8%, +10.2%] | −7.4% [−30.6%, +57.6%] |
| Single-index equality | +6.9% [−8.5%, +23.6%] | −5.4% [−47.2%, +6.7%] |
| Ordinary full-text search | −0.9% [−16.4%, +33.7%] | +3.8% [−1.9%, +20.7%] |
| Two indexing tasks, sum of server task durations | +0.2% [−54.5%, +38.9%] | −8.4% [−20.9%, +16.1%] |
| Empty federated list | — | +0.8% [−34.1%, +77.8%] |
| Singleton federated list | — | +3.8% [−12.7%, +50.4%] |

None of the tested secondary intervals lay wholly above +10%. The normal-build indexing high-water RSS medians were 500.1 and 499.9 MiB for stock and patched v1.42.0. Each secondary comparison used eight balanced pairs. A search window contained 128 successful timed requests plus a preflight. Within a pair, both processes used the same workload order. Short-list HTTP tests were added after the isolated allocation finding. These exploratory intervals are unadjusted for multiple comparisons. Several intervals still accommodate material slowdowns. The Intel archive covers 376 distinct server processes and 109,568 successful native measurement requests, including diagnostic windows. It retains all unchanged-code controls and every per-process observation.

| Isolated v1.42 parser conversion | Allocation calls, stock → patch | Requested bytes, stock → patch |
| --- | ---: | ---: |
| Equality | 4 → 4 | 28 → 28 |
| Empty list | 2 → 3 | 12 → 44 |
| Singleton list | 4 → 4 | 34 → 52 |
| Eight catalog values | 18 → 11 | 624 → 184 |

These single-threaded counts cover one ownership conversion with the system allocator. Meilisearch uses mimalloc in the server. An early shared Cargo target reused the stock parser for both checkouts. Both arms were rebuilt in empty separate target directories, and the patched mixed-context test confirmed arm identity. All 11 stock and 12 patched parser tests passed.

## Residual investigation

* **Intel reduced builds.** 3,200 samples in four separately profiled pairs. The CPU gate returned `no_consistent_material_user_cpu_increase` and produced no ranked functions. `mdb_node_search` accounted for 98/1,509 → 124/1,691 sampled leaves.
* **Intel normal builds.** Timing did not pass the regression gate, so no residual CPU profiles were collected.

Inlining and unattributed samples limit interpretation of individual leaf symbols. The following instrumented wall times are medians of four per-process medians, each from eight requests after warm-up. The columns compare v1.41.0 to patched v1.42.0. Parent stages include their children. To estimate the unlabelled time, subtract only the immediately nested stages within the same request, using the rounded durations supplied by Meilisearch.

| Instrumented stage, ms | Reduced | Normal |
| --- | --- | --- |
| Whole search | 3.907 → 4.443 | 3.860 → 4.390 |
| Query partitioning | 0.016 → 0.118 | 0.016 → 0.107 |
| Execute local search | 3.767 → 2.782 | 3.708 → 2.675 |
| Search time outside named direct children | 0.013 → 1.425 | 0.015 → 1.353 |

v1.42.0 prepares filters before the partition timer starts. The two releases therefore include different work within their named stages. They also use different locked `heed` and `lmdb-master-sys` versions. A matched counter around filter preparation would distinguish that work from other unlabelled time. Neither these wall-time estimates nor the sampled leaf counts establish the cause of the full residual. [v1.41.0 federated search](https://github.com/meilisearch/meilisearch/blob/8dd5a9511e7757b72883c07efba34c05213f78f6/crates/meilisearch/src/search/federated/perform.rs) · [v1.42.0 filter preparation](https://github.com/meilisearch/meilisearch/blob/02ff9a4a1cae79ce405638b6323fbd2f939f7f67/crates/meilisearch/src/search/federated/perform.rs#L110)

## Verification and scope

All 14 SPE suites passed in both Linux Release and ASan/UBSan in quiet local runs, including real HTTP and HTTPS. An earlier Release headers test during Rust compilation recorded a missed arrival and four successful HTTPS requests out of five. Three further quiet Release repetitions of the headers suite passed with the original test. PR #10’s six v2 reports, its 16 original sampled captures, and six older v1 reports also replayed. [Focused review](REVIEW.md)

The normal builds used the locked upstream default features, authentic Japanese and Korean dictionary inputs, the upstream dictionary compiler, and the verified v0.4.1 dashboard. The task’s proxy returned HTTP 403 at CONNECT for the historical dictionary host. Individual source files were verified against their upstream Git or LFS object identities. Rebuilding the dictionary cache from scratch reproduced every generated component hash. The archive records fixture and build fingerprints, versions, CPU assignment, resource counters and return-value digests. The study covers fixed local catalogs, a short indexing batch and concurrency one. Remote federation, long-running mixed loads and current-release latency remain unmeasured.

The archive SHA-256 is `ff21327ea5f06471d05fefe7d47e80b1851dab9181fed7d43109c99e8cb37589`. [Evidence archive](../evidence/meili-readiness-linux-x86_64.tar.gz) · [Reproduction and replay](REPRODUCE.md) · [Study protocol](DESIGN.md)
