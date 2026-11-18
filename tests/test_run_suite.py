"""Retention checks use newly generated disposable directories only."""
import json
import multiprocessing
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import run_suite
from run_suite import OWNER, inventory, prune, read_owned


def concurrent_run(path, start, active, peak, results):
    def execute(*args, **kwargs):
        with active.get_lock():
            active.value += 1
            peak.value = max(peak.value, active.value)
        time.sleep(0.15)
        with active.get_lock():
            active.value -= 1
        return mock.Mock(returncode=0)
    start.wait(10)
    args = ['run_suite.py', '--suite', 'integration', '--engine', '/unused', '--artifacts', path]
    with mock.patch('sys.argv', args), mock.patch.object(run_suite.subprocess, 'run', side_effect=execute), mock.patch('builtins.print'):
        results.put(run_suite.main())


def mark(path, suite, status, owner=OWNER):
    (path/'.spe-test-run.json').write_text(json.dumps(dict(owner=owner, suite=suite, status=status, inventory=inventory(path))))


class RetentionTests(unittest.TestCase):
    def test_wrapper_keeps_two_failures_then_compacts_a_success(self):
        with tempfile.TemporaryDirectory() as temp:
            args = ['run_suite.py', '--suite', 'integration', '--engine', '/unused-test-engine', '--artifacts', temp]
            def execute(command, **kwargs):
                kwargs['stdout'].write('controlled harness diagnostic\n')
                return mock.Mock(returncode=1)
            with mock.patch('sys.argv', args), mock.patch.object(run_suite.subprocess, 'run', side_effect=execute), mock.patch('builtins.print'):
                for _ in range(3):
                    self.assertEqual(run_suite.main(), 1)
                root = Path(temp)/'integration'
                self.assertEqual(len(list(root.glob('run-*'))), 2)
                with mock.patch.object(run_suite.subprocess, 'run', return_value=mock.Mock(returncode=0)):
                    self.assertEqual(run_suite.main(), 0)
                self.assertEqual(len(list(root.glob('run-*'))), 2)
                self.assertEqual(json.loads((root/'summary.json').read_text())['status'], 'passed')

    def test_prunes_only_owned_completed_runs_and_keeps_diagnostics(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for name, owner, suite, status in [
                ('run-success', OWNER, 'headers', 'passed'),
                ('run-failure', OWNER, 'headers', 'failed'),
                ('run-active', OWNER, 'headers', 'running'),
                ('run-foreign', 'other', 'headers', 'passed'),
                ('run-other-suite', OWNER, 'https', 'passed')]:
                p = root/name; p.mkdir()
                mark(p, suite, status, owner)
            unmarked = root/'run-unmarked'; unmarked.mkdir()
            (unmarked/'important.json').write_text('original evidence')
            alias = root/'run-symlink'; alias.symlink_to(unmarked, target_is_directory=True)
            prune(root, 'headers', 0, 2)
            self.assertFalse((root/'run-success').exists())
            for name in ['run-failure', 'run-active', 'run-foreign', 'run-other-suite', 'run-unmarked', 'run-symlink']:
                self.assertTrue((root/name).exists(), name)

    def test_failure_limit_and_protected_instructions(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for i in range(5):
                p = root/f'run-{i}'; p.mkdir()
                mark(p, 'headers', 'failed')
            p = root/'run-protected'; p.mkdir()
            mark(p, 'headers', 'passed')
            (p/'AGENTS.md').write_text('local instructions')
            hidden = root/'run-hidden'; hidden.mkdir()
            (hidden/'.local-notes').write_text('private notes')
            mark(hidden, 'headers', 'passed')
            prune(root, 'headers', 0, 2)
            self.assertEqual(len([p for p in root.iterdir() if read_owned(p, 'headers').get('status') == 'failed']), 2)
            self.assertTrue((root/'run-protected/AGENTS.md').exists())
            self.assertTrue((root/'run-hidden/.local-notes').exists())

    def test_modified_unknown_and_malformed_evidence_is_preserved(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for name in ['changed', 'added', 'legacy', 'malformed']:
                p = root/('run-'+name); p.mkdir()
                (p/'output.log').write_text('original')
                mark(p, 'headers', 'passed')
            (root/'run-changed/output.log').write_text('investigation notes')
            (root/'run-added/unique.json').write_text('historical evidence')
            (root/'run-legacy/.spe-test-run.json').write_text(json.dumps(dict(owner=OWNER, suite='headers', status='passed')))
            (root/'run-malformed/.spe-test-run.json').write_text('[]')
            prune(root, 'headers', 0, 0)
            self.assertEqual(len(list(root.glob('run-*'))), 4)

    def test_concurrent_first_runs_are_serialized(self):
        with tempfile.TemporaryDirectory() as temp:
            ctx = multiprocessing.get_context('spawn')
            start, results = ctx.Event(), ctx.Queue()
            active, peak = ctx.Value('i', 0), ctx.Value('i', 0)
            workers = [ctx.Process(target=concurrent_run, args=(temp, start, active, peak, results)) for _ in range(2)]
            try:
                for worker in workers: worker.start()
                start.set()
                for worker in workers:
                    worker.join(15)
                    self.assertEqual(worker.exitcode, 0)
                self.assertEqual([results.get(timeout=5) for _ in workers], [0, 0])
                self.assertEqual(peak.value, 1)
                self.assertEqual(len(list((Path(temp)/'integration').glob('run-*'))), 0)
            finally:
                for worker in workers:
                    if worker.is_alive(): worker.terminate(); worker.join(5)


if __name__ == '__main__':
    unittest.main()
