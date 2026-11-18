# Systems Performance Engine

Systems Performance Engine (SPE) is a C++20 CLI for measuring how a backend endpoint responds under concurrent load. Run the same GET workload before and after a service change, then use `spe compare` to inspect differences in throughput, latency, and request outcomes.

**Early development.** Comparisons are descriptive. In controlled Spring Petclinic experiments, SPE and k6 both observed the slowdown from an added 5 ms delay. For a change that removed a redundant owner lookup, variation between unchanged runs left the performance effect unresolved. [Experiment results and controls](docs/VALIDATION.md#executed-local-timing-observations)

## Implemented

- HTTP/1.1 GET over HTTP or verified HTTPS, with connection reuse, optional custom CA trust, and request headers from a private file. Header values are omitted from reports. The target URL, including its query, is recorded.
- Closed-loop execution with bounded concurrency, request counts, timeouts, and response sizes. Versioned JSON records completed requests, failures by category, cancellations, and requests that never started.
- Successful throughput and client-observed latency, including mean and nearest-rank p50, p95, and p99. Comparison accepts one or more compatible runs per version, reports run-to-run variation, and withholds performance deltas for failed or incomplete runs.

## Platforms

The native engine runs on macOS and Linux. Builds require CMake 3.24+, a C++20 compiler, and libcurl 7.85+ with asynchronous DNS. HTTPS requires a TLS-enabled libcurl. Linux also requires OpenSSL Crypto development files. Tests use Python 3.10+ and the OpenSSL CLI. CI covers Release and ASan/UBSan on Ubuntu x86-64. Windows is unsupported. [Build and platform details](docs/USAGE.md#build-and-test)

## Run and compare

From the repository root, build the CLI and start the included loopback HTTP service in one terminal.

```sh
cmake -S . -B build/release -DCMAKE_BUILD_TYPE=Release
cmake --build build/release --parallel 2
ctest --test-dir build/release --output-on-failure
python3 tests/http_server.py --port 8080
```

In a second terminal at the repository root, run two measurements against the same service. Each invocation of this example creates a fresh results directory.

```sh
results=$(mktemp -d build/example.XXXXXX)
for version in baseline candidate; do
  build/release/spe http://127.0.0.1:8080/ok \
    --requests 100 --concurrency 4 --timeout-ms 1000 \
    --json "$results/$version.json" || break
done
build/release/spe compare "$results/baseline.json" "$results/candidate.json"
```

This is an unchanged-service control. Both labels refer to the same code, so the observed difference illustrates run-to-run variation. To measure a code change, run the baseline, replace the service with the candidate at the same address, and repeat the workload. Stop the example service with Ctrl-C.

## Measurement limits and planned work

A closed-loop client sends the next request when a slot frees up. A slow response therefore reduces the offered load. Latency includes connection setup when needed and the time until the client observes completion. One run per version cannot establish repeatability, and SPE does not compute statistical significance.

Additional HTTP methods and request bodies, fixed-arrival-rate scheduling, trace replay, and statistical regression detection are future work. [Roadmap](docs/ROADMAP.md) · [Architecture](docs/ARCHITECTURE.md)

## License

Systems Performance Engine is licensed under the [Apache License 2.0](LICENSE). The vendored nlohmann/json header retains its [MIT license](third_party/nlohmann/LICENSE.MIT). The Spring Petclinic benchmark patches retain the [upstream attribution](benchmarks/petclinic/NOTICE.txt) and [Petclinic license](benchmarks/petclinic/PETCLINIC-LICENSE.txt).
