"""Installed native catalog and shared helpers, cwd=/ without PYTHONPATH."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class LaunchdImportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name).resolve()
        self.product = self.home / '.agentstack'
        self.dashboard = self.product / 'dashboard'
        self.dashboard.mkdir(parents=True)
        for file in ('managed_catalog.py', 'managed_capabilities.py', 'harness_helpers.py'):
            shutil.copyfile(ROOT / 'dashboard' / file, self.dashboard / file)
        hooks = self.product / 'hooks'
        hooks.mkdir()
        for file in ('harness_child.py', 'child_resume.py', 'child_tools.py', 'native_connections.py', 'native_server_models.py'):
            shutil.copyfile(ROOT / 'hooks' / file, hooks / file)
        self.manifest = self.home / 'manifest.json'
        self.manifest.write_text(json.dumps({'enabled': True,
            'backends': {'deepseek': {'model': 'fixture-deepseek'}, 'strata': {'model': 'fixture-strata'}},
            'harnesses': {}}))

    def child(self, code):
        # The application path is only installed dashboard/. Product helpers
        # must resolve their own package siblings without a checkout on sys.path.
        boot = '''import sys, json
from pathlib import Path
stdlib = [p for p in sys.path if 'site-packages' not in p]
sys.path[:] = [sys.argv[1]] + stdlib
assert str(Path.cwd()) == '/'
assert sys.argv[2] not in sys.path
import managed_catalog as m
'''
        return subprocess.run([sys.executable, '-I', '-B', '-c', boot + code,
                               str(self.dashboard), str(self.product), str(self.manifest)],
            cwd='/', env={'HOME': str(self.home), 'PATH': '/usr/bin:/bin'},
            capture_output=True, text=True, timeout=10)

    def test_catalog_resolves_shared_launcher_and_returns_unavailable_rows(self):
        result = self.child('''
# Platform is synthetic; filesystem checks use the real installed helpers.
m.sys.platform='darwin'
launcher=m.thin_launcher()
assert Path(launcher.__file__).resolve().is_relative_to(Path(sys.argv[2]))
assert launcher.child_resume and launcher.child_tools
assert Path(launcher.native_server_models.__file__).resolve().is_relative_to(Path(sys.argv[2]))
rows=m.standard_catalog(sys.argv[3],mode='legacy')
assert [p['id'] for p in rows] == ['deepseek','strata']
assert all(not h['available'] and h['reason'] for p in rows for r in p['models'] for h in r['harnesses'])
assert all('managed' not in p for p in rows)
print('installed imports resolved')
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), 'installed imports resolved')
        self.assertEqual(result.stderr, '')

    def test_missing_shared_helper_cannot_advertise_ready(self):
        (self.product / 'hooks' / 'harness_child.py').unlink()
        result = self.child('''
m.sys.platform='darwin'
rows=m.standard_catalog(sys.argv[3],mode='legacy')
assert rows
assert all(not h['available'] and h['reason']=='runtime_not_prepared'
           for p in rows for r in p['models'] for h in r['harnesses'])
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, '')


if __name__ == '__main__':
    unittest.main()
