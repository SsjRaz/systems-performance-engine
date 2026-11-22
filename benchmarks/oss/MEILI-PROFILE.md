# Differential CPU study of Meilisearch federated search

The experiment uses the existing two-index catalog and its 1,000-value search. Each arm starts a fresh local server, reopens the indexed database for its version and handles 384 sequential timed requests. The application access log must contain 385 successful searches in the SPE invocation, including its identical preflight. Separate semantic probes check the 2,000 eligible documents and all 20 returned hits before and after the invocation. Primary timing uses uninstrumented processes. Four later balanced pairs use gperftools at 250 Hz.

## Source intervention design

The official release profiles implicated filter ownership alongside LMDB lookup, parser and allocator functions. The candidate source converts the parsed filter to owned form before passing local queries to a blocking worker. The experimental patch shares one immutable source context among borrowed values belonging to the same filter input. Tokens with different sources keep their own context, and the test covers modified fragments and error context after the original input is dropped. [Candidate source](https://github.com/meilisearch/meilisearch/blob/02ff9a4a1cae79ce405638b6323fbd2f939f7f67/crates/filter-parser/src/lib.rs) · [Experimental patch](meili-share-filter-context.patch)

The source comparisons use identical compiler options, per-version databases and release-study traffic. The prespecified primary metric is the median paired percentage change in mean request latency with a 10% meaningful-change threshold. Within each comparison half the pairs run in either order. App CPU divided by all 385 successful search receipts is a separate descriptive measure.

| Comparison | Baseline | Candidate | Fresh pairs | Order seed |
| --- | --- | --- | ---: | ---: |
| Source build control | 1.41.0 | 1.42.0 | 8 | 9147 |
| Intervention | 1.42.0 | 1.42.0 with shared filter context | 12 | 9427 |
| Residual difference | 1.41.0 | 1.42.0 with shared filter context | 12 | 9683 |

A lower latency for the changed 1.42.0 binary supports a contribution from the ownership path at this operating point. The third comparison measures any residual difference directly. Each comparison uses its full declared set of observations.
