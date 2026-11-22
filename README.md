# Systems Performance Engine

Systems Performance Engine (SPE) is a C++20 command-line tool for running concurrent HTTP and HTTPS workloads and comparing backend service performance.

## Aim

Help backend developers understand how code changes affect service performance before those changes reach production. For example, after changing a database query, run the same endpoint workload against both versions and compare throughput, tail latency, and failures. Repeating the workload against unchanged code gives those differences context.

## Current capabilities

**Early development.** The native runner and saved-result comparison are implemented. Trace replay and statistical regression detection are planned.

- Run GET and HEAD with connection reuse. POST, PUT, PATCH, and DELETE use a fresh connection for each write and default to one request. POST, PUT, and PATCH accept a private binary payload up to 64 KiB. [HTTP methods and bodies](docs/USAGE.md#http-methods-and-request-bodies)
- Set a target arrival rate and duration with `--rate` and `--duration-ms`. The runner measures dispatch delay and counts arrivals missed under overload. [Fixed-rate scheduling](docs/USAGE.md#fixed-rate-scheduling)
- Run two weighted GET paths on one origin and see throughput, latency and missed arrivals for each path. [Endpoint mixes](docs/USAGE.md#two-endpoint-workloads)
- Test authenticated endpoints with verified HTTPS, optional custom CA trust, and headers loaded from a private file. Reports omit header values and retain the target URL, including its query.
- Record throughput, mean latency, and nearest-rank p50, p95, and p99 in versioned JSON. Request counts distinguish successes, failures, cancellations, and requests that never started.
- Compare one or more compatible saved runs per version with `spe compare`. It reports percentage changes and run-to-run variation. Failed or incomplete runs produce diagnostics without performance deltas.

## Platforms

| Platform | Verification |
| --- | --- |
| macOS arm64 | Built and tested locally with Apple Clang 16 and libcurl 8.7.1. |
| Linux arm64 | Release and ASan/UBSan tests passed on Ubuntu 24.04 in Docker. |
| Linux x86-64 | GitHub Actions runs Release and ASan/UBSan tests on Ubuntu 24.04. |
| Windows | Unsupported. |

Building requires a C++20 compiler, CMake 3.24+, and libcurl 7.85+ with asynchronous DNS. HTTPS requires TLS support in libcurl. Linux also requires OpenSSL Crypto development files. Tests use Python 3.10+ and the OpenSSL CLI.

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

## Interpreting results

The default closed-loop workload refills each client slot on completion, so the offered load falls as responses slow down. A fixed-rate workload schedules arrivals independently. Check delivered and missed counts before interpreting its latency percentiles. Latency is measured at the client and includes connection setup when needed.

In controlled Spring Petclinic experiments, SPE and k6 both observed a slowdown when the service added a 5 ms delay. Removing a redundant owner lookup produced a much smaller difference that unchanged-code variation could explain. SPE reports observed differences and leaves regression judgments to the reader. [Experiment results and controls](docs/VALIDATION.md#executed-local-timing-observations)

## Documentation

- Build options, authentication, CLI flags, and result fields — [Usage reference](docs/USAGE.md)
- Petclinic setup, k6 comparisons, and recorded findings — [Measurement validation](docs/VALIDATION.md)
- The event loop, connection handling, and measurement boundaries — [Architecture](docs/ARCHITECTURE.md)
- Work in progress and planned milestones — [Roadmap](docs/ROADMAP.md)

## License

Systems Performance Engine is licensed under the [Apache License 2.0](LICENSE). The vendored nlohmann/json header retains its [MIT license](third_party/nlohmann/LICENSE.MIT). The Spring Petclinic benchmark patches retain the [upstream attribution](benchmarks/petclinic/NOTICE.txt) and [Petclinic license](benchmarks/petclinic/PETCLINIC-LICENSE.txt).
