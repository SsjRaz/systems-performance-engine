# Compare two Git revisions

Run `spe study --baseline main --candidate HEAD --config spe.json` inside a trusted local application repository. SPE resolves both commits, builds each distinct revision once and measures a read-only HTTP GET on fresh application processes. The terminal report names both commits and gives the paired change, uncertainty interval and advisory verdict.

## Try a controlled regression

Paste this block from the SPE repository root with CMake, the SPE build dependencies, Git and Python 3.10+ on your path. It creates a disposable application repository. The second commit adds 12 ms to the lookup and returns the same JSON. The final command runs an unchanged-commit control.

```sh
(
  set -eu
  cmake -S . -B build/release -DCMAKE_BUILD_TYPE=Release
  cmake --build build/release --target spe --parallel 2
  spe="$PWD/build/release/spe"
  demo=$(mktemp -d)
  cp -R examples/git-study/. "$demo"
  cd "$demo"
  git init -q -b main
  git config user.name "SPE demo"
  git config user.email "demo@example.invalid"
  git add service spe.json
  git commit -qm "Baseline catalog lookup"
  git switch -qc slower
  printf 'DELAY_MS = 15\n' > service/cost.py
  git commit -qam "Add 12 ms to the catalog lookup"

  "$spe" study --baseline main --candidate HEAD --config spe.json --output changed
  "$spe" study --baseline HEAD --candidate HEAD --config spe.json --output control
)
```

The default example sends 24 timed GETs per fresh process. Across eight pairs, the delayed commit should show a regression. For the unchanged control an interval crossing the 10% threshold band gives an inconclusive verdict. Host load affects both measurements. [Example configuration](../examples/git-study/spe.json) · [Paired analysis method](REGRESSION.md)

## Configure your application

Pass one reviewed JSON file from your working copy. SPE reads it once before resolving the two revisions. Both revisions and their build systems must be trusted because build and startup programs run with your local account permissions. Commands are argument arrays passed directly to executables. Revision selection accepts local Git references and commit IDs.

| Field | Meaning |
| --- | --- |
| `schema_version`, `workload_id` | Use schema `1` and a public identifier such as `catalog-lookup-v1`. Change the identifier when your dataset, endpoint or response contract changes. |
| `build` | Ordered list of commands run in the checkout root. Use `[]` for an interpreted app with no build step. Optional `build_timeout_s` defaults to 600 per command. |
| `start` | One foreground command, also run in the checkout root. Bind the service to `127.0.0.1` and the port supplied through `{port}` in an argument or the `SPE_PORT` environment variable. Keep child workers in the foreground process group. |
| `port` | Optional fixed local port. By default the OS chooses a port and both arms use it throughout the study. |
| `readiness` | An object with a GET `path` such as `/ready` and optional `timeout_ms` (default 10000). A completed 2xx response makes the instance ready. |
| `endpoint` | The read-only GET path, including an optional query string. The path is included in saved results. |
| `expected_response` | One native verification rule, such as `{"fields":{"/answer":42}}` or `{"sha256":"<lowercase hex digest>"}`. SPE verifies an untimed GET before the timed requests for each instance. [Response policies](USAGE.md#untimed-response-verification) |
| `measurement` | Optional settings. Defaults are 8 pairs, 50 requests per process, concurrency 1, a 1000 ms request timeout, `mean_latency_ms` and a 10% meaningful change. |

Use `pairs` (6–100), `requests`, `concurrency`, `timeout_ms`, `metric` and `meaningful_change_percent` to adjust the measurement. The other supported metrics are `p95_latency_ms` and `successful_rps`. An optional unsigned 32-bit `seed` repeats the randomized, balanced arm order. The plan records the chosen order before either build starts. [Example with these field names](../examples/git-study/spe.json)

Each distinct commit gets a detached checkout in a temporary local clone. Each run gets a new process group and an empty directory in `SPE_INSTANCE_DIR` and `TMPDIR`. Keep the measured endpoint read-only and put transient service state there. Builds and versioned input files are shared by fresh starts of the same revision. Provision any external services and identical test data before the study.

## Read the results

SPE prints the absolute results directory at startup. Supply a new path under an existing directory with `--output` to keep it in a particular location. The default is a unique directory under the system temporary directory.

- `manifest.json` records the resolved commit IDs, metric, threshold and run order. Each run has its own native JSON result and application log. Build logs identify the commit.
- `report.txt` and `report.json` contain the completed paired analysis. To replay it, run `spe analyze /path/to/results/manifest.json`.
- `status.json` records the stage, measurement settings and completion state. A failed build, readiness timeout or failed measurement stops the study. Partial results and logs stay available for diagnosis. SPE releases its process groups and temporary checkouts on success, failure, Ctrl-C or SIGTERM.

A completed analysis exits 0 for regression, improvement or inconclusive. A failed response preflight exits 3 and skips timed requests for that instance. Build and startup failures exit 1. Invalid configuration exits 2. Interrupt and termination signals yield 130 and 143 respectively. The verdict applies to the configured workload and host.
