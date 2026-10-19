"""Offline installer ownership/opt-in tests: synthetic acquisition, no services."""
import importlib.util
import base64
import hashlib
import io
import json
import os
import re
import shlex
from pathlib import Path
import subprocess
import tempfile
import tarfile
import unittest
import sys
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location('harness_install', ROOT / 'scripts/lib/managed_harness_install.py')
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.install = self.home / '.agentstack'
        self.enterContext(patch.object(installer, 'run', return_value=installer.HERMES_COMMIT))

    def generation(self, harness):
        return self.install / 'harnesses' / harness / installer.generation_id(ROOT, harness)

    def write_product_manifest(self):
        text = (ROOT / 'scripts/install.sh').read_text()
        function = re.search(r'^write_manifest\(\) \{\n.*?(?=^main\(\) \{)', text, re.M | re.S).group(0)
        values = {name: '' for name in re.findall(r'\$\{?([A-Z][A-Z_]+)', function)}
        values.update(INSTALL_DIR=str(self.install), MANIFEST=str(self.install / 'install-state.json'),
            REPO_ROOT=str(ROOT), PYTHON_BIN=sys.executable, DRY_RUN='false',
            AGENT_MAIL_NAME_CAPABILITY_JSON='{}', ENV_FILE=str(self.install / 'env.sh'),
            SAFE_MERGE_RESULT_FILE=str(self.home / 'absent-settings.json'),
            MCP_MERGE_RESULT_FILE=str(self.home / 'absent-mcp.json'))
        script = 'set -euo pipefail\nplan() { :; }\n' + '\n'.join(name + '=' + shlex.quote(value) for name, value in values.items())
        result = subprocess.run(['/bin/bash', '-c', script + '\n' + function + '\nwrite_manifest manual ""\n'],
            env={'HOME': str(self.home), 'PATH': os.defpath}, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return installer.read(self.install / 'install-state.json')

    def fake_acquire(self, repo, root, *args):
        if root.parent.name == 'dsh':
            node = self.home / 'node'
            node.write_text('synthetic pinned executable')
            node.chmod(0o700)
            runtime = root / 'runtime'
            installer.save(root / 'evidence/node-runtime.json', {'sha256': hashlib.sha256(node.read_bytes()).hexdigest()})
            for item in installer.read(ROOT / 'integrations/dsh/runtime-pins.json'):
                installer.save(runtime / 'node_modules' / item['name'] / 'package.json', {'version': item['version']})
            recipe = ROOT / 'integrations/dsh/tui-managed.patch'
            changes = []
            for name in {r['file'] for r in installer.read(recipe)['replacements']}:
                file = runtime / 'node_modules/@deepseek-harness-tui/dsh-tui' / name
                file.parent.mkdir(parents=True, exist_ok=True)
                file.write_text('synthetic patched file')
                changes.append({'file': name, 'after': hashlib.sha256(file.read_bytes()).hexdigest()})
            receipt = root / 'evidence/tui-patch-applied.json'
            installer.save(receipt, {'recipe_sha256': hashlib.sha256(recipe.read_bytes()).hexdigest(), 'changes': changes})
            return {'node': str(node), 'runtime': str(runtime), 'patch_receipt': str(receipt)}
        source = root / 'source'
        source.mkdir()
        key = hashlib.sha256(str(source.resolve()).encode()).hexdigest()[:16]
        store = root / 'installs' / key
        generation = store / 'environments/fixture'
        for name in ('hermes', 'python'):
            file = generation / 'bin' / name
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text('synthetic executable')
            file.chmod(0o700)
        installer.save(store / 'facts.json', {'packages': {'venv': {'environment': str(generation)}}})
        (generation / 'pyvenv.cfg').write_text('synthetic venv')
        return {'hermes_source': str(source), 'managed_root': str(root), 'hermes_executable': str(generation / 'bin/hermes')}

    def test_default_and_dry_run_do_not_acquire_or_copy(self):
        with patch.object(installer, 'install_dsh') as dsh, patch.object(installer, 'install_hermes') as hermes:
            for harness in ('dsh', 'hermes'):
                installer.provision(ROOT, self.install, harness)
                installer.provision(ROOT, self.install, harness, choice='enable', dry_run=True)
            self.assertFalse(self.install.exists())
            dsh.assert_not_called()
            hermes.assert_not_called()

    def test_separate_opt_in_keeps_manual_home_and_uninstalls_owned_roots(self):
        manual = self.install / 'hermes-managed/profiles/manual'
        manual.mkdir(parents=True)
        sentinel = manual / 'memory.txt'
        sentinel.write_text('manual home')
        with patch.object(installer, 'install_dsh', side_effect=self.fake_acquire), patch.object(installer, 'install_hermes', side_effect=self.fake_acquire):
            installer.provision(ROOT, self.install, 'dsh', choice='enable')
            self.assertFalse(self.generation('hermes').exists())
            installer.provision(ROOT, self.install, 'hermes', choice='enable')
        roots = [self.generation(h) for h in ('dsh', 'hermes')]
        profile = self.install / 'harnesses/hermes/profiles/new/memory.txt'
        profile.parent.mkdir(parents=True)
        profile.write_text('new user memory')
        for harness in ('dsh', 'hermes'):
            self.assertTrue(list((self.install / 'integrations' / harness).glob('*.py')))
        manifest = self.write_product_manifest()
        self.assertEqual(set(manifest['managed_harness_roots']), {str(p) for p in roots})
        env = {'HOME': str(self.home), 'PATH': os.defpath}
        argv = ['bash', str(ROOT / 'scripts/uninstall.sh'), '--install-dir', str(self.install)]
        check = subprocess.run(argv + ['--dry-run'], env=env, capture_output=True, text=True)
        self.assertEqual(check.returncode, 0, check.stderr)
        self.assertTrue(all(p.exists() for p in roots))
        check = subprocess.run(argv, env=env, capture_output=True, text=True)
        self.assertEqual(check.returncode, 0, check.stderr)
        self.assertTrue(all(not p.exists() for p in roots))
        self.assertEqual(profile.read_text(), 'new user memory')
        self.assertEqual(sentinel.read_text(), 'manual home')

    def test_fresh_empty_install_uninstall_leaves_no_harness_artifacts(self):
        # The same real uninstall entry point can remove empty scaffolding.
        with patch.object(installer, 'install_dsh', side_effect=self.fake_acquire):
            installer.provision(ROOT, self.install, 'dsh', choice='enable')
        root = self.generation('dsh')
        manifest = self.write_product_manifest()
        self.assertEqual(manifest['managed_harness_roots'], [str(root)])
        result = subprocess.run(['bash', str(ROOT / 'scripts/uninstall.sh'), '--install-dir', str(self.install)],
                                env={'HOME': str(self.home), 'PATH': os.defpath}, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.install.exists())

    def test_persisted_enabled_disable_and_ownership_collision(self):
        with patch.object(installer, 'install_dsh', side_effect=self.fake_acquire) as acquire:
            installer.save(self.install / 'dashboard/dsh-provider.json', {'enabled': True})
            installer.provision(ROOT, self.install, 'dsh')
            self.assertEqual(acquire.call_count, 1)
            installer.provision(ROOT, self.install, 'dsh')
            self.assertEqual(acquire.call_count, 1)
            installer.provision(ROOT, self.install, 'dsh', choice='disable')
            installer.provision(ROOT, self.install, 'dsh')
            self.assertEqual(acquire.call_count, 1)
        self.generation('hermes').mkdir(parents=True)
        with self.assertRaisesRegex(ValueError, 'without installer ownership'):
            installer.provision(ROOT, self.install, 'hermes', choice='enable')

    def test_failed_acquisition_is_not_enabled_and_new_root_is_removed(self):
        with patch.object(installer, 'install_hermes', side_effect=ValueError('fixture failure')):
            with self.assertRaisesRegex(ValueError, 'fixture failure'):
                installer.provision(ROOT, self.install, 'hermes', choice='enable')
        self.assertFalse(self.generation('hermes').exists())
        self.assertFalse((self.install / 'dashboard/hermes-provider.json').exists())

    def test_clean_env_has_no_provider_keys_or_real_home(self):
        root = self.home / 'private'
        root.mkdir()
        with patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'dummy-do-not-copy', 'DEEPSEEK_API_KEY': 'dummy'}):
            env = installer.environment(root)
        self.assertNotIn('ANTHROPIC_API_KEY', env)
        self.assertNotIn('DEEPSEEK_API_KEY', env)
        self.assertEqual(env['HERMES_HOME'], str(root))
        self.assertTrue(Path(env['HOME']).is_relative_to(root))

    def test_real_shell_entry_default_dry_run_and_disabled_on_system_bash(self):
        text = (ROOT / 'scripts/install.sh').read_text()
        function = re.search(r'^install_managed_harnesses\(\) \{\n.*?^\}\n', text, re.M | re.S).group(0)
        script = ('set -euo pipefail\nREPO_ROOT=$1; SCRIPT_DIR=$1/scripts; INSTALL_DIR=$2; PYTHON_BIN=$3; '
                  'DRY_RUN=$4; DSH_OPT_IN=$5; HERMES_OPT_IN=$6; DSH_NODE=""; HARNESS_CACHE=""\n' + function + '\ninstall_managed_harnesses\n')
        import sys
        for dry, choice in [('false', ''), ('true', 'enable'), ('false', 'disable')]:
            result = subprocess.run(['/bin/bash', '-c', script, 'fixture', str(ROOT), str(self.install),
                sys.executable, dry, choice, choice], env={'HOME': str(self.home), 'PATH': os.defpath}, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            if choice != 'disable':
                self.assertFalse(self.install.exists())


class AcquisitionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.repo = self.base / 'repo'
        self.root = self.base / 'generation'
        self.root.mkdir()
        self.cache = self.base / 'cache'
        self.cache.mkdir()
        self.node = self.base / 'node/bin/node'
        self.node.parent.mkdir(parents=True)
        self.node.write_text('synthetic node executable')
        self.node.chmod(0o700)
        self.calls = []
        self.version = 'v24.3.0'
        self.exec_path = str(self.node)
        self.commit = installer.HERMES_COMMIT
        self.bad_patch = False
        pins = installer.read(ROOT / 'integrations/dsh/runtime-pins.json')
        for item in pins:
            data = ('synthetic tarball ' + item['name']).encode()
            if item['name'] == '@deepseek-harness-tui/dsh-tui':
                output = io.BytesIO()
                with tarfile.open(fileobj=output, mode='w:gz') as archive:
                    files = {'package/package.json': json.dumps({'version': item['version']}),
                             'package/lib/probe.js': 'old-one\nold-two\n',
                             'package/node_modules/bundled/package.json': '{"version":"bundled-original"}'}
                    for name, content in files.items():
                        member = tarfile.TarInfo(name)
                        member.size = len(content.encode())
                        archive.addfile(member, io.BytesIO(content.encode()))
                data = output.getvalue()
            (self.cache / item['file']).write_bytes(data)
            item['integrity'] = 'sha512-' + base64.b64encode(hashlib.sha512(data).digest()).decode()
        installer.save(self.repo / 'integrations/dsh/runtime-pins.json', pins)
        installer.save(self.repo / 'integrations/dsh/runtime-package-lock.json', {'packages': {'': {'dependencies': {'fixture': 'file:../evidence/package-0.tgz'}}}})
        installer.save(self.repo / 'integrations/dsh/ui-package.json', {'dependencies': {'bundled': '1.0.0', 'missing': '1.0.0'}})
        installer.save(self.repo / 'integrations/dsh/ui-package-lock.json', {'lockfileVersion': 3})
        installer.save(self.repo / 'integrations/dsh/tui-managed.patch', {'replacements': [
            {'file': 'lib/probe.js', 'old': 'old-one', 'new': 'new-one'},
            {'file': 'lib/probe.js', 'old': 'old-two', 'new': 'new-two'}]})

    def fake_run(self, argv, *, cwd, env):
        self.calls.append((argv, Path(cwd), dict(env)))
        self.assertNotIn('DEEPSEEK_API_KEY', env)
        self.assertNotIn('ANTHROPIC_API_KEY', env)
        if '-p' in argv:
            return json.dumps({'version': self.version, 'execPath': self.exec_path})
        if 'ci' in argv:
            self.assertIn('--ignore-scripts', argv)
            self.assertIn('--offline', argv)
            self.assertIn('--fetch-retries=0', argv)
            if Path(cwd).name == 'runtime':
                for item in installer.read(self.repo / 'integrations/dsh/runtime-pins.json'):
                    if item['name'] == '@deepseek-harness-tui/dsh-tui':
                        continue
                    installer.save(Path(cwd) / 'node_modules' / item['name'] / 'package.json', {'version': item['version']})
                if self.bad_patch:
                    recipe = self.repo / 'integrations/dsh/tui-managed.patch'
                    rows = installer.read(recipe)
                    rows['replacements'][0]['old'] = 'wrong preimage'
                    installer.save(recipe, rows)
            else:
                for name in ('bundled', 'missing'):
                    installer.save(Path(cwd) / 'node_modules' / name / 'package.json', {'version': '1.0.0'})
            return ''
        if argv[:2] == ['git', 'clone']:
            Path(argv[-1]).mkdir()
            return ''
        if argv[:2] == ['git', 'checkout']:
            self.assertEqual(argv[-1], installer.HERMES_COMMIT)
            return ''
        if argv[:2] == ['git', 'rev-parse']:
            return self.commit
        self.assertEqual(argv[1:], ['-m', 'pm.cli', 'install', '--without', 'agent-browser',
                                    '--without', 'cua-driver', '--extra', 'anthropic'])
        self.assertEqual(env['HERMES_HOME'], str(self.root))
        key = hashlib.sha256(str(Path(cwd).resolve()).encode()).hexdigest()[:16]
        store = self.root / 'installs' / key
        generation = store / 'environments/fixture'
        for name in ('python', 'hermes'):
            file = generation / 'bin' / name
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text('synthetic executable')
            file.chmod(0o700)
        installer.save(store / 'facts.json', {'packages': {'venv': {'environment': str(generation)}}})
        (generation / 'pyvenv.cfg').write_text('synthetic venv')
        return ''

    def dsh(self):
        with patch.object(installer, 'run', side_effect=self.fake_run), patch.object(installer.urllib.request, 'urlopen', side_effect=AssertionError('network not allowed')):
            return installer.install_dsh(self.repo, self.root, str(self.node), self.cache)

    def test_dsh_cached_integrity_node_npm_bundle_patch_and_revalidation(self):
        details = self.dsh()
        installer.validate(self.repo, 'dsh', {'harness': 'dsh', 'complete': True, 'details': details})
        tui = self.root / 'runtime/node_modules/@deepseek-harness-tui/dsh-tui'
        self.assertEqual(installer.read(tui / 'node_modules/bundled/package.json')['version'], 'bundled-original')
        self.assertTrue((tui / 'node_modules/missing').is_symlink())
        self.assertEqual((tui / 'lib/probe.js').read_text(), 'new-one\nnew-two\n')
        self.assertEqual(len(installer.read(details['patch_receipt'])['changes']), 1)
        (tui / 'lib/probe.js').write_text('ordinary damaged patch')
        with self.assertRaisesRegex(ValueError, 'patch changed'):
            installer.validate(self.repo, 'dsh', {'harness': 'dsh', 'complete': True, 'details': details})

    def test_sha512_mismatch_stops_before_npm(self):
        (self.cache / 'package-0.tgz').write_bytes(b'wrong tarball')
        with self.assertRaisesRegex(ValueError, 'sha512 mismatch'):
            self.dsh()
        self.assertEqual(len(self.calls), 1)

    def test_wrong_node_version_and_exec_path_stop_before_acquisition(self):
        for version, executable in [('v22.17.0', str(self.node)), ('v24.3.0', '/different/node')]:
            self.version, self.exec_path = version, executable
            with self.assertRaisesRegex(ValueError, 'version/execPath'):
                self.dsh()
            self.assertFalse((self.root / 'evidence').exists())

    def test_wrong_patch_preimage_is_failure(self):
        self.bad_patch = True
        with self.assertRaisesRegex(ValueError, 'preimage mismatch'):
            self.dsh()
        self.assertFalse((self.root / 'evidence/tui-patch-applied.json').exists())

    def test_hermes_pin_pm_arguments_settings_and_runtime(self):
        with patch.object(installer, 'run', side_effect=self.fake_run):
            details = installer.install_hermes(self.repo, self.root, '/fixed/python', self.cache)
            installer.validate(self.repo, 'hermes', {'harness': 'hermes', 'complete': True, 'details': details})
        config = installer.read(self.root / 'config.yaml')
        self.assertFalse(config['auth']['adopt_external_logins'])
        self.assertFalse(config['updates']['check'])
        self.assertEqual(config['telemetry']['shared_metrics'], {'enabled': False, 'send': False})
        self.assertFalse((self.root / '.env').exists())
        self.assertFalse((self.root / 'source/.env').exists())

    def test_hermes_wrong_commit_stops_before_pm(self):
        self.commit = 'wrong-pin'
        with patch.object(installer, 'run', side_effect=self.fake_run):
            with self.assertRaisesRegex(ValueError, 'commit mismatch'):
                installer.install_hermes(self.repo, self.root, '/fixed/python', self.cache)
        self.assertEqual(len(self.calls), 3)


class CatalogGateTests(unittest.TestCase):
    def test_independent_harness_opt_in_and_legacy_manifest(self):
        from dashboard import managed_spawn, managed_catalog
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for selected in ('dsh', 'hermes', 'neither', 'legacy'):
                for harness in ('dsh', 'hermes'):
                    file = root / 'dashboard' / (harness + '-provider.json')
                    if selected == 'legacy':
                        file.unlink(missing_ok=True)
                    else:
                        installer.save(file, {'enabled': harness == selected})
                metadata = {backend: {'model': 'fixture'} for backend in ('strata', 'deepseek')}
                readiness = {backend: {h: {'available': True} for h in ('dsh', 'hermes')} for backend in metadata}
                module = SimpleNamespace(load_catalog_inputs=lambda *args, **kw: (metadata, readiness, True),
                                         catalog_providers=managed_catalog.catalog_providers)
                with patch.object(managed_spawn, 'ROOT', root), patch.object(managed_spawn.importlib, 'import_module', return_value=module):
                    self.assertEqual(managed_spawn.catalog(None, selected_mode='legacy'), [])
                    rows = managed_spawn.catalog('explicit-private-manifest', selected_mode='legacy')
                for provider in rows:
                    statuses = {h['id']: h for h in provider['models'][0]['harnesses']}
                    for harness in ('dsh', 'hermes'):
                        expected = selected == 'legacy' or selected == harness
                        self.assertEqual(statuses[harness]['available'], expected, (selected, harness))
                        if not expected:
                            self.assertEqual(statuses[harness]['reason'], 'disabled_by_opt_in')


if __name__ == '__main__':
    unittest.main()
