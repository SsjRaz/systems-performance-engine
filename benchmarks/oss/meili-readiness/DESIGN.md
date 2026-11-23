# Meilisearch upstream-readiness protocol

Compare the original v1.41.0, original v1.42.0, and PR #10's context-sharing v1.42.0. Reuse the SHA-256-pinned release-mode ELFs and deterministic two-index fixture from PR #10 on the second machine. Run the following comparisons sequentially. Each arm starts a fresh server. Keep the collector off for timing.

| Comparison | Pairs | Order seed | Primary outcome |
| --- | ---: | ---: | --- |
| Unchanged original v1.42.0 | 8 | 84101 | SPE mean latency |
| Original versus patched v1.42.0 | 12 | 84102 | SPE mean latency |
| v1.41.0 versus patched v1.42.0 | 12 | 84103 | SPE mean latency |

Use PR #10's 384 searches plus one identical preflight per window, balanced order, paired 95% interval and 10% practical floor. Preserve raw native records, application receipts, process CPU and high-water RSS. Compare hardware and ELF identities with the archived study. Run four additional balanced CPU-profile pairs only if the independent residual comparison meets SPE's regression gate. Keep those windows out of the timing comparison.

Try a locked build of all three arms with normal default features. If available, repeat all three comparisons with the same pair counts and new seeds 84201–84203. Record genuine data-asset provenance and build settings. Do not pool feature configurations or hosts.

On disposable indexes compare complete successful payloads and error responses across arms, with independent expected document IDs or error codes. Cover plain and federated search, boolean expressions, ranges, quoted and Unicode values, empty/short lists, differing request sizes and an unfilterable field. Check indexing task completion and retrieved corpus. Measure ordinary small searches and bulk indexing using eight balanced process pairs per comparison with the same application settings. Use paired intervals and a 10% practical floor. Treat process RSS as a lifetime high-water mark. Count requested bytes and allocation calls in a separate small parser ownership diagnostic. Keep instrumented phase breakdowns separate from timing. Preserve negative results and avoid drawing a causal conclusion from sample composition alone.

## Short-list follow-up

The isolated parser diagnostic observed higher requested bytes for empty and singleton lists. Before collecting default-feature timing, add a focused HTTP check of those two federated lists on the original catalog. Run eight unchanged-code pairs (seed 85230) and eight stock-versus-patched pairs (85231), 128 timed requests per workload per process, with one verified preflight each. Alternate the two workloads in the same seeded order within a pair. Use the same 10% floor and exact interval. Report these as follow-up comparisons.
