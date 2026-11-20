"""Bound artifact retention for routine functional tests, separately from benchmarks."""
import argparse
import fcntl
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path


OWNER = 'spe-functional-tests-v1'


def inventory(path):
    """Fingerprint only local nodes; never follow fixture symlinks or open FIFOs."""
    result = {}
    for parent, directories, files in os.walk(path, followlinks=False):
        for name in directories + files:
            p = Path(parent)/name
            if p == path/'.spe-test-run.json':
                continue
            key = str(p.relative_to(path))
            if p.is_symlink():
                result[key] = ['link', os.readlink(p)]
            elif p.is_dir():
                result[key] = ['directory']
            elif p.is_file():
                digest = hashlib.sha256()
                with p.open('rb') as stream:
                    while chunk := stream.read(1024*1024):
                        digest.update(chunk)
                result[key] = ['file', digest.hexdigest()]
            else:
                result[key] = ['special', p.lstat().st_mode]
    return result


def read_owned(path, suite):
    if path.is_symlink() or not path.is_dir():
        return None
    marker = path/'.spe-test-run.json'
    if marker.is_symlink():
        return None
    try:
        data = json.loads(marker.read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get('owner') == OWNER and data.get('suite') == suite else None


def prune(root, suite, keep_successes, keep_failures):
    """Caller holds the per-suite lock. Unmarked, unfinished and foreign data is untouched."""
    for status, limit in [('passed', keep_successes), ('failed', keep_failures)]:
        runs = []
        for path in root.glob('run-*'):
            data = read_owned(path, suite)
            if data and data.get('status') == status:
                runs.append((path.stat().st_mtime_ns, path.name, path))
        for _, _, path in sorted(runs, reverse=True)[limit:]:
            if any(p.name in ['AGENTS.md', 'credentials', 'secrets'] or
                   (p.name.startswith('.') and p != path/'.spe-test-run.json') for p in path.rglob('*')):
                continue
            try:
                if data := read_owned(path, suite):
                    if data.get('inventory') != inventory(path):
                        continue
                else:
                    continue
            except OSError:
                continue  # Changed, unreadable or unrecognized evidence is never pruned.
            shutil.rmtree(path)


def limit(value):
    n = int(value)
    if not 0 <= n <= 20:
        raise argparse.ArgumentTypeError('retention must be between 0 and 20 runs per suite')
    return n


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--suite', choices=['integration', 'comparison', 'https', 'headers', 'mixed_workload'], required=True)
    parser.add_argument('--engine', type=Path, required=True)
    parser.add_argument('--openssl', type=Path)
    parser.add_argument('--artifacts', type=Path, required=True)
    parser.add_argument('--keep-successes', type=limit, default=os.environ.get('SPE_TEST_KEEP_SUCCESSES', '0'))
    parser.add_argument('--keep-failures', type=limit, default=os.environ.get('SPE_TEST_KEEP_FAILURES', '2'))
    args = parser.parse_args()
    root = args.artifacts.resolve()/args.suite
    if root.is_symlink():
        parser.error('managed suite directories must not be symlinks')
    root.mkdir(parents=True, exist_ok=True)
    lock = root/'.lock'
    if lock.is_symlink():
        parser.error('refusing a symlink lock file')
    with lock.open('a') as guard:
        fcntl.flock(guard, fcntl.LOCK_EX)
        # Initialization belongs under the same lock as execution and pruning.
        marker = root/'.spe-managed-tests.json'
        if marker.is_symlink():
            parser.error('refusing a symlink ownership marker')
        if marker.exists():
            try:
                owned = json.loads(marker.read_text()) == dict(owner=OWNER, suite=args.suite)
            except (OSError, ValueError):
                owned = False
            if not owned:
                parser.error('artifact directory belongs to another owner')
        else:
            if any(p != lock for p in root.iterdir()):
                parser.error('refusing to manage a nonempty unmarked directory')
            marker.write_text(json.dumps(dict(owner=OWNER, suite=args.suite))+'\n')
        directory = Path(tempfile.mkdtemp(prefix='run-', dir=root))
        run_marker = directory/'.spe-test-run.json'
        metadata = dict(owner=OWNER, suite=args.suite, status='running')
        run_marker.write_text(json.dumps(metadata)+'\n')
        script = 'compare.py' if args.suite == 'comparison' else args.suite+'.py'
        command = [sys.executable, str(Path(__file__).with_name(script)), '--engine', str(args.engine.resolve()),
                   '--artifacts', str(directory/'artifacts')]
        if args.openssl:
            command += ['--openssl', str(args.openssl.resolve())]
        started = time.monotonic()
        # File redirection avoids unbounded capture in memory and keeps CTest output quiet.
        with (directory/'output.log').open('w') as output:
            process = subprocess.run(command, stdout=output, stderr=subprocess.STDOUT)
        metadata.update(status='passed' if process.returncode == 0 else 'failed', returncode=process.returncode,
                        elapsed_seconds=time.monotonic()-started)
        observations = list((directory/'artifacts').glob('**/observations.json'))
        summary = dict(metadata)
        if len(observations) == 1:
            try:
                data = json.loads(observations[0].read_text())
                summary.update({k: v for k, v in data.items() if k not in ['observations', 'engine']})
            except (OSError, ValueError, AttributeError):
                metadata.update(status='failed', returncode=1)
                summary = dict(metadata, summary_error='unreadable test manifest; raw diagnostics retained')
        # Publish the compact summary before pruning only our completed runs.
        temporary = root/'summary.pending'
        temporary.write_text(json.dumps(summary, indent=2)+'\n')
        temporary.replace(root/'summary.json')
        run_marker.write_text(json.dumps(dict(metadata, inventory=inventory(directory)))+'\n')
        print(f"{args.suite}: {metadata['status']} ({metadata['elapsed_seconds']:.2f}s)")
        if metadata['returncode']:
            with (directory/'output.log').open('rb') as log:
                log.seek(max(0, (directory/'output.log').stat().st_size-65536))
                print(log.read().decode(errors='replace'))
            print(f'Failure diagnostics: {directory}')
        prune(root, args.suite, args.keep_successes, args.keep_failures)
        return metadata['returncode']


if __name__ == '__main__':
    raise SystemExit(main())
