# Fixed-rate delivery on Linux

The experiment runs a Release build against the existing 16-byte HTTP timing fixture on loopback. Each trial starts fresh generator and fixture processes. The fixture records the monotonic time of each handler entry. Run from the repository root with two distinct CPUs from the process's affinity mask:

```sh
python3 scripts/measure_linux_pacing.py \
  --baseline build/release/spe --candidate build/release/spe \
  --client-cpu 1 --server-cpu 2 --output build/pacing-control
```

This unchanged-code control uses A–B–B–A order for each workload. For a change, supply the two saved Release binaries and add `--blocks 2`. The second block reverses both workload order and binary order. Run the experiment without builds, other benchmarks or tracing in progress. The Linux runner requires Python 3.10+, taskset and procfs. Each invocation creates a new directory with binary hashes, one native JSON report per trial and a CSV. It checks server receipts against completed successful requests, reconciles the arrival budget, and verifies the CLI exit status. A fixed-rate trial that misses arrivals returns 3.

## Baseline at fd03ae5

Linux x86-64 guest, kernel 6.18.57, GCC 14.2 Release, libcurl 8.14.1, Python 3.12.14. The VM exposed five CPUs and a four-CPU cgroup quota. SPE ran on CPU 1, the fixture on CPU 2. The cgroup recorded zero throttling periods through this sweep. Both processes shared a kernel and physical host. The Release binary's SHA-256 was `baf95f4f6bb2ac5d0ed1ef4c2e7101715301c1c0d53a2e9faf321cbcb0f47e1a`.

Four three-second windows per rate, concurrency 4. Rates and p95 columns are medians of per-run statistics. Receipt and miss counts are totals across the four runs. Dispatch rate uses the arrival window. Successful throughput also includes drain time. Dispatch delay covers only arrivals that started.

| Target/s | Dispatched/s | Successful/s | Server receipts | Missed late | At capacity | Dispatch p95 ms | Receipt gap p95 ms |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 100 | 100.0 | 100.0 | 1,200 | 0 | 0 | 1.043 | 10.419 |
| 500 | 498.8 | 498.7 | 5,980 | 14 | 6 | 1.038 | 2.337 |
| 1,000 | 923.8 | 923.6 | 11,089 | 872 | 39 | 0.952 | 1.348 |
| 2,000 | 1,086.2 | 1,085.9 | 13,004 | 10,915 | 81 | 0.467 | 1.303 |

At 2,000/s the identical A and B binaries achieved median success rates of 1,080.0 and 1,086.8/s. All four trials fell between 1,074.3 and 1,087.5/s. SPE used a median 6.6% of one CPU and the fixture used 9.0%. Their median observed high-water RSS values were 7.7 and 19.9 MiB. The CSV includes these measurements for every trial. Generator CPU includes startup and reporting. Fixture CPU includes the Python driver during the child lifetime. The driver samples the child's Linux VmHWM every 50 ms, so late RSS growth can be missed. Fixture RSS includes interpreter startup and receipt storage.

The closed-loop control completed all 4,096 requests in every trial. Median successful throughput at concurrency 1, 4 and 16 was 11,236, 14,250 and 16,423/s. The fixture used 63%, 91% and 95% of one CPU respectively. This gives the fixture headroom for the 2,000/s experiment. The high-concurrency closed-loop result measures the generator and fixture together.

## Wait diagnosis

A separate one-second baseline run at 2,000/s, concurrency 4, was traced with `strace -qq -yy -T -e trace=poll,ppoll,clock_nanosleep,nanosleep` on the SPE command. There were 691 timed-out polls watching only libcurl's eventfd. Each requested one millisecond, their median duration was 1.072 ms, and their recorded durations summed to 751 ms. The interarrival period is 0.5 ms. In the fixed-rate loop, the wait for the next arrival is rounded up to a whole millisecond even after the last transfer completes.

The trace identifies a concrete idle-wait cost. Tracing perturbs timing, so its delivery figures are excluded from the tables. VM scheduling and target delays can still contribute to late or capacity misses. Independent-host replication remains open.
