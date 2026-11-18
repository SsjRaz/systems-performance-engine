"""Restoration checks use newly generated evidence only, never archived experiments."""
import gzip
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class RestorationTests(unittest.TestCase):
    def test_restore_after_build_cleanup_and_refuse_conflicts_or_corruption(self):
        (ROOT/'local-evidence').mkdir(exist_ok=True)
        (ROOT/'build').mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=ROOT/'local-evidence') as archives, tempfile.TemporaryDirectory(dir=ROOT/'build') as outputs:
            archive = Path(archives)/'fixture.json.gz'
            original = Path(outputs)/'missing-parent/fixture.json'
            payload = b'{"scope":"generated restoration test fixture"}\n'
            archive.write_bytes(gzip.compress(payload, mtime=0))
            manifest = Path(archives)/'manifest.json'
            manifest.write_text(json.dumps(dict(files=[dict(original=str(original.relative_to(ROOT)),
                archive=str(archive.relative_to(ROOT)), sha256=hashlib.sha256(payload).hexdigest(),
                compressed_sha256=hashlib.sha256(archive.read_bytes()).hexdigest(), original_bytes=len(payload),
                mode=0o600, mtime_ns=1_000_000_000)])))
            def run(*extra):
                return subprocess.run([sys.executable, '-O', str(ROOT/'scripts/restore_profiles.py'),
                    '--manifest', str(manifest), *extra], text=True, capture_output=True, timeout=10)
            self.assertEqual(run().returncode, 0)
            self.assertFalse(original.exists())
            self.assertEqual(run('--restore').returncode, 0)
            self.assertEqual(original.read_bytes(), payload)
            self.assertTrue(archive.exists())
            self.assertEqual(run('--restore').returncode, 0)
            original.write_text('a conflicting file must survive')
            self.assertNotEqual(run('--restore').returncode, 0)
            self.assertEqual(original.read_text(), 'a conflicting file must survive')
            archive.write_bytes(b'corrupted recording')
            self.assertNotEqual(run().returncode, 0)
            self.assertEqual(original.read_text(), 'a conflicting file must survive')


if __name__ == '__main__':
    unittest.main()
