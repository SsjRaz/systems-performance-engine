"""Target preparation checks; numerical XML fixtures are not benchmark evidence."""
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import compare_petclinic as runner
import prepare_petclinic as prepare


class TargetChecks(unittest.TestCase):
    def test_checked_in_patches_match_the_pinned_specification(self):
        spec = prepare.load_spec()
        self.assertEqual(spec['revision'], '500158f732419217507c7656904b8e6aa1bcc0d6')

    def test_changed_patch_and_non_commit_revision_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary)
            for source in prepare.SPEC.glob('*'):
                if source.is_file(): shutil.copy2(source, path / source.name)
            patch = path / 'owner-reuse.patch'
            original = patch.read_bytes(); patch.write_bytes(original + b'changed')
            with self.assertRaises(RuntimeError): prepare.load_spec(path)
            patch.write_bytes(original)
            spec = json.loads((path / 'target.json').read_text()); spec['revision'] = 'main'
            (path / 'target.json').write_text(json.dumps(spec))
            with self.assertRaises(RuntimeError): prepare.load_spec(path)

    def test_test_reports_must_exist_and_have_no_failures_or_skips(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary)
            with self.assertRaises(RuntimeError): prepare.test_counts(path)
            report = path / 'TEST-fixture.xml'
            report.write_text('<testsuite tests="3" failures="0" errors="0" skipped="0"/>')
            self.assertEqual(prepare.test_counts(path), dict(tests=3, failures=0, errors=0, skipped=0))
            for outcome in ('failures', 'errors', 'skipped'):
                values = dict(tests=3, failures=0, errors=0, skipped=0); values[outcome] = 1
                report.write_text('<testsuite ' + ' '.join(f'{k}="{v}"' for k, v in values.items()) + '/>')
                with self.subTest(outcome=outcome), self.assertRaises(RuntimeError): prepare.test_counts(path)

    def test_fresh_target_needs_no_previous_server_run(self):
        with tempfile.TemporaryDirectory() as temporary:
            self.assertEqual(runner.managed_record(Path(temporary)), {})
        self.assertEqual(runner.recorded_java(dict(java_executable='/jdk/bin/java')), Path('/jdk/bin/java'))
        self.assertEqual(runner.recorded_java(dict(command=['/old-jdk/bin/java','-jar','old.jar'])), Path('/old-jdk/bin/java'))

    def test_timeout_stops_a_forked_child_that_ignores_termination(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary); pid_file = path / 'child.pid'
            child = 'import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); print("ready",flush=True); time.sleep(60)'
            parent = ('import subprocess,sys,time,pathlib; '
                      f'p=subprocess.Popen([sys.executable,"-c",{child!r}],stdout=subprocess.PIPE,text=True); '
                      'p.stdout.readline(); pathlib.Path(sys.argv[1]).write_text(str(p.pid)); time.sleep(60)')
            try:
                with self.assertRaises(subprocess.TimeoutExpired):
                    prepare.run([sys.executable, '-c', parent, str(pid_file)], path, path / 'parent.log', timeout=1)
                self.assertTrue(pid_file.exists(), 'forked child did not reach its ready state')
                pid = int(pid_file.read_text())
                for _ in range(20):
                    if sys.platform.startswith('linux'):
                        try: live = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[0] != 'Z'
                        except (FileNotFoundError, ProcessLookupError): live = False
                    else:
                        result = subprocess.run(['ps', '-p', str(pid), '-o', 'stat='], capture_output=True, text=True)
                        live = result.returncode == 0 and not result.stdout.strip().startswith('Z')
                    if not live: break
                    time.sleep(0.05)
                self.assertFalse(live, 'owned child survived the timed-out build')
            finally:
                if pid_file.exists():
                    try: os.kill(int(pid_file.read_text()), signal.SIGKILL)
                    except ProcessLookupError: pass


if __name__ == '__main__': unittest.main(verbosity=2)
