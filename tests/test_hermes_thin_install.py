"""Hermes plain receipts with owned CA inputs; no package/network calls."""
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from test_managed_harness_install import CAFixture, fixture_stamp

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('thin_installer', ROOT / 'scripts/lib/managed_harness_install.py')
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class ThinInstall(unittest.TestCase):
    def test_native_ca_install_keeps_plain_contract_and_binds_recipe(self):
        with tempfile.TemporaryDirectory() as temporary, CAFixture(installer, Path(temporary)) as ca:
            root = Path(temporary) / 'runtime'
            root.mkdir()
            calls, paths = [], {}
            def acquire(argv, *, cwd, env):
                calls.append(argv)
                if argv[:2] == ['git', 'clone']:
                    (root / 'source').mkdir()
                    ca.source(root / 'source')
                elif argv[:2] == ['git', 'rev-parse']:
                    return installer.HERMES_COMMIT
                elif argv[-1] == installer.HERMES_TARGET_SCRIPT:
                    return ca.target
                elif argv[1:3] == ['-c', installer.HERMES_STAMP_SCRIPT]:
                    ca.ca.verify_inputs(cwd, ca.policy, 'after')
                    fixture_stamp(cwd)
                elif argv[-1] == installer.HERMES_CA_PROBE_SCRIPT:
                    return json.dumps(paths)
                elif argv[1:3] == ['-m', 'pm.cli']:
                    ca.ca.verify_inputs(cwd, ca.policy, 'after')
                    source = root / 'source'
                    store = root / 'installs' / hashlib.sha256(str(source.resolve()).encode()).hexdigest()[:16]
                    venv = store / 'environments/fixture'
                    (venv / 'bin').mkdir(parents=True, exist_ok=True)
                    for name in ('hermes', 'python'):
                        file = venv / 'bin' / name
                        file.write_text('fixture')
                        file.chmod(0o700)
                    (venv / 'pyvenv.cfg').write_text('fixture')
                    paths.update(ca.material(root, source, venv))
                return ''
            with patch.object(installer, 'run', side_effect=acquire):
                details = installer.install_hermes(ROOT, root, '/fixture/python')
            self.assertEqual(details['variant'], 'plain')
            self.assertEqual(details['thin_config_contract'], 1)
            self.assertIn('-ca1-', installer.generation_id(ROOT, 'hermes'))
            self.assertEqual(details['ca_recipe'], ca.ca.load(ROOT)[2])
            self.assertFalse(any('patch' in arg for argv in calls for arg in argv))
            self.assertEqual([argv[3:] for argv in calls if argv[1:3] == ['-m', 'pm.cli']],
                [['install', '--without', 'agent-browser', '--without', 'cua-driver'], ['install', '--extra', 'anthropic']])
            self.assertNotIn('provider_request_limit', json.dumps(details))
            with patch.object(installer, 'run', side_effect=acquire):
                installer.validate(ROOT, 'hermes', {'harness': 'hermes', 'complete': True, 'details': details})

    def test_existing_owned_v2_is_never_adopted_or_changed_on_ca_build_failure(self):
        for failure in (None, 'stamp', 'source'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as temporary:
                install = Path(temporary) / 'product'
                old = install / 'harnesses/hermes' / (installer.HERMES_COMMIT[:16] + '-v2')
                source = old / 'source'
                source.mkdir(parents=True)
                receipt = {'harness': 'hermes', 'complete': True, 'details': {
                    'hermes_source': str(source), 'managed_root': str(old),
                    'hermes_executable': str(old / 'bin/hermes'), 'old_state': failure}}
                installer.save(old / installer.OWNER, receipt)
                original = (old / installer.OWNER).read_bytes()
                with patch.object(installer, 'validate') as validate, patch.object(installer, 'run') as run, \
                     patch.object(installer, 'install_hermes', side_effect=ValueError('new CA build failed')) as acquire:
                    with self.assertRaisesRegex(ValueError, 'new CA build failed'):
                        installer.provision(ROOT, install, 'hermes', choice='enable')
                    acquire.assert_called_once()
                    validate.assert_not_called()
                    run.assert_not_called()
                self.assertEqual((old / installer.OWNER).read_bytes(), original)
                self.assertFalse((install / 'harnesses/hermes/current.json').exists())


if __name__ == '__main__':
    unittest.main()
