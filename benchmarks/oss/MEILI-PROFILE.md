# Differential CPU study of Meilisearch federated search

Sharing filter context reduced 1.42.0 mean search latency by **42.1%** in a controlled source build. The changed binary still ran **19.6% slower** than the matched 1.41.0 build. This identifies a material contributor to the regression and measures the remaining gap on this host.

The experiment uses the existing two-index catalog and its 1,000-value search. Each arm starts a fresh local server, reopens the indexed database for its version and handles 384 sequential timed requests. The application access log must contain 385 successful searches in the SPE invocation, including its identical preflight. Separate semantic probes check the 2,000 eligible documents and all 20 returned hits before and after the invocation. Primary timing uses uninstrumented processes. Four later balanced pairs use gperftools at 250 Hz.

The host was an x86-64 Linux guest with a four-CPU quota. SPE used CPU 0. Each server inherited affinity to CPUs 1 and 2 with two Rayon threads. The kernel denied the userspace perf CPU-clock event. The study used the gperftools process timer with libunwind and recorded 10 ms Linux process accounting ticks. Sampling started after the external warm-up probe and stopped before the final semantic check. One exporter calibration exposed a wide inode field in the mapping table. The four registered release profile pairs were then collected in full.

## Published releases

On this Linux VM the median of the eight per-process mean latencies was 3.26 ms on 1.41.0 and 7.37 ms on 1.42.0. SPE estimated a **108.9% median paired latency increase**. The exact interval ran from +80.8% to +201.9% with 99.2% coverage under the paired sign-test assumptions. The arm medians and the median of the within-pair changes summarize different quantities.

| Unprofiled measure | 1.41.0 arm median | 1.42.0 arm median | Median within-pair increase |
| --- | ---: | ---: | ---: |
| Mean timed latency | 3.26 ms | 7.37 ms | 3.68 ms |
| Application user CPU per successful search | 3.00 ms | 6.87 ms | 3.55 ms |
| Application system CPU per successful search | 0.14 ms | 0.36 ms | 0.22 ms |
| Wall time per successful search in the full invocation | 3.33 ms | 7.54 ms | 3.79 ms |

Every release pair used more application user CPU on 1.42.0. The increase ranged from 2.70 to 5.61 ms per search. This supports predominantly additional CPU work on this host. The process clock sums all application threads, and the external wall window includes SPE startup and reporting. Quantifying scheduler, lock or I/O waits would require separate instrumentation.

Four fresh diagnostic pairs produced 1,164 baseline and 2,214 candidate interrupts. The leading estimated self-cost increases were 156 µs per successful search for `mdb_node_search`, 117 µs for `mdb_page_search` and 108 µs for `filter_parser::IndexFilterCondition::into_owned`. The ownership function had 42 candidate leaf samples. In the retained four-frame prefixes, 349 candidate samples and zero baseline samples contained a filter-parser ownership conversion. Of those 349, 282 had an unresolved leaf. Caller presence supplies additional context about time spent beneath the conversion.

The published ELFs contain named functions but omit source line tables. Across the official captures 16.4% of baseline leaves and 36.6% of candidate leaves were unresolved or omitted from the bounded table. SPE keeps their weight in the denominator. The sampler uses a process CPU timer on a multithreaded server. Signal delivery, optimized inlining, shared library frames and per-thread scheduling limit the accuracy of individual self-cost estimates.

## Source-level evidence

The matched source builds reproduced the slowdown across eight unprofiled pairs, with a +91.9% median paired change and an interval of +74.3% to +167.4%. Four separate profile pairs recorded 1,317 baseline and 2,709 candidate interrupts. SPE ranked `mdb_node_search` first at an estimated +294 µs per success and `filter_parser::Token::into_owned` second at +96 µs. The latter had 38 candidate leaf samples, with a positive estimate of +72 to +132 µs in every pair.

The retained stack for the token conversion connects the leaf at line 216 to `IndexFilter::into_owned`, the local query partition at line 1123 and the federated search entry. These locations identify the source path exercised by the search. The direct self estimate excludes work sampled in callees and allocation routines. [Token conversion](https://github.com/meilisearch/meilisearch/blob/02ff9a4a1cae79ce405638b6323fbd2f939f7f67/crates/filter-parser/src/lib.rs#L216) · [Local query partition](https://github.com/meilisearch/meilisearch/blob/02ff9a4a1cae79ce405638b6323fbd2f939f7f67/crates/meilisearch/src/search/federated/perform.rs#L1123)

## Source intervention design

The official release profiles implicated filter ownership alongside LMDB lookup, parser and allocator functions. The candidate source converts the parsed filter to owned form before passing local queries to a blocking worker. The experimental patch shares one immutable source context among borrowed values belonging to the same filter input. Tokens with different sources keep their own context, and the test covers modified fragments and error context after the original input is dropped. [Candidate source](https://github.com/meilisearch/meilisearch/blob/02ff9a4a1cae79ce405638b6323fbd2f939f7f67/crates/filter-parser/src/lib.rs) · [Experimental patch](meili-share-filter-context.patch)

The source comparisons use identical compiler options, per-version databases and release-study traffic. The prespecified primary metric is the median paired percentage change in mean request latency with a 10% meaningful-change threshold. Within each comparison half the pairs run in either order. App CPU divided by all 385 successful search receipts is a separate descriptive measure.

| Comparison | Baseline | Candidate | Fresh pairs | Order seed |
| --- | --- | --- | ---: | ---: |
| Source build control | 1.41.0 | 1.42.0 | 8 | 9147 |
| Intervention | 1.42.0 | 1.42.0 with shared filter context | 12 | 9427 |
| Residual difference | 1.41.0 | 1.42.0 with shared filter context | 12 | 9683 |

## Intervention results

| Unprofiled comparison | Arm medians of mean latency | Median paired change | Exact interval | Arm medians of user CPU per successful search |
| --- | ---: | ---: | ---: | ---: |
| Source 1.41.0 → 1.42.0 | 3.46 → 6.32 ms | +91.9% | +74.3% to +167.4% | 3.06 → 5.92 ms |
| Source 1.42.0 → changed 1.42.0 | 6.58 → 3.90 ms | −42.1% | −49.5% to −38.2% | 6.16 → 3.53 ms |
| Source 1.41.0 → changed 1.42.0 | 3.15 → 3.77 ms | +19.6% | +13.7% to +28.2% | 2.94 → 3.44 ms |

The two 12-pair intervals have 96.1% coverage under the paired sign-test assumptions. Both the drop after the change and the residual slowdown exceeded the prespecified 10% threshold. The evidence supports ownership conversion as a contributing code path for this fixed workload. It leaves the cause of the residual open. The higher LMDB estimates remain candidates for investigation.

All 36,864 timed requests succeeded across 96 fresh measured processes. Each native preflight passed, and the application logs reconciled all 36,960 in-window searches. Untimed probes verified the returned documents. A separate fresh probe on each of the two official and three source-built executables produced the same ordered 20-hit JSON array and 2,000 eligible documents. All 12 tests in the patched filter-parser library passed, including the test for retained error context. The six saved SPE reports replayed, and all 7,404 raw samples re-exported to the recorded stacks across 16 captures.

The results cover one VM, two local indexes and one filter workload. The source intervention changes an owned representation in the parser. Other query shapes, default-feature builds of the patch and independent hosts still need validation before an upstream production recommendation.

The follow-up study repeats the ownership intervention on an Intel host with normal and reduced feature builds, checks additional workloads, and traces the upstream fix to its release. [Upstream-readiness study](meili-readiness/README.md)

## Reproduce the measurements

Use Linux x86-64 with three assignable CPUs, GNU binutils, gperftools libprofiler and the existing dataset preparation tools. Build SPE in Release mode and prepare the fixture as described in the OSS benchmark guide. Select the same SPE executable for every timing and diagnostic run within a comparison. Each timing command creates a new output directory. [OSS setup](README.md)

```bash
common=(--engine build/release/spe --tools build/oss/tools --fixture build/oss/meili-1)
study=build/meili-release-study
python3 benchmarks/oss/meili_profile.py timing "${common[@]}" \
    --output "$study" --name meili-release --pairs 8 --seed 56119
python3 benchmarks/oss/meili_profile.py profile "${common[@]}" \
    --output "$study" --library /usr/lib/x86_64-linux-gnu/libprofiler.so.0
build/release/spe profile-diff "$study/profile.json" --json "$study/diagnosis.json"
```

For the source controls, use separate clean checkouts of the two pinned upstream tags. The tested configuration used Rust 1.91.1, Cargo's locked release profile, line tables, retained symbols and frame pointers. The default build stopped when the optional Korean tokenizer dictionary download failed. All three source binaries used the same reduced feature set, which omits the optional dashboard and language tokenizers. The official release comparison used the published binaries. [1.41.0 source](https://github.com/meilisearch/meilisearch/tree/8dd5a9511e7757b72883c07efba34c05213f78f6) · [1.42.0 source](https://github.com/meilisearch/meilisearch/tree/02ff9a4a1cae79ce405638b6323fbd2f939f7f67)

Run these commands from each checkout with the pinned toolchain on `PATH`. Set a common absolute Cargo target directory to reuse dependencies. Save the executable after each build. For the third build, use the same 1.42.0 checkout, apply the experimental patch, and run the same build command.

```bash
export CARGO_PROFILE_RELEASE_DEBUG=1 CARGO_PROFILE_RELEASE_STRIP=none
export RUSTFLAGS=-Cforce-frame-pointers=yes
cargo build --locked --no-default-features --release -p meilisearch --bin meilisearch
# Run in the changed checkout to check parser and error-context behavior.
cargo test --locked --release -p filter-parser --lib
```

For each source comparison supply an `--arms` JSON file with `baseline` and `candidate` objects. Each object contains `path` to the saved executable, its `version` string, and its lowercase `sha256`. Use the pair counts and seeds in the design table. The profile phase accepts the same arms file and uses newly started processes. Keep compilation and other benchmarks outside the measurement windows.

## Replay the evidence

The archive includes native schema 7 results, whole-process counters and route receipt counts, normalized weighted stacks, ELF-relative PCs, build identities, paired plans and original reports. The portable PCs preserve the interrupted leaf and up to three real callers. PCs outside the measured ELF are represented by null. The capture also records the hash of the original gperftools file. [Linux evidence](evidence/meili-profile-linux-x86_64.tar.gz)

```bash
mkdir -p build/meili-replay
tar -xzf benchmarks/oss/evidence/meili-profile-linux-x86_64.tar.gz -C build/meili-replay
(cd build/meili-replay/meili-study && sha256sum -c SHA256SUMS)
python3 benchmarks/oss/meili_replay.py build/meili-replay/meili-study \
    --engine build/release/spe
```

Supply repeatable `--elf PATH` arguments to re-symbolize the recorded PCs against exact measured binaries. An ELF hash identifies the appropriate windows. Binutils versions can affect demangled names and source locations. Fresh source builds can have different build IDs and hashes.
