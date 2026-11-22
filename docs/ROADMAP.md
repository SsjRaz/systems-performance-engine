# Roadmap

The project is building toward reproducible comparisons of backend service versions. Native execution and descriptive saved-result comparison are implemented; reliable regression decisions require further measurement evidence.

| Milestone | Status | Outcome |
| --- | --- | --- |
| Native HTTP foundation | Implemented | Bounded C++20 HTTP runner, request accounting, JSON output, and automated tests. |
| Initial measurement validation | Completed locally | Identical-code controls, k6 comparisons, and injected application work exercised against Petclinic. |
| Linux verification and test CI | Implemented | Local Release and sanitizer tests passed on Ubuntu arm64. The [CI workflow](../.github/workflows/ci.yml) covers Ubuntu x86_64. |
| Measurement repeatability | In progress | Reproducible experiment setup, quantified variability, and clearer generator limits. |
| Native saved-result comparison | Implemented | Application-independent compatibility checks, descriptive differences, run variability, and versioned reports. |
| Secure HTTPS | Implemented | Verified certificates/hostnames, TLS 1.2 minimum, custom CA bundles, TLS failure accounting, and real TLS tests. |
| Configurable GET headers | Implemented | Private file input, strict field policy, redacted reports, explicit comparison acknowledgement, and authenticated HTTP/HTTPS tests. |
| Fixed-arrival-rate scheduling | Implemented | Monotonic deadlines, bounded admission, dispatch-delay measurements and explicit missed arrivals. |
| Two-endpoint GET workloads | Implemented | Reproducible weighted assignment on one origin with per-path outcomes, latency and overload accounting. |
| Bounded HTTP methods and request bodies | Implemented | HEAD and single-target writes with private payloads, explicit budgets for repeated writes, and wire-level tests. |
| Statistical comparison | Planned | Validated uncertainty methods and practical decision rules based on experimental runs or JVM blocks. |
| Workload and CI extensions | Under consideration | Broader HTTP workloads, automation, and result export. |

## Implemented foundation

- C++20/CMake CLI using libcurl for concurrent HTTP/1.1 requests over HTTP and HTTPS.
- Closed-loop and fixed-rate scheduling with bounded concurrency, timeouts, and response size.
- Complete request outcome accounting, interruption handling, and versioned JSON results.
- Native `spe compare` for one or more saved runs per version, with compatibility checks, failure diagnostics, and descriptive summaries.
- Unit tests, actual HTTP integration tests, and AddressSanitizer/UndefinedBehaviorSanitizer verification on macOS arm64.
- Petclinic A–B–B–A comparisons with preserved artifact identities, excluded warm-up, and descriptive reports.

The initial validation covered 56 fresh JVM blocks and 1.6 million measured successful requests. SPE and k6 observed substantial injected slowdowns; small changes remained unresolved under unchanged-code variation. Results and experimental limits are recorded in [measurement validation](VALIDATION.md#executed-local-timing-observations).

## Continuing work: measurement repeatability

This work addresses the gap between observing large changes and making reliable service-version comparisons.

Deliverables:

- Completed: a pinned Petclinic builder and checked-in patches, verified with a fresh checkout and bounded smoke traffic. Historical JARs are no longer required for basic validation.
- Documented warm-up and measurement-window criteria, with repeated unchanged-code controls.
- Experiments separating generator capacity, service behavior, and shared-host contention.
- Validation across additional endpoints and environments. Ubuntu arm64 native Release/sanitizer tests have passed; hosted x86_64 coverage is handled by CI. Linux application setup remains unverified.
- Recorded sensitivity to different change sizes, including conditions where results remain inconclusive.

Completion requires reproducible commands, retained evidence, and documented limits on interpretation. Automatic statistical verdicts remain outside this milestone.

## Implemented: native comparison

`spe compare` consumes supported native SPE results from any HTTP backend. It checks workload and engine compatibility, reports percentage differences and per-run variability, and suppresses performance deltas for failed or incomplete experiments. It produces terminal and versioned JSON reports without starting applications. Percentile summaries describe per-run percentiles rather than a pooled request distribution.

## Planned: statistical comparison

Uncertainty estimates and regression, improvement, or inconclusive outcomes require a method validated with unchanged controls and known changes. Experimental runs or JVM blocks must be the units of analysis; file count alone does not establish independence.

Statistical methods, practical thresholds, and minimum evidence requirements are not yet selected.

## Later capabilities

Further scheduler validation includes independent-host pacing and generator saturation measurements. Sanitized trace replay, CI performance-comparison reports, native profiling, and observability exports remain candidates. Priorities depend on measurement evidence and developer feedback. Automated correctness testing is already configured separately from performance comparisons.

Developer-demand validation is also outstanding. The technical experiments establish local feasibility, while adoption, repeat usage, and commercial value remain open research questions.

## Public release

The first public source release targets early development on macOS and Linux. Systems Performance Engine uses the Apache License 2.0. [Build instructions and scope](../README.md) · [License](../LICENSE)
