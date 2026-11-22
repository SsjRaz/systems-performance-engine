# Paired detector validation on Linux

The native detector flagged every deliberately large CPU and delay injection in a held-out loopback fixture. None of the unchanged campaigns raised a regression alarm. Eight campaigns per condition on one shared Linux VM give limited precision for false alarms and detection rates. Automated regression gating remains disabled.

## Held-out controls

Commit `318a945` fixed the primary metric, 10% meaningful-change threshold, sample size, sign interval and campaign driver before collection. Each campaign used eight paired blocks, 16 fresh server processes, one separate preflight per process and 12 timed requests per process. The 40 decision campaigns were interleaved with five separate diagnostics in seeded random order. Each row shows regression decisions divided by decision campaigns. Effects are ranges across the eight campaign medians.

| Condition | Regression decisions | Median paired latency change across campaigns |
| --- | ---: | ---: |
| Identical service | 0 / 8 | −1.1% to +0.7% |
| Identical service under competing CPU load | 0 / 8 | −8.2% to +5.8% |
| Injected 2.5 ms thread CPU per request | 8 / 8 | +120.6% to +124.6% |
| Injected 3 ms response delay | 8 / 8 | +143.1% to +150.1% |
| Injected 70 µs response delay | 0 / 8 | +2.9% to +4.0% |

The small-change and unchanged campaigns all returned inconclusive. At eight pairs the two-sided sign interval spans the observed minimum and maximum pair effects. Its coverage bound under the model is 99.22%. Even treating successive campaigns as independent, 0/8 false alarms leaves a one-sided exact 95% upper bound of 31.2%. An 8/8 detection rate has a corresponding lower bound of 68.8%. More independent hosts and campaigns, particularly for effects close to the threshold, are needed to set a deployment policy. Counts from different conditions use separate denominators.

## Recorded failure behavior

Changing the arrival model or the offered rate rejected the comparison even when both sides delivered all requested work. In the capacity control the candidate missed 72 of 96 scheduled arrivals. In the incorrect-content control a wrong HTTP 200 stopped the candidate before all 96 timed requests. Four pairs produced a descriptive median and no finite 95% interval.

The original analyzer identified repeated empty timing records as copies when several failed probes shared a timestamp second. It withheld a verdict but obscured the reason. Commit `681ebec` reports the failed correctness probes and applies the copied-content check to successful measurements. Reanalysis preserved all 48 original fixture and application verdicts, intervals and effects. A fresh five-campaign diagnostic suite verified the corrected failure reason together with the two compatibility checks, capacity misses and insufficient replication. A final review added rejection of chronologically impossible overlapping measurement windows. The collected runs passed this check.

## Meilisearch and Gitea

The application replays reused PR #6’s local catalog and Git repositories, and started a fresh application for each arm of each block. The table shows the median of eight baseline-relative paired changes in mean successful latency. Intervals refer to variation between process pairs. Semantic preflights and independent fixture checks ran outside the timing window. All three applications/workload combinations ran on the same VM.

| Workload | Median paired change | Exact interval | Verdict |
| --- | ---: | ---: | --- |
| Meilisearch 1.41.0 → 1.42.0, 1,000 filter values | +132.1% | +116.3% to +153.7% | Regression |
| Meilisearch 1.41.0 on both sides | −4.3% | −49.4% to +7.6% | Inconclusive |
| Gitea 1.24.6 Git-object mix on both sides | +0.9% | −13.0% to +17.5% | Inconclusive |

For the version change, medians of the individual run means were 3.31 and 7.61 ms. The earlier independently indexed campaigns measured request-weighted mean increases of 50.9% and 63.2%. The new process-paired estimate confirms direction. The different magnitudes demonstrate its sensitivity to the run protocol and environment. The upstream ninefold observation, responsible Meilisearch function and possible Gitea pooling benefit remain unresolved. Running the historical schema 6 files through the strict analyzer withheld a verdict because they lack recorded native preflights. Their descriptive results remain usable. [Original experiments](RESULTS.md)

## Reproduction and verification

The held-out fixture recorded 712 native runs, 8,376 timed receipts and 712 probes. The real applications added 48 runs, 1,248 timed receipts and 64 probes. The five fresh diagnostic campaigns added 72 runs, 696 timed receipts and 72 probes. Across these collections all 11,168 server receipts reconciled. A standalone Python verifier checks the run and probe records and recomputes the interval ranks with integer binomial coefficients. It can additionally replay each manifest through the native engine. [Protocol and commands](../../docs/REGRESSION.md)

```sh
python3 benchmarks/oss/verify_detector.py --engine build/release/spe build/held-out build/paired-apps
```
