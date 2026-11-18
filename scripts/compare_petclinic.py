"""Run a local Petclinic A-B-B-A experiment using preserved, verified JARs.

No arguments checks the inputs and prints the protocol. --run executes it.
--smoke performs a short lifecycle check, never a performance comparison.
"""

import argparse
import contextlib
import datetime
import fcntl
import hashlib
import json
import math
import os
import platform
import shutil
import signal
import statistics
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

import reference_k6
from experiment_diagnostics import Diagnostics, jvm_flags

ROOT = Path(__file__).resolve().parents[1]
ORDER = ("baseline", "candidate", "candidate", "baseline")
CONTROLS = {'control': (0, 0), 'delay': (5_000_000, 0),
            'cpu-small': (0, 10_000), 'cpu-large': (0, 1_000_000)}
CLIENT = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest() if sys.version_info >= (3, 11) else hash_stream(stream)


def hash_stream(stream):
    result = hashlib.sha256()
    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
        result.update(chunk)
    return result.hexdigest()


def save(path, data):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, indent=2, allow_nan=False) + "\n")
    temporary.replace(path)


@contextlib.contextmanager
def exclusive_run(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError("Another comparison runner is active. Let it finish first.") from error
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def pids(command):
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode not in (0, 1) or (result.returncode == 1 and result.stderr.strip()):
        raise RuntimeError(f"Cannot inspect processes with {command[0]}; refusing to proceed")
    return set(result.stdout.split())


def engine_idle(engine):
    active = set()
    for name in {engine.name, 'spe', 'k6'}:
        active |= pids(["pgrep", "-x", name])
    if active:
        raise RuntimeError(f"Load generator already active (PIDs {', '.join(sorted(active))}). Let it finish first.")


def managed_record(petclinic):
    path = petclinic / 'target/local-run/run.json'
    return json.loads(path.read_text()) if path.exists() else {}


def recorded_java(metadata):
    # New builds record a tool path rather than inventing an earlier server run.
    return Path(metadata['java_executable'] if 'java_executable' in metadata else metadata['command'][0])


def managed_listener(port, metadata):
    listeners = pids(["lsof", "-nP", "-t", f"-iTCP:{port}", "-sTCP:LISTEN"])
    if not listeners:
        return None
    pid = str(metadata.get("pid"))
    if listeners != {pid} or urlsplit(metadata.get("url", "")).port != port:
        raise RuntimeError(f"Port {port} is occupied by an unrecognized process; nothing was stopped")
    expected = metadata["command"][metadata["command"].index("-jar") + 1]
    actual = subprocess.check_output(["ps", "-ww", "-p", pid, "-o", "args="], text=True)
    if expected not in actual or "java" not in actual:
        raise RuntimeError("The recorded server PID no longer matches its JAR; nothing was stopped")
    return int(pid)


def stop_managed(pid, port):
    os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + 20
    while pids(["lsof", "-nP", "-t", f"-iTCP:{port}", "-sTCP:LISTEN"]):
        if time.monotonic() > deadline:
            raise RuntimeError("Existing service did not release the port; refusing to launch a duplicate")
        time.sleep(0.25)
    # Also wait for the old JVM to exit, not merely for its listener to close.
    while True:
        status = subprocess.run(["ps", "-p", str(pid), "-o", "stat="], capture_output=True, text=True)
        if status.returncode == 1 or status.stdout.strip().startswith("Z"):
            return
        if status.returncode != 0 or time.monotonic() > deadline:
            raise RuntimeError("Cannot confirm the old JVM exited; refusing to overlap servers")
        time.sleep(0.25)


def stop_child(process, interrupt=False):
    if process is None or process.poll() is not None:
        return
    process.send_signal(signal.SIGINT if interrupt else signal.SIGTERM)
    try:
        process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def get(url):
    with CLIENT.open(url, timeout=2) as response:
        if response.status != 200:
            raise RuntimeError(f"Unexpected HTTP status from {url}")
        return response.read()


def ready(process, base, reference):
    deadline = time.monotonic() + 60
    while process.poll() is None:
        try:
            if json.loads(get(base + "/actuator/health")).get("status") == "UP":
                if get(base + "/owners/1") != reference:
                    raise RuntimeError("Owner page differs from the preserved baseline; refusing the comparison")
                return
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            pass
        if time.monotonic() > deadline:
            break
        time.sleep(0.5)
    raise RuntimeError("Petclinic failed readiness checks; inspect this block's server.log")


def validate_result(result, requests, concurrency, timeout_ms, url):
    c, w = result["counts"], result["workload"]
    if result["schema_version"] == 3 and w.get("headers") != {"names": [], "values": "redacted"}:
        raise RuntimeError("The Petclinic helper does not support redacted custom-header workloads; use native compare with explicit acknowledgement")
    if (w["url"], w["requests"], w["concurrency"], w["timeout_ms"]) != (url, requests, concurrency, timeout_ms):
        raise RuntimeError("Result workload does not match the experiment protocol")
    if any(c[key] != requests for key in ("requested", "started", "completed", "successes")):
        raise RuntimeError("Not every requested response succeeded; partial results have been preserved")
    if c.get("tls_errors", 0) or any(c[key] for key in ("http_errors", "connection_errors", "network_errors", "timeouts",
                              "response_limit_errors", "cancelled", "not_started")):
        raise RuntimeError("Errors or incomplete requests were recorded")
    if result["interrupted"] or result["engine_error"] is not None:
        raise RuntimeError("Interrupted run or engine error")
    if result["http_status_counts"] != {"200": requests}:
        raise RuntimeError("Unexpected response status distribution")
    if result["success_latency_ms"]["samples"] != requests or not 0 < result["peak_in_flight"] <= min(concurrency, requests):
        raise RuntimeError("Invalid sample count or concurrency accounting")
    elapsed = result["elapsed_seconds"]
    if not math.isfinite(elapsed) or elapsed <= 0:
        raise RuntimeError("Invalid elapsed time")
    for key in ("successful_throughput_rps", "completed_throughput_rps"):
        if not math.isclose(result[key], requests / elapsed, rel_tol=1e-9):
            raise RuntimeError("Throughput does not match completed requests and elapsed time")
    for key in ("min", "mean", "p50", "p95", "p99", "max"):
        value = result["success_latency_ms"][key]
        if not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
            raise RuntimeError("Invalid latency observation")
    latency = result["success_latency_ms"]
    ordered = [latency[key] for key in ("min", "p50", "p95", "p99", "max")]
    if ordered != sorted(ordered) or not latency["min"] <= latency["mean"] <= latency["max"]:
        raise RuntimeError("Inconsistent latency summary")
    if requests * latency['mean'] / 1000 > concurrency * elapsed + 0.001:
        raise RuntimeError('Summed request time exceeds the configured concurrency window')


def workload(engine, url, count, prefix, environment, protocol):
    engine_idle(engine)
    if protocol.get('tool') == 'k6':
        result = reference_k6.run(engine, url, count, protocol['concurrency'], protocol['timeout_ms'], prefix, environment)
        validate_result(result, count, protocol['concurrency'], protocol['timeout_ms'], url)
        engine_idle(engine)
        return result
    for suffix in (".json", ".stdout", ".stderr", ".command.json", '.process.json'):
        if prefix.with_suffix(suffix).exists():
            raise RuntimeError(f"Output already exists: {prefix}; refusing to overwrite it")
    command = [str(engine), url, "--requests", str(count), "--concurrency", str(protocol["concurrency"]),
               "--timeout-ms", str(protocol["timeout_ms"]), "--json", str(prefix.with_suffix(".json"))]
    save(prefix.with_suffix(".command.json"), command)
    with prefix.with_suffix(".stdout").open("x") as out, prefix.with_suffix(".stderr").open("x") as err:
        started_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
        started_clock = time.monotonic()
        process = subprocess.Popen(command, stdout=out, stderr=err, env=environment, start_new_session=True)
        try:
            code = process.wait(timeout=180)
        finally:
            stop_child(process, interrupt=True)
            save(prefix.with_suffix('.process.json'), dict(started_at_utc=started_at,
                finished_at_utc=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                process_wall_seconds=time.monotonic()-started_clock, returncode=process.returncode,
                note='Outer process interval includes initialization and result aggregation; not the active request window.'))
    if code != 0:
        raise RuntimeError(f"Engine exited with {code}; inspect {prefix}.stderr and its JSON")
    result = json.loads(prefix.with_suffix(".json").read_text())
    validate_result(result, count, protocol["concurrency"], protocol["timeout_ms"], url)
    engine_idle(engine)
    return result


def describe_block(number, variant, results):
    elapsed = sum(r["elapsed_seconds"] for r in results)
    return dict(block=number, variant=variant, measured_runs=len(results),
                combined_successful_throughput_rps=sum(r["counts"]["successes"] for r in results) / elapsed,
                mean_request_latency_ms=statistics.mean(r["success_latency_ms"]["mean"] for r in results),
                per_run_p99_ms=[r["success_latency_ms"]["p99"] for r in results])


def percentage_change(baseline, candidate):
    return 100 * (candidate / baseline - 1) if baseline != 0 else None


def compare_results(directory):
    """Revalidate recorded inputs before computing descriptive changes, never p-values."""
    report = dict(schema_version=1, status="invalid", statistical_significance="not_assessed",
                  checks={}, metrics={}, blocks=[], input_sha256={},
                  notes=["Warm-ups are excluded from the comparison.",
                         "Run percentiles are summarized as medians; they are not pooled request percentiles.",
                         "The two fresh JVM blocks per version are the replication units; individual requests are not independent experiments.",
                         "Matching recorded settings does not establish identical background load or thermal conditions."])
    stage = "experiment_complete"
    def read_input(path):
        raw = path.read_bytes()
        try:
            value = json.loads(raw)
        except ValueError as error:
            raise RuntimeError(f"Invalid JSON in {path.relative_to(directory)}") from error
        report["input_sha256"][str(path.relative_to(directory))] = hashlib.sha256(raw).hexdigest()
        return value

    try:
        manifest_path = directory / "manifest.json"
        manifest = read_input(manifest_path)
        report["scope"] = manifest["scope"]
        if manifest["status"] != "complete":
            raise RuntimeError(f"Experiment is {manifest['status']}; no complete comparison is available")
        report["checks"][stage] = "pass"
        if manifest.get('diagnostic_only'):
            report.update(status='diagnostic_only', interpretation='Instrumented experiment: diagnostic evidence only, no performance comparison.')
            return report

        stage = "complete_balanced_block_order"
        if [(b["block"], b["variant"]) for b in manifest["blocks"]] != list(enumerate(ORDER, 1)):
            raise RuntimeError("Expected four completed blocks in baseline-candidate-candidate-baseline order")
        protocol = manifest["protocol"]
        runs_per_block = protocol["measured_runs_per_block"]
        if not isinstance(runs_per_block, int) or not 1 <= runs_per_block <= 100:
            raise RuntimeError("Invalid measured run budget")
        if any(b["measured_runs"] != runs_per_block for b in manifest["blocks"]):
            raise RuntimeError("A block did not complete its measured run budget")
        report["checks"][stage] = "pass"

        stage = "recorded_artifact_identity_consistent"
        for identifier in [manifest["engine_sha256"], *manifest["jar_sha256"].values()]:
            if len(identifier) != 64:
                raise RuntimeError("Missing or invalid binary fingerprint")
            int(identifier, 16)
        for variant in set(ORDER):
            if manifest["jar_sha256"][variant] != manifest["source_metadata"][variant]["jar_sha256"]:
                raise RuntimeError(f"The recorded {variant} artifact identifiers disagree")
        if protocol.get('same_artifact'):
            if not manifest.get('same_artifact_verified') or len(set(manifest['jar_sha256'].values())) != 1:
                raise RuntimeError('Identical-artifact control has different JAR fingerprints')
            settings = protocol.get('variant_settings')
            if settings and settings['baseline'] != settings['candidate']:
                raise RuntimeError('Identical-code control has different application settings')
        report["checks"][stage] = "pass"

        stage = "expected_files_readable"
        commands, measured, warmups = [], [], []
        for number, variant in enumerate(ORDER, 1):
            block = directory / f"{number:02d}-{variant}"
            command_path = block / "server-command.json"
            commands.append(read_input(command_path))
            measured.append([read_input(block / f"run-{run:02d}.json") for run in range(1, runs_per_block + 1)])
            warmups.append(read_input(block / "warmup.json"))
        report["checks"][stage] = "pass"

        stage = "recorded_jvm_settings_match"
        common_command = None
        for variant, command in zip(ORDER, commands):
            jar_index = command.index("-jar") + 1
            expected_jar = manifest.get('artifact_paths', {}).get(variant, f'{variant}.jar')
            if (Path(command[jar_index]) != Path(expected_jar) if 'artifact_paths' in manifest
                    else Path(command[jar_index]).name != Path(expected_jar).name):
                raise RuntimeError("Server command does not identify the expected variant")
            command[jar_index] = "<variant.jar>"
            if common_command is None:
                common_command = command
            if command != common_command or any(f"-Xm{kind}{protocol['heap_mb']}m" not in command for kind in ("s", "x")):
                raise RuntimeError("JVM settings differ between blocks or disagree with the recorded heap")
        report["checks"][stage] = "pass"

        stage = "workload_and_engine_build_match"
        reference = measured[0][0]
        workload_settings = reference["workload"]
        for result in [r for block in measured for r in block]:
            if result["schema_version"] not in (1, 2, 3) or result["schema_version"] != reference["schema_version"] or result["workload"] != workload_settings or result["engine"] != reference["engine"]:
                raise RuntimeError("Measured workloads or engine builds differ")
        for result in warmups:
            expected = dict(workload_settings, requests=protocol["warmup_requests"])
            if result["schema_version"] not in (1, 2, 3) or result["schema_version"] != reference["schema_version"] or result["workload"] != expected or result["engine"] != reference["engine"]:
                raise RuntimeError("Warm-up workloads or engine builds differ")
        report["checks"][stage] = "pass"

        stage = "requests_and_measurements_valid"
        for result in [r for block in measured for r in block]:
            validate_result(result, protocol["measured_requests"], protocol["concurrency"], protocol["timeout_ms"], workload_settings["url"])
        for result in warmups:
            validate_result(result, protocol["warmup_requests"], protocol["concurrency"], protocol["timeout_ms"], workload_settings["url"])
        report["checks"][stage] = "pass"
        scenario = protocol.get('scenario', 'existing')
        if scenario != 'existing':
            stage = 'controlled_application_work_verified'
            if scenario not in CONTROLS or len(set(manifest['jar_sha256'].values())) != 1:
                raise RuntimeError('Controlled workloads must use one verified validation JAR')
            for number, variant in enumerate(ORDER, 1):
                block = directory / f'{number:02d}-{variant}'
                delay, iterations = (0, 0) if variant == 'baseline' else CONTROLS[scenario]
                if tuple(protocol['variant_settings'][variant]) != (delay, iterations):
                    raise RuntimeError('Recorded application dose differs from the selected scenario')
                applied = read_input(block / 'applied-controls.json')
                if (applied['delay_nanos'], applied['cpu_iterations']) != (delay, iterations):
                    raise RuntimeError('Application did not acknowledge the configured dose')
                conditioning = read_input(block / 'conditioning.json')
                validate_result(conditioning, protocol['conditioning_requests'], protocol['concurrency'],
                                protocol['timeout_ms'], workload_settings['url'])
                if conditioning['engine'] != reference['engine']:
                    raise RuntimeError('Conditioning used a different workload engine')
                for run in range(1, runs_per_block + 1):
                    stats = read_input(block / f'run-{run:02d}.server-stats.json')
                    n = protocol['measured_requests']
                    expected = dict(requests=n, delayCalls=n if delay else 0, cpuCalls=n if iterations else 0,
                                    cpuIterationsExecuted=n * iterations)
                    actual = {key: stats['after'][key] - stats['before'][key] for key in expected}
                    if actual != expected or stats['deltas'] != actual or stats['expected'] != expected:
                        raise RuntimeError('Application execution counters do not match measured traffic')
                    for point in ('before', 'after'):
                        if (stats[point]['delay_nanos'], stats[point]['cpu_iterations']) != (delay, iterations):
                            raise RuntimeError('Application settings changed during measured traffic')
                    if iterations and stats['after']['checksum'] == 0:
                        raise RuntimeError('No observable CPU-work result')
            report['checks'][stage] = 'pass'
        report["blocks"] = [describe_block(i, variant, measured[i - 1]) for i, variant in enumerate(ORDER, 1)]
        report["protocol"] = protocol
        report['measurement_definition'] = reference['engine'].get('latency_definition', 'SPE monotonic request start through completion observation, including connection work')
        if manifest["scope"] == "runner lifecycle verification only":
            report.update(status="functional_check_only", interpretation="Tiny lifecycle-check workloads cannot support a performance comparison.")
            return report

        grouped = {variant: [r for i, block in enumerate(measured) if ORDER[i] == variant for r in block]
                   for variant in set(ORDER)}
        for variant, results in grouped.items():
            report.setdefault("totals", {})[variant] = dict(measured_runs=len(results),
                successful_requests=sum(r["counts"]["successes"] for r in results),
                measured_seconds=sum(r["elapsed_seconds"] for r in results))
        getters = {
            "successful_throughput_rps": lambda runs: sum(r["counts"]["successes"] for r in runs) / sum(r["elapsed_seconds"] for r in runs),
            "mean_request_latency_ms": lambda runs: sum(r["success_latency_ms"]["mean"] * r["success_latency_ms"]["samples"] for r in runs) / sum(r["success_latency_ms"]["samples"] for r in runs),
        }
        for percentile in ("p50", "p95", "p99"):
            getters[f"median_run_{percentile}_ms"] = lambda runs, p=percentile: statistics.median(r["success_latency_ms"][p] for r in runs)
        for metric, getter in getters.items():
            baseline, candidate = getter(grouped["baseline"]), getter(grouped["candidate"])
            report["metrics"][metric] = dict(baseline=baseline, candidate=candidate,
                absolute_change=candidate - baseline, change_percent=percentage_change(baseline, candidate),
                favorable_direction="higher" if metric == "successful_throughput_rps" else "lower")
        report.update(status="descriptive_comparison", interpretation="Observed differences only; this report does not establish a regression or improvement beyond noise.")
    except (OSError, ValueError, KeyError, IndexError, TypeError, RuntimeError, ZeroDivisionError) as error:
        report["checks"][stage] = "fail"
        report["error"] = str(error)
        report["metrics"] = {}
    return report


def write_comparison(directory):
    report = compare_results(directory)
    save(directory / "comparison.json", report)
    lines = ["COMPARISON CHECKS", *(f"  {name}: {status.upper()}" for name, status in report["checks"].items())]
    if report["status"] == "invalid":
        lines.append("Comparison unavailable: " + report["error"])
    else:
        lines.append(report["interpretation"])
        if report.get('measurement_definition'): lines.append('Latency definition: '+report['measurement_definition'])
        if report["metrics"]:
            lines.extend(["", "Metric                         Baseline    Candidate    Change"])
            for name, metric in report["metrics"].items():
                delta = "n/a" if metric["change_percent"] is None else f"{metric['change_percent']:+.2f}%"
                lines.append(f"{name:30} {metric['baseline']:10.3f} {metric['candidate']:12.3f} {delta:>9}")
            lines.extend(["", "Throughput uses total successful requests / total measured seconds.",
                          "Mean latency is weighted by successful request count.",
                          "Run p50/p95/p99 medians are not pooled request percentiles.",
                          "Block throughput (fresh JVMs; inspect within-version differences):"])
            for block in report["blocks"]:
                lines.append(f"  {block['block']} {block['variant']}: {block['combined_successful_throughput_rps']:.3f} req/s")
            lines.append("Statistical significance: NOT ASSESSED. Environment drift can remain.")
    text = "\n".join(lines) + "\n"
    (directory / "comparison.txt").write_text(text)
    print(text, flush=True)
    return report


def prepare(args):
    if args.same_artifact and args.scenario not in ('existing','control'):
        raise RuntimeError('A/A mode requires identical application settings; omit --same-artifact for injected changes')
    engine = (args.k6 if args.tool == 'k6' else args.engine).resolve()
    petclinic = args.petclinic.resolve()
    source = petclinic / "target/local-run/owner-reuse"
    for tool in ("pgrep", "lsof", "ps"):
        if shutil.which(tool) is None:
            raise RuntimeError(f"Required POSIX tool is missing: {tool}")
    if not engine.is_file() or not os.access(engine, os.X_OK):
        raise RuntimeError("Build the Release engine first")
    metadata = {v: json.loads((source / f"{v}-run.json").read_text()) for v in set(ORDER)}
    jars = {v: source / f"{v}.jar" for v in set(ORDER)}
    hashes = {v: digest(jars[v]) for v in jars}
    for variant in jars:
        if hashes[variant] != metadata[variant]["jar_sha256"]:
            raise RuntimeError(f"The preserved {variant} JAR changed; refusing to use it")
    if args.scenario != 'existing':
        source = petclinic / 'target/local-run/measurement-validation'
        control_metadata = json.loads((source / 'build.json').read_text())
        jars = {v: source / 'validation.jar' for v in ORDER}
        metadata = {v: dict(control_metadata) for v in ORDER}
        hashes = {v: digest(jars[v]) for v in ORDER}
        if any(h != control_metadata['jar_sha256'] for h in hashes.values()):
            raise RuntimeError('Validation JAR fingerprint changed')
    if args.same_artifact:
        selected = args.same_artifact
        jars['candidate'] = jars['baseline'] = jars[selected]
        metadata['candidate'] = metadata['baseline'] = metadata[selected]
        hashes['candidate'] = hashes['baseline'] = digest(jars[selected])
        if hashes['baseline'] != hashes['candidate']: raise RuntimeError('A/A artifact identity failed')
    if metadata["baseline"]["java_version"] != metadata["candidate"]["java_version"]:
        raise RuntimeError("Preserved builds used different Java versions")
    java = recorded_java(metadata['baseline'])
    if not java.is_file():
        raise RuntimeError("The recorded Java executable no longer exists")
    engine_idle(engine)
    protocol = dict(order=ORDER, warmup_requests=8 if args.smoke else args.warmup_requests,
                    measured_requests=20 if args.smoke else args.requests, measured_runs_per_block=1 if args.smoke else args.runs,
                    concurrency=4, timeout_ms=2000, heap_mb=512, rest_between_blocks_seconds=0 if args.smoke else 5,
                    tool=args.tool, scenario=args.scenario, same_artifact=args.same_artifact,
                    diagnostics=args.diagnostics, conditioning_requests=8 if args.smoke else args.conditioning_requests,
                    variant_settings={'baseline':(0,0),'candidate':CONTROLS.get(args.scenario,(0,0))})
    return engine, petclinic, source, metadata, jars, hashes, java, protocol


def execute(args, inputs):
    engine, petclinic, source, metadata, jars, hashes, java, protocol = inputs
    current_path = petclinic / "target/local-run/run.json"
    current = managed_record(petclinic)
    existing_pid = managed_listener(args.port, current)
    args.output_root.mkdir(parents=True, exist_ok=True)
    prefix = "petclinic-abba-" + datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ-")
    output = Path(tempfile.mkdtemp(prefix=prefix, dir=args.output_root)).resolve()
    environment = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C", "LC_ALL": "C"}
    # Avoid inherited JVM instrumentation, Spring profiles, external config, and proxies.
    java_version = subprocess.run([str(java), "-version"], capture_output=True, text=True, check=True, env=environment).stderr
    manifest = dict(scope="runner lifecycle verification only" if args.smoke else "controlled local A-B-B-A experiment; descriptive results only",
                    status="running", started_at_utc=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    protocol=protocol, engine_sha256=digest(engine), jar_sha256=hashes,
                    runner_sha256=digest(Path(__file__)), python_version=platform.python_version(),
                    source_metadata=metadata, java_version=java_version, platform=platform.platform(),
                    logical_cpus=os.cpu_count(), child_environment=environment, blocks=[])
    manifest['artifact_paths'] = {v:str(p) for v,p in jars.items()}
    manifest['same_artifact_verified'] = hashes['baseline'] == hashes['candidate']
    manifest['diagnostic_only'] = args.diagnostics
    manifest['helper_sha256'] = {name: digest(Path(__file__).with_name(name)) for name in
                               ('reference_k6.py', 'reference_k6.js', 'experiment_diagnostics.py')}
    save(output / "manifest.json", manifest)
    if args.same_artifact:
        save(output/'artifact-selection.json',dict(kind='identical executable control',sha256=hashes))
    elif (source / 'candidate.patch').exists(): shutil.copy2(source / "candidate.patch", output / "candidate.patch")
    else: shutil.copy2(source / 'source.patch', output / 'source.patch')
    print(f"Results: {output}", flush=True)
    server = None
    try:
        if existing_pid is not None:
            engine_idle(engine)
            if managed_listener(args.port, current) != existing_pid:
                raise RuntimeError("Server identity changed during preflight; nothing was stopped")
            print(f"Stopping managed Petclinic PID {existing_pid} before the experiment.", flush=True)
            stop_managed(existing_pid, args.port)
            current.update(running=False, stopped_by_experiment=str(output))
            save(current_path, current)
        base = f"http://127.0.0.1:{args.port}"
        expected = (source / "baseline-owner.html").read_bytes()
        expected_engine = None
        for number, variant in enumerate(ORDER, 1):
            engine_idle(engine)
            if pids(["lsof", "-nP", "-t", f"-iTCP:{args.port}", "-sTCP:LISTEN"]):
                raise RuntimeError("Another service acquired the experiment port")
            if digest(jars[variant]) != hashes[variant] or digest(engine) != manifest["engine_sha256"]:
                raise RuntimeError("An experiment binary changed during the experiment")
            block = output / f"{number:02d}-{variant}"
            block.mkdir()
            command = [str(java), "-Xms512m", "-Xmx512m", "-jar", str(jars[variant]),
                       "--server.address=127.0.0.1", f"--server.port={args.port}",
                       "--spring.config.location=classpath:/application.properties", "--spring.profiles.active=default",
                       "--database=h2", "--logging.level.root=INFO", "--logging.level.org.springframework.web=INFO",
                       "--logging.level.org.hibernate.SQL=WARN", "--debug=false", "--trace=false"]
            if args.scenario != 'existing':
                command[command.index('--spring.profiles.active=default')] = '--spring.profiles.active=measurement-validation'
            if args.diagnostics:
                command[1:1] = jvm_flags(block)
            save(block / "server-command.json", command)
            print(f"Block {number}/4: {variant}; starting a fresh JVM.", flush=True)
            with (block / "server.log").open("x") as log:
                server = subprocess.Popen(command, cwd=petclinic, env=environment, stdin=subprocess.DEVNULL,
                                          stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
                diagnostic = Diagnostics(server.pid,block,java) if args.diagnostics else None
                try:
                    ready(server, base, expected)
                    listeners = pids(["lsof", "-nP", "-t", f"-iTCP:{args.port}", "-sTCP:LISTEN"])
                    if listeners != {str(server.pid)}:
                        raise RuntimeError("The listening server does not match the JVM launched for this block")
                    print(f"  Warm-up: {protocol['warmup_requests']} requests (excluded).", flush=True)
                    if diagnostic: diagnostic.snapshot('before-warmup')
                    warmup = workload(engine, base + "/owners/1", protocol["warmup_requests"], block / "warmup", environment, protocol)
                    if args.scenario != 'existing':
                        delay, iterations = protocol['variant_settings'][variant]
                        request = urllib.request.Request(base+'/validation/config',
                            data=json.dumps(dict(delay_nanos=delay,cpu_iterations=iterations)).encode(),
                            headers={'Content-Type':'application/json'}, method='POST')
                        with CLIENT.open(request,timeout=5) as response:
                            save(block/'applied-controls.json',json.load(response))
                        workload(engine,base+'/owners/1',protocol['conditioning_requests'],block/'conditioning',environment,protocol)
                        if get(base+'/owners/1') != expected: raise RuntimeError('Controlled workload changed response body')
                    if diagnostic: diagnostic.snapshot('after-warmup'); diagnostic.phase='measurement'
                    if expected_engine is None:
                        expected_engine = warmup["engine"]
                    results = []
                    for run in range(1, protocol["measured_runs_per_block"] + 1):
                        before = json.loads(get(base+'/validation/stats')) if args.scenario != 'existing' else None
                        result = workload(engine, base + "/owners/1", protocol["measured_requests"], block / f"run-{run:02d}", environment, protocol)
                        if before is not None:
                            after=json.loads(get(base+'/validation/stats'))
                            n=protocol['measured_requests']; delay,iterations=protocol['variant_settings'][variant]
                            expected_deltas=dict(requests=n,delayCalls=n if delay else 0,cpuCalls=n if iterations else 0,cpuIterationsExecuted=n*iterations)
                            actual={k:after[k]-before[k] for k in expected_deltas}
                            save(block/f'run-{run:02d}.server-stats.json',dict(before=before,after=after,deltas=actual,expected=expected_deltas))
                            if actual != expected_deltas or (iterations and after['checksum']==0):
                                raise RuntimeError('Injected application code execution counts do not match the workload')
                        if result["engine"] != expected_engine:
                            raise RuntimeError("Engine build metadata changed")
                        if server.poll() is not None:
                            raise RuntimeError("The trial JVM exited during measurement")
                        results.append(result)
                        print(f"  Run {run}: {result['successful_throughput_rps']:.1f} req/s; p99 {result['success_latency_ms']['p99']:.3f} ms", flush=True)
                    manifest["blocks"].append(describe_block(number, variant, results))
                    if diagnostic: diagnostic.snapshot('after-measurement')
                    save(output / "manifest.json", manifest)
                finally:
                    stop_child(server)
                    if diagnostic: diagnostic.close()
                    server = None
            if number != len(ORDER):
                time.sleep(protocol["rest_between_blocks_seconds"])
        if digest(engine) != manifest["engine_sha256"] or any(digest(jars[v]) != hashes[v] for v in jars):
            raise RuntimeError("An experiment binary changed during the experiment")
        manifest["status"] = "complete"
    except BaseException as error:
        manifest.update(status="interrupted" if isinstance(error, KeyboardInterrupt) else "failed", error=str(error))
        raise
    finally:
        stop_child(server)
        manifest["finished_at_utc"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        save(output / "manifest.json", manifest)
    print("Complete. All trial JVMs stopped. Warm-ups are separate; no significance verdict is computed.", flush=True)
    report = write_comparison(output) if not args.diagnostics else {'status':'diagnostic_only'}
    if report["status"] == "invalid":
        raise RuntimeError("Completed outputs failed comparison checks; inspect comparison.json")
    return output


def interrupted(*_):
    raise KeyboardInterrupt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--run", action="store_true", help="execute the full experiment; stops the recognized server on the chosen port")
    mode.add_argument("--smoke", action="store_true", help="execute tiny functional checks, unsuitable for performance interpretation")
    mode.add_argument("--report", type=Path, help="check and compare a completed experiment directory without running workloads")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--engine", type=Path, default=ROOT / "build/release/spe")
    parser.add_argument('--tool', choices=('spe','k6'), default='spe')
    parser.add_argument('--k6',type=Path,default=ROOT/'build/tools/k6-v2.4.0/k6-v2.4.0-macos-arm64/k6')
    parser.add_argument('--same-artifact',choices=('baseline','candidate'))
    parser.add_argument('--scenario',choices=('existing','control','delay','cpu-small','cpu-large'),default='existing')
    parser.add_argument('--requests',type=int,default=50000)
    parser.add_argument('--warmup-requests',type=int,default=50000)
    parser.add_argument('--conditioning-requests',type=int,default=2000)
    parser.add_argument('--runs',type=int,default=5)
    parser.add_argument('--diagnostics',action='store_true')
    parser.add_argument("--petclinic", type=Path, default=ROOT.parent / "spring-petclinic")
    parser.add_argument("--output-root", type=Path, default=ROOT / "build/petclinic-results")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("port must be in [1, 65535]")
    if any(not 1 <= n <= 1_000_000 for n in (args.requests,args.warmup_requests,args.conditioning_requests)) or not 1 <= args.runs <= 10:
        parser.error('request budgets must be 1..1000000 and runs 1..10')
    # Raising on SIGTERM ensures the same child cleanup as Ctrl-C.
    signal.signal(signal.SIGTERM, interrupted)
    try:
        if args.report is not None:
            if not args.report.is_dir():
                raise RuntimeError("Experiment directory does not exist")
            report = write_comparison(args.report.resolve())
            return 1 if report["status"] == "invalid" else 0
        with exclusive_run(ROOT / "build/petclinic-results/.comparison.lock"):
            inputs = prepare(args)
            print(json.dumps(inputs[-1], indent=2), flush=True)
            if args.run or args.smoke:
                execute(args, inputs)
            else:
                current = managed_record(inputs[1])
                listener = managed_listener(args.port, current)
                print(f"Inputs verified. Recognized listener: {listener or 'none'}. Use --run to execute.")
    except (OSError, ValueError, RuntimeError, KeyError, subprocess.SubprocessError) as error:
        print(f"Experiment stopped: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Experiment interrupted; partial artifacts retained and trial processes stopped.", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
