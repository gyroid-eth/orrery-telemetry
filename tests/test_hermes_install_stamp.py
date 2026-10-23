"""Temporary installer acquisition with real pinned stamp writer/reader.

Git metadata and PM acquisition are fixture values. Stamp generation, receipt
validation and the runtime reader are real; no source install is modified.
"""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from test_managed_harness_install import CAFixture

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('stamp_installer', ROOT / 'scripts/lib/managed_harness_install.py')
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)

DENY = '''
def deny(name,args):
 if name in ('subprocess.Popen','socket.connect','socket.getaddrinfo'):
  raise RuntimeError('stamp fixture forbids subprocess and network')
sys.addaudithook(deny)
'''


class InstallStamp(unittest.TestCase):
    def test_fresh_v2_stamp_is_read_with_all_subprocesses_denied(self):
        source = os.environ.get('ORRERY_HERMES_TEST_SOURCE')
        python = os.environ.get('ORRERY_HERMES_TEST_PYTHON')
        if not source or not python:
            self.skipTest('Explicit installed dependency interpreter/source required')
        with tempfile.TemporaryDirectory() as temporary:
            ca = self.enterContext(CAFixture(installer, Path(temporary)))
            root = Path(temporary) / installer.generation_id(ROOT, 'hermes')
            root.mkdir()
            files = ['hermes_constants.py', 'hermes_cli/__init__.py', 'hermes_cli/version_info.py',
                     'hermes_cli/steward.py', 'hermes_cli/update_channel.py', 'hermes_cli/release_channels.py',
                     'pm/__init__.py', 'pm/paths.py', 'pm/environments.py', 'scripts/write_install_stamp.py']
            pm = []
            paths = {}
            def acquire(argv, *, cwd, env):
                nonlocal paths
                cwd = Path(cwd)
                if argv[:2] == ['git', 'clone']:
                    target = Path(argv[-1])
                    for relative in files:
                        dest = target / relative
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copyfile(Path(source) / relative, dest)
                    (target / '.git').mkdir()  # Any fallback would try forbidden git.
                    ca.source(target)
                elif argv[:2] == ['git', 'checkout']:
                    self.assertEqual(argv[-1], installer.HERMES_COMMIT)
                elif argv[:2] == ['git', 'rev-parse']:
                    return installer.HERMES_COMMIT
                elif argv[1:3] == ['-c', installer.HERMES_STAMP_SCRIPT]:
                    # Acquisition metadata is synthetic, but upstream's writer
                    # and the installer's exact stamp program execute unchanged.
                    code = 'import sys,os\nsys.path.insert(0,os.getcwd())\n' + DENY + '''
from hermes_cli import version_info as version
version.get_version_info=lambda:version.VersionInfo('0.1.0','0.1.0',0,sys.argv[1],'fixture','git',True,1)
''' + installer.HERMES_STAMP_SCRIPT
                    subprocess.run([python, '-I', '-B', '-c', code, installer.HERMES_COMMIT], cwd=cwd,
                                   env=env, capture_output=True, text=True, check=True, timeout=10)
                elif argv[-1] == installer.HERMES_TARGET_SCRIPT:
                    return ca.target
                elif argv[-1] == installer.HERMES_CA_PROBE_SCRIPT:
                    return json.dumps(paths)
                else:
                    self.assertEqual(argv[1:4], ['-m', 'pm.cli', 'install'])
                    pm.append(argv)
                    store = root / 'installs' / hashlib.sha256(str(cwd.resolve()).encode()).hexdigest()[:16]
                    generation = store / 'environments/fixture/venv'
                    (generation / 'bin').mkdir(parents=True, exist_ok=True)
                    (generation / 'bin/python').symlink_to(python) if not (generation / 'bin/python').exists() else None
                    executable = generation / 'bin/hermes'
                    executable.write_text('#!/bin/sh\nexit 0\n')
                    executable.chmod(0o700)
                    (generation / 'pyvenv.cfg').write_text('# fixture\n')
                    installer.save(store / 'facts.json', {'packages': {'venv': {'environment': str(generation)}}})
                    paths = ca.material(root, cwd, generation)
                return ''
            with patch.object(installer, 'run', side_effect=acquire):
                details = installer.install_hermes(ROOT, root, python)
                installer.validate(ROOT, 'hermes', {'harness': 'hermes', 'complete': True, 'details': details})
            self.assertEqual(len(pm), 2)
            installed = Path(details['hermes_source'])
            code = 'import sys\nsys.path.insert(0,sys.argv[1])\n' + DENY + '''
from hermes_cli.version_info import get_version_info
info=get_version_info()
assert info.commit==sys.argv[2] and info.base_version=='0.1.0' and info.source=='local' and info.dirty
print('stamp-reader-no-git-ok')
'''
            args = [python, '-I', '-B', '-c', code, str(installed), installer.HERMES_COMMIT]
            result = subprocess.run(args, cwd='/', env=installer.environment(root), capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), 'stamp-reader-no-git-ok')
            (installed / 'install-stamp.json').unlink()
            missing = subprocess.run(args, cwd='/', env=installer.environment(root), capture_output=True, text=True, timeout=10)
            self.assertNotEqual(missing.returncode, 0)
            self.assertIn('stamp fixture forbids subprocess', missing.stderr)
