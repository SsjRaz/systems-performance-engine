# Differential CPU diagnosis

SPE can compare sampled application CPU costs after its paired detector confirms an independent latency or throughput regression. The opt-in Linux study serves freight quotes over HTTP. All three builds calculate the same response over the same 32,768 parcels. The candidate scans a zone-rate table on every lookup. The baseline and reverted builds index it directly. A separate control adds a 2 ms sleep to the baseline executable.

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

The sampler observes the application process between its preflight and last timed response. Initialization, native load-generator CPU and shutdown are outside the window. The application measures user CPU, system CPU, wall time, context switches and process peak RSS with a monotonic clock and `getrusage`. Perf uses a fixed-period userspace CPU-clock event and frame-pointer stacks. Gperftools uses its process CPU timer and symbolization against the exact ELF with GNU debug information. The requested 500 Hz may exceed the delivered interrupt rate. Exported gperftools stack weights must match delivered interrupts. Scheduling, coalesced interrupts, profiler overhead and sampling variance can affect attribution.

For each symbol and process, SPE multiplies measured **user CPU microseconds per successful request** by that symbol's share of all interrupted leaf samples. Unresolved and external leaves stay in the denominator. A caller in the stack helps locate the path. Only the interrupted leaf receives self cost. Resources and estimates are averaged across independent processes with equal weight. Wall time includes blocking. A CPU profile cannot name a database, socket or lock as the cause of a wait.

The advisory ranking requires a successful, compatible schema 7 primary regression, distinct native artifacts across phases, matching dataset, workload, engine, target version and verification policy, and successful receipt reconciliation. For this protocol each pair must exceed both 150 µs and 10% additional user CPU per success. Each window needs at least 25 samples, each arm at least 100, and at least half the leaves must resolve to application source in every window. A ranked symbol has at least 10 candidate leaf samples and a positive estimate in every pair. These are descriptive support checks. They do not estimate a false-discovery rate. Gperftools groups same-file symbols by its reported name, which can combine overloads. The supplied fixture has distinct business function names.

The bounded version-1 profiling manifest references the existing timing manifest, dataset SHA-256 and 2–8 ordered diagnostic pairs. A capture names its native result and exact file hash, ELF SHA-256 and GNU build ID, runtime ID, collector, requested frequency, resource counters and positive weighted leaf-first stacks indexed into a table of public symbol/file/line triples. Timing version IDs hash `ELF_SHA256/RUNTIME_ID` as ASCII. The timing workload ID is the dataset digest. The runner checks artifacts at execution time. Saved metadata relies on the collector's provenance. Native run schemas and the behavior of ordinary workload execution, comparison and paired analysis are preserved.
