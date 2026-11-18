"""Build the pinned public Petclinic target in a new, isolated directory."""

import argparse
import datetime
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import xml.etree.ElementTree as ET
from pathlib import Path

import compare_petclinic as runner

SPEC = runner.ROOT / 'benchmarks/petclinic'


def load_spec(directory=SPEC):
    spec = json.loads((directory / 'target.json').read_text())
    if not re.fullmatch(r'[0-9a-f]{40}', spec['revision']):
        raise RuntimeError('Target revision must be a full Git commit SHA')
    if set(spec['patches']) != {'owner-reuse.patch', 'measurement-controls.patch'}:
        raise RuntimeError('Target must identify both expected source patches')
    for name, expected in spec['patches'].items():
        if Path(name).name != name or runner.digest(directory / name) != expected:
            raise RuntimeError(f'Target patch checksum failed: {name}')
    return spec


def java_home(requested, major):
    candidate = requested / 'bin/java' if requested else Path(shutil.which('java') or '/missing/java')
    settings = subprocess.run([str(candidate), '-XshowSettings:properties', '-version'],
                              capture_output=True, text=True, check=True, timeout=20).stderr
    version = re.search(r'^\s*java.specification.version = (\d+)\s*$', settings, re.M)
    home = re.search(r'^\s*java.home = (.+)$', settings, re.M)
    if not version or int(version[1]) != major or not home:
        raise RuntimeError(f'A JDK {major} installation is required; use --java-home PATH')
    home = Path(home[1]).resolve()
    if not (home / 'bin/javac').is_file():
        raise RuntimeError('The selected Java installation is not a full JDK')
    return home


def run(command, cwd, log, environment=None, timeout=600):
    runner.save(log.with_suffix('.command.json'), dict(command=command, cwd=str(cwd)))
    with log.open('x') as stream:
        process = subprocess.Popen(command, cwd=cwd, env=environment, stdin=subprocess.DEVNULL,
                                   stdout=stream, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            code = process.wait(timeout=timeout)
        finally:
            # Maven forks a test JVM; stop the entire owned process group on interruption.
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
            # A fork can ignore SIGTERM even after Maven itself has exited.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    if code:
        raise RuntimeError(f'Command failed ({code}); inspect {log}')


def test_counts(directory):
    reports = sorted(directory.glob('TEST-*.xml'))
    if not reports:
        raise RuntimeError('No Maven test reports were produced')
    counts = {key: 0 for key in ('tests', 'failures', 'errors', 'skipped')}
    for report in reports:
        suite = ET.parse(report).getroot()
        for key in counts:
            counts[key] += int(suite.get(key, '0'))
    if counts['tests'] == 0 or any(counts[k] for k in ('failures', 'errors', 'skipped')):
        raise RuntimeError(f'Target tests did not all execute successfully: {counts}')
    return counts


def capture_reference(checkout, jar, java, directory, port):
    if runner.pids(['lsof', '-nP', '-t', f'-iTCP:{port}', '-sTCP:LISTEN']):
        raise RuntimeError('Reference port is occupied; no service was stopped')
    command = [str(java), '-Xms512m', '-Xmx512m', '-jar', str(jar),
               '--server.address=127.0.0.1', f'--server.port={port}',
               '--spring.config.location=classpath:/application.properties',
               '--spring.profiles.active=default', '--spring.docker.compose.enabled=false',
               '--database=h2', '--logging.level.root=INFO', '--logging.level.org.hibernate.SQL=WARN']
    runner.save(directory / 'reference-command.json', command)
    environment = {'PATH': os.environ.get('PATH', '/usr/bin:/bin'), 'LANG': 'C', 'LC_ALL': 'C'}
    base = f'http://127.0.0.1:{port}'
    with (directory / 'reference-server.log').open('x') as log:
        process = subprocess.Popen(command, cwd=checkout, env=environment, stdin=subprocess.DEVNULL,
                                   stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            deadline = time.monotonic() + 60
            while process.poll() is None and time.monotonic() < deadline:
                try:
                    if json.loads(runner.get(base + '/actuator/health')).get('status') == 'UP':
                        listeners = runner.pids(['lsof', '-nP', '-t', f'-iTCP:{port}', '-sTCP:LISTEN'])
                        if listeners != {str(process.pid)}:
                            raise RuntimeError('Reference listener does not match the launched JVM')
                        body = runner.get(base + '/owners/1')
                        if not all(value in body for value in (b'George', b'Franklin', b'Leo')):
                            raise RuntimeError('Seeded owner response is not the expected application page')
                        return body
                except (urllib.error.URLError, TimeoutError, ConnectionError):
                    pass
                time.sleep(0.25)
            raise RuntimeError('Reference application failed readiness; inspect reference-server.log')
        finally:
            runner.stop_child(process)


def prepare(args):
    spec = load_spec()
    for tool in ('git', 'pgrep', 'ps', 'lsof'):
        if not shutil.which(tool):
            raise RuntimeError(f'Required tool is missing: {tool}')
    home = java_home(args.java_home, spec['java_major'])
    java = home / 'bin/java'
    version = subprocess.run([str(java), '-version'], capture_output=True, text=True,
                             check=True, timeout=20).stderr.strip()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    logs = output / 'logs'; logs.mkdir()
    checkout = output / 'source'
    staging = output / 'artifacts'; staging.mkdir()
    # Maven clean removes target/. Preserved JARs stay outside it until all builds finish.
    owner = staging / 'owner-reuse'; owner.mkdir()
    controls = staging / 'measurement-validation'; controls.mkdir()
    state = dict(status='running', scope='target preparation and HTTP reference capture; not a benchmark',
                 started_at_utc=datetime.datetime.now(datetime.timezone.utc).isoformat(), spec=spec,
                 script_sha256=runner.digest(Path(__file__)), java_version=version, variants=[])
    runner.save(output / 'preparation.json', state)
    print(f'Preparation: {output}', flush=True)
    environment = dict(os.environ)
    for key in tuple(environment):
        if key.startswith('SPRING_') or key in ('JDK_JAVA_OPTIONS', '_JAVA_OPTIONS', 'MAVEN_ARGS'):
            environment.pop(key)
    environment.update(JAVA_HOME=str(home), PATH=str(home / 'bin') + os.pathsep + environment.get('PATH', ''),
                       MAVEN_OPTS='-Xmx512m', JAVA_TOOL_OPTIONS='-Xmx512m')
    try:
        run(['git', 'init', '--quiet', str(checkout)], output, logs / 'git-init.log', timeout=30)
        run(['git', 'remote', 'add', 'origin', spec['repository']], checkout, logs / 'git-origin.log', timeout=30)
        run(['git', 'fetch', '--depth', '1', 'origin', spec['revision']], checkout, logs / 'git-fetch.log', timeout=180)
        run(['git', 'checkout', '--detach', '--quiet', 'FETCH_HEAD'], checkout, logs / 'git-checkout.log', timeout=30)
        revision = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=checkout, text=True).strip()
        if revision != spec['revision']:
            raise RuntimeError('Fetched revision differs from the pinned target')
        applied = []
        for variant, patch, destination in (
                ('baseline', None, owner / 'baseline.jar'),
                ('candidate', 'owner-reuse.patch', owner / 'candidate.jar'),
                ('validation', 'measurement-controls.patch', controls / 'validation.jar')):
            if patch:
                path = str((SPEC / patch).resolve())
                run(['git', 'apply', '--check', path], checkout, logs / f'{variant}-patch-check.log', timeout=30)
                run(['git', 'apply', path], checkout, logs / f'{variant}-patch.log', timeout=30)
                applied.append(patch)
            tests = 'OwnerControllerTests,PetClinicIntegrationTests'
            if variant == 'validation':
                tests += ',MeasurementValidationTests'
            command = ['./mvnw', '-B', '-ntp', '-Dspring.docker.compose.enabled=false', f'-Dtest={tests}',
                       'clean', 'package']
            print(f'Building {variant}; Maven and test JVM heaps are limited to 512 MiB.', flush=True)
            run(command, checkout, logs / f'{variant}-build.log', environment)
            reports = checkout / 'target/surefire-reports'
            counts = test_counts(reports)
            report_copy = logs / f'{variant}-test-reports'; report_copy.mkdir()
            for report in reports.glob('TEST-*.xml'):
                shutil.copy2(report, report_copy / report.name)
            jars = list((checkout / 'target').glob('spring-petclinic-*.jar'))
            if len(jars) != 1:
                raise RuntimeError(f'Expected one executable JAR; found {len(jars)}')
            shutil.copy2(jars[0], destination)
            metadata = dict(repository=spec['repository'], source_revision=revision, variant=variant,
                            java_executable=str(java), java_version=version, build_command=command,
                            build_tests=counts, build_tests_skipped=False, source_dirty=bool(applied),
                            applied_patch_sha256={name: spec['patches'][name] for name in applied},
                            jar_sha256=runner.digest(destination), build_log=str(logs / f'{variant}-build.log'))
            name = 'build.json' if variant == 'validation' else f'{variant}-run.json'
            runner.save(destination.parent / name, metadata)
            state['variants'].append(metadata)
            runner.save(output / 'preparation.json', state)
            print(f'  {counts["tests"]} tests passed; JAR SHA-256 {metadata["jar_sha256"]}', flush=True)
        shutil.copy2(SPEC / 'owner-reuse.patch', owner / 'candidate.patch')
        (controls / 'source.patch').write_bytes((SPEC / 'owner-reuse.patch').read_bytes() +
                                               (SPEC / 'measurement-controls.patch').read_bytes())
        artifacts = checkout / 'target/local-run'
        staging.rename(artifacts)
        runner.engine_idle(Path('spe'))
        reference = capture_reference(checkout, artifacts / 'owner-reuse/baseline.jar', java, logs, args.port)
        for directory in (artifacts / 'owner-reuse', artifacts / 'measurement-validation'):
            (directory / 'baseline-owner.html').write_bytes(reference)
        state.update(status='complete', petclinic_directory=str(checkout),
                     reference_owner_sha256=runner.digest(artifacts / 'owner-reuse/baseline-owner.html'))
        print(f'Target ready: {checkout}', flush=True)
        print(f'Smoke: python3 scripts/compare_petclinic.py --smoke --petclinic "{checkout}" --port {args.port}', flush=True)
    except BaseException as error:
        state.update(status='interrupted' if isinstance(error, KeyboardInterrupt) else 'failed', error=str(error))
        raise
    finally:
        state['finished_at_utc'] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        runner.save(output / 'preparation.json', state)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=runner.ROOT / 'build/targets/petclinic')
    parser.add_argument('--java-home', type=Path, default=Path(os.environ['JAVA_HOME']) if os.environ.get('JAVA_HOME') else None)
    parser.add_argument('--port', type=int, default=8081)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error('port must be in [1, 65535]')
    signal.signal(signal.SIGTERM, runner.interrupted)
    try:
        with runner.exclusive_run(runner.ROOT / 'build/petclinic-results/.comparison.lock'):
            runner.engine_idle(Path('spe'))
            prepare(args)
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError, ET.ParseError) as error:
        print(f'Preparation stopped: {error}', file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print('Preparation interrupted; partial files retained and child process stopped.', file=sys.stderr)
        return 130
    return 0


if __name__ == '__main__':
    sys.exit(main())
