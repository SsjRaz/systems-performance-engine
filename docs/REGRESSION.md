# Paired regression analysis

`spe analyze manifest.json --json report.json` analyzes one prespecified metric in an experiment with two arms. The command reports a regression, improvement or inconclusive result. These are advisory decisions. Both directional outcomes exit 0. Invalid designs or incompatible inputs exit 2. A well-formed experiment with unusable measurements or inadequate replication exits 3. `spe compare` continues to provide descriptive comparisons of existing saved runs.

## Declare the experiment before running it

Choose a workload, application versions, primary metric, smallest meaningful change, fixed number of pairs, and randomized, counterbalanced run order. Save the manifest before observing results. Each block contains one fresh baseline instance and one fresh candidate instance, listed in actual execution order. Run each instance once. Give each block and instance a unique public identifier. This example shows one block to illustrate the format.

```json
{
  "schema_version": 1,
  "experiment_id": "catalog-repeat-01",
  "workload_id": "catalog-filter-1000-v1",
  "baseline_version": "1.41.0",
  "candidate_version": "1.42.0",
  "metric": "mean_latency_ms",
  "meaningful_change_percent": 10,
  "private_inputs_confirmed": true,
  "blocks": [
    {"id": "pair-01", "runs": [
      {"arm":"candidate","instance":"server-01","result":"candidate-01.json"},
      {"arm":"baseline","instance":"server-02","result":"baseline-01.json"}
    ]}
  ]
}
```

Append the remaining blocks before measurement. Paths are relative to the manifest directory. Use content or release identifiers for the application versions and identify the dataset and workload through `workload_id`. Use `private_inputs_confirmed: true` only after independently arranging identical private request bodies and header values. Their contents are redacted in SPE results. The fixture checks retained private file hashes locally. The report records a hash of the canonical parsed manifest. It cannot establish when an operator wrote the plan, authenticate applications or prove fresh-process independence.

Each input must pass native workload, engine, accounting and completeness checks and have a successful schema 7 preflight with the same public policy ID. Both arms must use one origin. The analyzer rejects repeated server identifiers, file aliases and reserialized copies of the same result. It checks timestamp order at the native clock's one-second reporting resolution. Supply documented instance lifetimes and avoid overlapping runs, shared caches with order effects or unbalanced host load. Host and dataset confounding can still produce a statistically convincing difference.

## Effect and uncertainty

Select `mean_latency_ms`, `p95_latency_ms` (one percentile per server run) or `successful_rps`. A pair contributes one baseline-relative percentage, `100 × (candidate − baseline) / baseline`. Every pair has equal weight. The reported effect is their sample median. Positive latency changes and negative throughput changes indicate slower service.

The two-sided distribution-free interval inverts the sign test for the population median. For sorted changes and zero-based endpoint index k, coverage is at least `1 − 2 × P(Binomial(n, 0.5) ≤ k)` for the interval from element k through element n−1−k. SPE chooses the narrowest interval with at least 95% coverage and reports its achieved bound. This assumes independent pairs drawn from a stable population with a common median. Ties can increase coverage. No normal latency distribution or independence between individual requests is required. File count does not prove independence.

A directional verdict requires the entire interval strictly beyond the predeclared threshold in the same direction. The interval touching or crossing the meaningful-change band yields inconclusive. This includes very precise changes within the band. Failure to detect a material change leaves it unresolved. At least six independent pairs are needed for a finite 95% interval. Baseline-first and candidate-first counts must differ by at most one. The method supports up to 100 pairs within the 64 KiB manifest limit. Exploring other metrics, changing thresholds or stopping early needs a new validation design.

## Locked validation protocol

The Linux loopback harness writes all manifests, randomizes campaign order using seed 831947 and records the driver and engine hashes before the first request. Each main condition has eight independent decision campaigns with eight paired fresh starts each. The sole primary endpoint is mean successful latency with a 10% threshold. Each process serves one native JSON preflight plus 12 timed requests. Native run files, independent receipt counts and paired reports are written under the chosen new directory.

- Unchanged controls use a 1.5 ms sleep and 0.3 ms of thread CPU per request on both versions. A second unchanged condition shares the service CPU with a competing process in both arms.
- Known changes add 2.5 ms of thread CPU or 3 ms of response delay. The small-change condition adds 70 microseconds of sleep.
- Separate diagnostics change scheduling model or offered rate, induce capacity misses, return an incorrect HTTP 200 response, or supply four pairs.
- Application replays reuse PR #6’s pinned and verified fixtures with fresh processes. Meilisearch uses the 1,000-value search with 24 sequential timed POSTs. Gitea uses a 3:2 Git-object mix at 20 reads/s over 1.5 seconds. Application campaigns are transfer checks. The held-out fixture campaigns supply the false-alarm and detection counts.

```sh
python3 benchmarks/oss/detector_validation.py fixture --engine build/release/spe --output build/held-out
python3 benchmarks/oss/detector_validation.py apps --engine build/release/spe --output build/paired-apps \
  --tools build/oss/tools --meili build/oss/meili-1 --gitea build/oss/gitea-1
```

For the application command, use prepared Meilisearch and seeded Gitea directories from the [application instructions](../benchmarks/oss/README.md). Run builds and other experiments outside measurement windows. Use a second independently provisioned host and further held-out campaigns before choosing a CI gating policy.
