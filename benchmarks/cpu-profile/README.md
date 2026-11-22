# Differential CPU diagnosis

SPE can compare sampled application CPU costs after its paired detector confirms an independent latency or throughput regression. The opt-in Linux study serves freight quotes over HTTP. All three builds calculate the same response over the same 32,768 parcels. The candidate scans a zone-rate table on every lookup. The baseline and reverted builds index it directly. A separate control adds a 2 ms sleep to the baseline executable.

## Measured on Linux

Commit `c54c319` fixed the protocol before collection. SPE ran 96 fresh server processes on one Linux x86-64 cloud VM with GCC 14.2.0. All 61,440 timed requests succeeded and all 96 separate preflights matched the reference digest. The timing experiments and the diagnostic captures used disjoint processes.

| Unprofiled comparison | Independent pairs | Median paired change in mean latency | Exact median interval |
| --- | ---: | ---: | ---: |
| Indexed lookup → linear scan | 8 | +334.0% | +327.3% to +371.5% |
| Indexed lookup → same binary with a 2 ms wait | 8 | +273.1% | +254.1% to +298.2% |
| Linear scan → rebuilt revert | 8 | −77.7% | −78.4% to −74.0% |
| Indexed lookup → rebuilt revert | 16 | −1.8% | −2.5% to +2.3% |

The restored source hashes, ELF SHA-256 and GNU build ID matched the baseline. The independent recovery interval fell inside the predeclared ±10% band. The original directional detector called baseline versus revert inconclusive, as expected when the interval includes zero.

In the CPU study, measured application user CPU rose from **0.654 to 3.366 ms per success**. Gperftools delivered 430 baseline and 2,170 candidate samples across four pairs. Estimated self CPU for **`freight::lookup_rate`** rose from 0.012 to 2.312 ms per success. It ranked first in every pair, with increases of 2.08–2.69 ms. A captured stack led from `serve_one` through `freight::quote` to the candidate's `catalog.cpp:9`, where the loop compares zone IDs. The timed recovery after reversing this patch supports attribution on the tested fixture. [Injected source change](linear-lookup.patch#L7)

In the sleep control, profiled user CPU moved from 0.689 to 0.711 ms per success while wall time rose from 0.810 to 2.953 ms per success. SPE withheld a CPU suspect. This control tests the distinction between waiting and CPU consumption. The study makes no function-level attribution for Meilisearch or Gitea.

The host denied `perf_event_open` with `EPERM`. The perf client reported `No permission to enable cpu-clock:u event.` Live measurements used Debian gperftools 2.16. A synthetic offline record exercised Linux perf 6.12.117 export and real ELF source resolution. Live perf capture and independent-host replication remain open.

The compressed replay contains the native results, process receipts, normalized stacks, protocol, build identities and original reports. It can be checked without profiler access. [Portable evidence](evidence/linux-x86_64-replay.tar.gz)

```sh
mkdir -p build/replay
tar -xzf benchmarks/cpu-profile/evidence/linux-x86_64-replay.tar.gz -C build/replay
(cd build/replay/quote-study && sha256sum -c SHA256SUMS)
build/release/spe profile-diff build/replay/quote-study/cpu/profiling.json
build/release/spe analyze build/replay/quote-study/recovery/timing.json
```

## Reproduce the study

Build a Release SPE binary. The study needs two available Linux CPUs, a C++20 compiler, GNU binutils, patch, taskset, and either Linux perf with userspace call-chain access and Python scripting or gperftools with libprofiler and google-pprof. Run under the account that owns the fixture. The output path must be new.

```sh
cmake -S . -B build/release -DCMAKE_BUILD_TYPE=Release
cmake --build build/release --parallel 2
python3 benchmarks/cpu-profile/experiment.py --engine build/release/spe --output build/quote-study
build/release/spe analyze build/quote-study/cpu/timing.json
build/release/spe profile-diff build/quote-study/cpu/profiling.json --json -
```

The driver fixes one port, pins the client and application to separate CPUs, and predeclares balanced, seeded execution orders. Each process gets one untimed native SHA-256 response probe and 640 timed GET requests at concurrency one. The fixture independently counts completed responses. Its integer reference implementation supplies the expected HTTP entity. SPE records the result of the preflight. The process counter and native success count reconcile the timed traffic.

Eight unprofiled pairs per condition establish the mean-latency change using the existing exact median interval and a 10% meaningful-change threshold. Each confirmed regression receives four fresh baseline/candidate profile pairs. The reverse patch and new build happen after the ranking. Recovery requires an improvement beyond 10% in eight fresh candidate/revert pairs and an interval wholly inside ±10% in 16 fresh baseline/revert pairs. Independent processes, a fixed dataset and a single primary endpoint narrow the interpretation to this fixture and host.

## Interpret a profile

The sampler observes the application process between its preflight and last timed response. Initialization, native load-generator CPU and shutdown are outside the window. The application measures user CPU, system CPU, wall time, context switches and process peak RSS with a monotonic clock and `getrusage`. Perf uses a fixed-period userspace CPU-clock event and frame-pointer stacks. Gperftools uses its process CPU timer and symbolization against the exact ELF with GNU debug information. The requested 500 Hz may exceed the delivered interrupt rate. Exported gperftools stack weights must match delivered interrupts. Scheduling, coalesced interrupts, profiler overhead and sampling variance can affect attribution. Gperftools’ timer advances during system CPU as well, which can bias the allocation of user CPU in syscall-heavy workloads. Measured system CPU in the lookup experiment was 0.021 and 0.038 ms per success.

For each symbol and process, SPE multiplies measured **user CPU microseconds per successful request** by that symbol's share of all interrupted leaf samples. Unresolved and external leaves stay in the denominator. A caller in the stack helps locate the path. Only the interrupted leaf receives self cost. Resources and estimates are averaged across independent processes with equal weight. Wall time includes blocking. A CPU profile cannot name a database, socket or lock as the cause of a wait.

The advisory ranking requires a successful, compatible schema 7 primary regression, distinct native artifacts across phases, matching dataset, workload, engine, target version and verification policy, and successful receipt reconciliation. For this protocol each pair must exceed both 150 µs and 10% additional user CPU per success. Each window needs at least 25 samples, each arm at least 100, and at least half the leaves must resolve to application source in every window. A ranked symbol has at least 10 candidate leaf samples and a positive estimate in every pair. These are descriptive support checks. They do not estimate a false-discovery rate. Gperftools groups same-file symbols by its reported name, which can combine overloads. The supplied fixture has distinct business function names.

The bounded version-1 profiling manifest references the existing timing manifest, dataset SHA-256 and 2–8 ordered diagnostic pairs. A capture names its native result and exact file hash, ELF SHA-256 and GNU build ID, runtime ID, collector, requested frequency, resource counters and positive weighted leaf-first stacks indexed into a table of public symbol/file/line triples. Timing version IDs hash `ELF_SHA256/RUNTIME_ID` as ASCII. The timing workload ID is the dataset digest. The runner checks artifacts at execution time. Saved metadata relies on the collector's provenance. Native run schemas and the behavior of ordinary workload execution, comparison and paired analysis are preserved.
