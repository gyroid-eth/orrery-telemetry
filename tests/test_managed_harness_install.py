"""Offline installer ownership/opt-in tests: synthetic acquisition, no services."""
import importlib.util
from contextlib import ExitStack
import copy
import difflib
import base64
import hashlib
import io
import json
import os
import re
import shlex
import shutil
from pathlib import Path
import subprocess
import tempfile
import tarfile
import unittest
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location('harness_install', ROOT / 'scripts/lib/managed_harness_install.py')
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


def fixture_stamp(source):
    installer.save(Path(source) / 'install-stamp.json', {'commit': installer.HERMES_COMMIT, 'source': 'local',
        'updateMechanism': 'external', 'payload': 'bootstrap', 'dirty': True,
        'baseVersion': '0.1.0', 'displayVersion': '0.1.0'})
    return installer.hermes_stamp_details(source)


class CAFixture:
    """Synthetic trust inputs; real recipe/patch/evidence code, no package calls."""
    def __init__(self, module, directory):
        self.stack = ExitStack()
        self.repo = Path(directory) / 'ca-fixture-repo'
        for name in ('hermes', 'dsh'):
            shutil.copytree(ROOT / 'integrations' / name, self.repo / 'integrations' / name)
        (self.repo / 'hooks').mkdir()
        shutil.copy2(ROOT / 'hooks/native_connections.py', self.repo / 'hooks/native_connections.py')
        shutil.copytree(ROOT / 'examples/native-harnesses', self.repo / 'examples/native-harnesses')
        target = self.repo / 'scripts/lib/managed_harness_install.py'
        target.parent.mkdir(parents=True)
        shutil.copy2(ROOT / 'scripts/lib/managed_harness_install.py', target)
        original_module = module or installer
        self.ca = original_module.hermes_ca()
        policy, _, _ = self.ca.load(ROOT)
        policy = copy.deepcopy(policy)
        self.bundle, self.uv = b'synthetic fixed CA bundle', b'synthetic fixed uv'
        policy['certifi']['cacert_sha256'] = hashlib.sha256(self.bundle).hexdigest()
        for item in policy['uv_artifacts'].values():
            item['executable_sha256'] = hashlib.sha256(self.uv).hexdigest()
        self.target = 'darwin-arm64'
        def inline(value):
            if isinstance(value, dict):
                return '{' + ','.join(json.dumps(k) + '=' + inline(v) for k, v in value.items()) + '}'
            if isinstance(value, list):
                return '[' + ','.join(inline(v) for v in value) + ']'
            return json.dumps(value)
        current = '[[package]]\n' + '\n'.join(k + '=' + inline(v) for k, v in policy['certifi']['lock_record'].items()) + '\n'
        new_uv = {'packages': {'uv': {'version': '0.13.5', 'artifacts': {
            name: {k: item[k] for k in ('url', 'sha256')} for name, item in policy['uv_artifacts'].items()}}}}
        after = {'pyproject.toml': '[project]\nname="fixture"\ndependencies=["certifi==2026.10.19"]\n',
                 'uv.lock': current, 'pm/lock.json': json.dumps(new_uv, indent=2) + '\n'}
        self.before = {name: value.replace('2026.10.19', '2026.5.20').replace('0.13.5', '0.12.3') for name, value in after.items()}
        patch_text = ''.join(''.join(difflib.unified_diff(self.before[name].splitlines(keepends=True), value.splitlines(keepends=True),
                            fromfile='a/' + name, tofile='b/' + name)) for name, value in after.items())
        policy['inputs'] = {name: {'before': hashlib.sha256(self.before[name].encode()).hexdigest(),
                                 'after': hashlib.sha256(value.encode()).hexdigest()} for name, value in after.items()}
        policy['patch_sha256'] = hashlib.sha256(patch_text.encode()).hexdigest()
        installer.save(self.repo / 'integrations/hermes/ca-inputs.json', policy)
        (self.repo / 'integrations/hermes/ca-inputs.patch').write_text(patch_text)
        self.policy = policy
        actual_load = self.ca.load
        self.stack.enter_context(patch.object(self.ca, 'load', side_effect=lambda repo: actual_load(self.repo)))
        if module:
            self.stack.enter_context(patch.object(module, 'hermes_ca', return_value=self.ca))

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.stack.close()

    def source(self, source):
        for name, text in self.before.items():
            target = Path(source) / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text)

    def material(self, root, source, generation):
        root, source, generation = Path(root), Path(source), Path(generation)
        bundle = generation / 'lib/site-packages/certifi/cacert.pem'
        metadata = generation / 'lib/site-packages/certifi-2026.10.19.dist-info/METADATA'
        bundle.parent.mkdir(parents=True, exist_ok=True)
        metadata.parent.mkdir(parents=True, exist_ok=True)
        bundle.write_bytes(self.bundle)
        metadata.write_text('Name: certifi\nVersion: 2026.10.19\n')
        resolved = generation.parent / 'workspace/uv.lock'
        resolved.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source / 'uv.lock', resolved)
        store = root / 'installs' / hashlib.sha256(str(source.resolve()).encode()).hexdigest()[:16]
        facts = installer.read(store / 'facts.json') if (store / 'facts.json').exists() else {'packages': {}}
        facts['packages'].setdefault('venv', {}).update(environment=str(generation), resolved_lock=str(resolved))
        installer.save(store / 'facts.json', facts)
        item = self.policy['uv_artifacts'][self.target]
        binary = root / 'tools/uv-fixture/uv'
        binary.parent.mkdir(parents=True, exist_ok=True)
        binary.write_bytes(self.uv)
        installer.save(root / 'tools/facts.json', {'packages': {'uv': {'entry': 'uv-fixture', 'target': self.target,
            'version': '0.13.5', 'artifacts': [item['sha256']]}}})
        return {'bundle': str(bundle), 'metadata': str(metadata)}


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name).resolve()
        self.install = self.home / '.agentstack'
        self.ca = self.enterContext(CAFixture(installer, self.home))
        self.enterContext(patch.object(installer, 'run', return_value=installer.HERMES_COMMIT))

    def generation(self, harness):
        return self.install / 'harnesses' / harness / installer.generation_id(ROOT, harness)

    def test_hermes_ca_upgrade_acquires_new_generation_and_keeps_v2(self):
        old = self.install / 'harnesses/hermes' / (installer.HERMES_COMMIT[:16] + '-v2')
        old.mkdir(parents=True)
        details = self.fake_acquire(ROOT, old)
        installer.save(old / installer.OWNER, {'harness': 'hermes', 'complete': True,
            'variant': 'plain', 'thin_config_contract': 1, 'details': details})
        original = (old / installer.OWNER).read_bytes()
        profile = old.parent / 'profiles/keep/memory.txt'
        profile.parent.mkdir(parents=True)
        profile.write_text('existing memory')
        with patch.object(installer, 'install_hermes', side_effect=self.fake_acquire) as acquire:
            installer.provision(ROOT, self.install, 'hermes', choice='enable')
        acquire.assert_called_once()
        self.assertNotEqual(self.generation('hermes'), old)
        self.assertEqual((old / installer.OWNER).read_bytes(), original)
        self.assertEqual(profile.read_text(), 'existing memory')

    def write_product_manifest(self):
        text = (ROOT / 'scripts/install.sh').read_text()
        function = re.search(r'^write_manifest\(\) \{\n.*?(?=^main\(\) \{)', text, re.M | re.S).group(0)
        values = {name: '' for name in re.findall(r'\$\{?([A-Z][A-Z_]+)', function)}
        values.update(INSTALL_DIR=str(self.install), MANIFEST=str(self.install / 'install-state.json'),
            REPO_ROOT=str(self.ca.repo), SCRIPT_DIR=str(ROOT / 'scripts'), PYTHON_BIN=sys.executable, DRY_RUN='false',
            AGENT_MAIL_NAME_CAPABILITY_JSON='{}', ENV_FILE=str(self.install / 'env.sh'),
            SAFE_MERGE_RESULT_FILE=str(self.home / 'absent-settings.json'),
            MCP_MERGE_RESULT_FILE=str(self.home / 'absent-mcp.json'))
        # provision() uses the parent process's run stub. The real manifest
        # writer validates the same synthetic owned source in a new process.
        tools = self.home / 'writer-tools'
        tools.mkdir(exist_ok=True)
        git = tools / 'git'
        git.write_text('#!/bin/sh\nif [ "$#" -eq 2 ] && [ "$1" = rev-parse ] && [ "$2" = HEAD ]; then\n'
                       '  printf "%s\\n" ' + shlex.quote(installer.HERMES_COMMIT) + '\nelse exit 2; fi\n')
        git.chmod(0o700)
        # The validator intentionally resets PATH to os.defpath. Isolate that
        # default inside this fixture process, not in the production validator.
        python = tools / 'fixture-python'
        code = ('import os,sys;os.defpath=' + repr(str(tools)) + '+os.pathsep+os.defpath;'
                'sys.argv=sys.argv[1:];exec(compile(sys.stdin.read(),"<manifest-fixture>","exec"))')
        python.write_text('#!/bin/sh\nexec ' + shlex.quote(sys.executable) + ' -B -c ' + shlex.quote(code) + ' "$@"\n')
        python.chmod(0o700)
        values['PYTHON_BIN'] = str(python)
        script = 'set -euo pipefail\nplan() { :; }\n' + '\n'.join(name + '=' + shlex.quote(value) for name, value in values.items())
        result = subprocess.run(['/bin/bash', '-c', script + '\n' + function + '\nwrite_manifest manual ""\n'],
            env={'HOME': str(self.home), 'PATH': str(tools) + os.pathsep + os.defpath}, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return installer.read(self.install / 'install-state.json')

    def fake_acquire(self, repo, root, *args, **kwargs):
        if root.parent.name == 'dsh':
            node = self.home / 'node'
            node.write_text('synthetic pinned executable')
            node.chmod(0o700)
            runtime = root / 'runtime'
            installer.save(root / 'evidence/node-runtime.json', {'sha256': hashlib.sha256(node.read_bytes()).hexdigest()})
            for item in installer.read(ROOT / 'integrations/dsh/runtime-pins.json'):
                installer.save(runtime / 'node_modules' / item['name'] / 'package.json', {'version': item['version']})
            files = []
            for name in installer.UPSTREAM_PROOF_FILES:
                file = runtime / 'node_modules/@deepseek-harness-tui/dsh-tui' / name
                file.parent.mkdir(parents=True, exist_ok=True)
                file.write_text('synthetic original upstream')
                files.append({'file': name, 'sha256': hashlib.sha256(file.read_bytes()).hexdigest()})
            receipt = root / 'evidence/upstream-runtime.json'
            variant = 'plain-datadir' if kwargs.get('datadir') else 'plain'
            installer.save(receipt, {'variant': variant, 'source_patch': 'tui-datadir-v1' if kwargs.get('datadir') else None,
                'recipe_sha256': hashlib.sha256((ROOT / 'integrations/dsh/tui-datadir.patch.json').read_bytes()).hexdigest(), 'files': files})
            return {'node': str(node), 'runtime': str(runtime), 'runtime_receipt': str(receipt),
                    'variant': variant, 'thin_config_contract': 1}
        source = root / 'source'
        source.mkdir()
        self.ca.source(source)
        self.ca.ca.apply(ROOT, source)
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
        paths = self.ca.material(root, source, generation)
        stamp = fixture_stamp(source)
        proof = self.ca.ca.runtime_evidence(ROOT, root, source, generation, self.ca.target, paths)
        return {'hermes_source': str(source), 'managed_root': str(root), 'hermes_executable': str(generation / 'bin/hermes'),
                'variant': 'plain', 'thin_config_contract': 1,
                'ca_recipe': self.ca.ca.load(ROOT)[2], 'ca_evidence': proof, **stamp}

    def test_default_and_dry_run_do_not_acquire_or_copy(self):
        with patch.object(installer, 'install_dsh') as dsh, patch.object(installer, 'install_hermes') as hermes:
            for harness in ('dsh', 'hermes'):
                installer.provision(ROOT, self.install, harness)
                installer.provision(ROOT, self.install, harness, choice='enable', dry_run=True)
            self.assertFalse(self.install.exists())
            dsh.assert_not_called()
            hermes.assert_not_called()

    def test_ca_reuse_refuses_changed_bundle_without_republishing(self):
        with patch.object(installer, 'install_hermes', side_effect=self.fake_acquire):
            installer.provision(ROOT, self.install, 'hermes', choice='enable')
        pointer = self.install / 'harnesses/hermes/current.json'
        original = pointer.read_bytes()
        current = installer.read(pointer)
        Path(current['ca_evidence']['bundle']).write_text('old or modified bundle')
        with patch.object(installer, 'install_hermes', side_effect=AssertionError('reuse reacquired')):
            with self.assertRaisesRegex(ValueError, 'bundle/version mismatch'):
                installer.provision(ROOT, self.install, 'hermes', choice='keep')
        self.assertEqual(pointer.read_bytes(), original)

    def test_plain_dsh_generation_keeps_existing_managed_generation(self):
        old = self.install / 'harnesses/dsh' / 'legacy-managed-generation'
        old.mkdir(parents=True)
        sentinel = old / 'keep.txt'
        sentinel.write_text('existing managed runtime')
        with patch.object(installer, 'install_dsh', side_effect=self.fake_acquire):
            installer.provision(ROOT, self.install, 'dsh', choice='enable')
        self.assertEqual(sentinel.read_text(), 'existing managed runtime')
        current = installer.read(self.install / 'harnesses/dsh/current.json')
        receipt = installer.read(current['generation_receipt'])
        self.assertEqual(receipt['variant'], 'plain-datadir')
        self.assertEqual(receipt['thin_config_contract'], 1)
        self.assertEqual(receipt['details']['generation_receipt'], current['generation_receipt'])
        self.assertNotEqual(Path(current['runtime']).parent, old)

    def test_legacy_dsh_receipt_is_rejected_without_opening_or_modifying_runtime(self):
        receipt = {'harness': 'dsh', 'complete': True, 'details': {'patch_receipt': 'legacy-receipt'}}
        with patch.object(installer, 'read', side_effect=AssertionError('legacy runtime must remain untouched')):
            with self.assertRaisesRegex(ValueError, 'Legacy managed dsh runtime is unsupported'):
                installer.validate(ROOT, 'dsh', receipt)

    def test_stamped_v2_keeps_legacy_v1_and_persistent_profiles_unchanged(self):
        base = self.install / 'harnesses/hermes'
        old = base / (installer.HERMES_COMMIT[:16] + '-v1')
        legacy = old / 'source/legacy.txt'
        legacy.parent.mkdir(parents=True)
        legacy.write_text('unstamped v1 source')
        installer.save(old / installer.OWNER, {'harness': 'hermes', 'complete': True, 'details': {'legacy': True}})
        receipt = (old / installer.OWNER).read_bytes()
        memory = base / 'profiles/fixture/memories/keep.txt'
        memory.parent.mkdir(parents=True)
        memory.write_text('persistent memory')
        with patch.object(installer, 'install_hermes', side_effect=self.fake_acquire):
            installer.provision(ROOT, self.install, 'hermes', choice='enable')
        self.assertEqual(legacy.read_text(), 'unstamped v1 source')
        self.assertEqual((old / installer.OWNER).read_bytes(), receipt)
        self.assertFalse((old / 'source/install-stamp.json').exists())
        self.assertEqual(memory.read_text(), 'persistent memory')
        current = installer.read(base / 'current.json')
        self.assertEqual(Path(current['managed_root']).resolve(), self.generation('hermes').resolve())

    def test_enabled_update_refreshes_capabilities_but_off_dry_run_do_not(self):
        from dashboard import managed_capabilities
        # The installer imports this pure file by location; verify its actual
        # output under synthetic acquisition rather than touching user config.
        with patch.object(installer, 'install_dsh', side_effect=self.fake_acquire):
            installer.provision(ROOT, self.install, 'dsh', choice='enable')
            target = self.generation('dsh') / 'evidence/effort-capabilities.json'
            self.assertTrue(target.is_file())
            self.assertEqual(installer.read(target)['entries'], [])
            target.write_text('{"stale":true}')
            installer.provision(ROOT, self.install, 'dsh')
            self.assertNotIn('stale', installer.read(target))
            target.write_text('{"kept":true}')
            installer.provision(ROOT, self.install, 'dsh', choice='disable')
            self.assertEqual(installer.read(target), {'kept': True})
            installer.provision(ROOT, self.install, 'dsh', choice='enable', dry_run=True)
            self.assertEqual(installer.read(target), {'kept': True})

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
        self.base = Path(self.tmp.name).resolve()
        self.repo = self.base / 'repo'
        self.ca = self.enterContext(CAFixture(installer, self.base))
        shutil.copytree(self.ca.repo / 'integrations/hermes', self.repo / 'integrations/hermes')
        self.root = self.base / 'generation'
        self.root.mkdir()
        self.cache = self.base / 'cache'
        self.cache.mkdir()
        self.node = self.base / 'node/bin/node'
        self.node.parent.mkdir(parents=True)
        self.node.write_text('synthetic node executable')
        self.node.chmod(0o700)
        self.calls = []
        self.pm_inputs = []
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
                    files.update({'package/' + name: installer.read(ROOT / 'integrations/dsh/tui-datadir.patch.json')['old'] if name.endswith('utils/paths.js') else 'native original ' + name for name in installer.UPSTREAM_PROOF_FILES})
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
        installer.save(self.repo / 'integrations/dsh/tui-datadir.patch.json', installer.read(ROOT / 'integrations/dsh/tui-datadir.patch.json'))


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
                    recipe = self.repo / 'integrations/dsh/tui-datadir.patch.json'
                    rows = installer.read(recipe)
                    rows['old'] = 'wrong preimage'
                    installer.save(recipe, rows)
            else:
                for name in ('bundled', 'missing'):
                    installer.save(Path(cwd) / 'node_modules' / name / 'package.json', {'version': '1.0.0'})
            return ''
        if argv[:2] == ['git', 'clone']:
            Path(argv[-1]).mkdir()
            self.ca.source(Path(argv[-1]))
            return ''
        if argv[:2] == ['git', 'checkout']:
            self.assertEqual(argv[-1], installer.HERMES_COMMIT)
            return ''
        if argv[:2] == ['git', 'rev-parse']:
            return self.commit
        if argv[1:3] == ['-c', installer.HERMES_STAMP_SCRIPT]:
            self.assertEqual(argv[3], installer.HERMES_COMMIT)
            fixture_stamp(cwd)
            return ''
        if argv[-1] == installer.HERMES_TARGET_SCRIPT:
            return self.ca.target
        if argv[-1] == installer.HERMES_CA_PROBE_SCRIPT:
            return json.dumps(self.paths)
        # Fixed Hermes validates combinations after parsing: combining these
        # flags exits 1 on stdout even though argparse accepts each separately.
        if '--without' in argv and '--extra' in argv:
            raise subprocess.CalledProcessError(1, argv,
                output='--without shapes the default closure; it does not take names, --extra, or --target')
        self.assertIn(argv[1:], (['-m', 'pm.cli', 'install', '--without', 'agent-browser', '--without', 'cua-driver'],
                                ['-m', 'pm.cli', 'install', '--extra', 'anthropic']))
        self.pm_inputs.append(self.ca.ca.verify_inputs(cwd, self.ca.policy, 'after'))
        self.assertEqual(env['HERMES_HOME'], str(self.root))
        key = hashlib.sha256(str(Path(cwd).resolve()).encode()).hexdigest()[:16]
        store = self.root / 'installs' / key
        previous = store / 'facts.json'
        if '--extra' in argv:
            self.assertEqual(installer.read(previous)['packages']['venv']['extras'], ['all'])
        generation = store / ('environments/with-anthropic' if '--extra' in argv else 'environments/default')
        for name in ('python', 'hermes'):
            file = generation / 'bin' / name
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text('synthetic executable')
            file.chmod(0o700)
        installer.save(store / 'facts.json', {'packages': {'venv': {'environment': str(generation),
            'extras': ['all', 'anthropic'] if '--extra' in argv else ['all']}}})
        (generation / 'pyvenv.cfg').write_text('synthetic venv')
        self.paths = self.ca.material(self.root, Path(cwd), generation)
        return ''

    def dsh(self):
        with patch.object(installer, 'run', side_effect=self.fake_run), patch.object(installer.urllib.request, 'urlopen', side_effect=AssertionError('network not allowed')):
            return installer.install_dsh(self.repo, self.root, str(self.node), self.cache)

    def test_dsh_cached_integrity_node_npm_bundle_datadir_and_revalidation(self):
        details = self.dsh()
        receipt = {'harness': 'dsh', 'complete': True, 'variant': details['variant'],
                   'thin_config_contract': 1, 'details': details}
        installer.validate(self.repo, 'dsh', receipt)
        tui = self.root / 'runtime/node_modules/@deepseek-harness-tui/dsh-tui'
        self.assertEqual(installer.read(tui / 'node_modules/bundled/package.json')['version'], 'bundled-original')
        self.assertTrue((tui / 'node_modules/missing').is_symlink())
        self.assertEqual((tui / 'lib/probe.js').read_text(), 'old-one\nold-two\n')
        self.assertEqual(installer.read(details['runtime_receipt'])['source_patch'], 'tui-datadir-v1')
        (tui / installer.UPSTREAM_PROOF_FILES[0]).write_text('ordinary damaged source')
        with self.assertRaisesRegex(ValueError, 'upstream runtime changed'):
            installer.validate(self.repo, 'dsh', receipt)

    def test_plain_acquisition_preserves_upstream_and_uses_separate_receipt(self):
        with patch.object(installer, 'run', side_effect=self.fake_run), patch.object(installer.urllib.request, 'urlopen', side_effect=AssertionError('network not allowed')):
            details = installer.install_dsh(self.repo, self.root, str(self.node), self.cache, datadir=False)
        receipt = {'harness': 'dsh', 'complete': True, 'variant': 'plain', 'thin_config_contract': 1, 'details': details}
        installer.validate(self.repo, 'dsh', receipt)
        tui = self.root / 'runtime/node_modules/@deepseek-harness-tui/dsh-tui'
        self.assertEqual((tui / 'lib/probe.js').read_text(), 'old-one\nold-two\n')
        self.assertFalse((self.root / 'evidence/tui-patch-applied.json').exists())
        self.assertNotIn('patch_receipt', details)
        self.assertIsNone(installer.read(details['runtime_receipt'])['source_patch'])
        self.assertNotEqual(installer.generation_id(self.repo, 'dsh'), installer.generation_id(self.repo, 'dsh', datadir=False))
        # Losing or modifying an ordinary source file must not look ready.
        (tui / installer.UPSTREAM_PROOF_FILES[0]).write_text('changed')
        with self.assertRaisesRegex(ValueError, 'upstream runtime changed'):
            installer.validate(self.repo, 'dsh', receipt)

    def test_datadir_acquisition_is_one_line_and_preimage_is_required(self):
        with patch.object(installer, 'run', side_effect=self.fake_run):
            details = installer.install_dsh(self.repo, self.root, str(self.node), self.cache, datadir=True)
        receipt = {'harness': 'dsh', 'complete': True, 'variant': 'plain-datadir', 'thin_config_contract': 1, 'details': details}
        installer.validate(self.repo, 'dsh', receipt)
        recipe = installer.read(self.repo / 'integrations/dsh/tui-datadir.patch.json')
        tui = self.root / 'runtime/node_modules/@deepseek-harness-tui/dsh-tui'
        self.assertEqual((tui / recipe['file']).read_text(), recipe['new'])
        self.assertEqual((tui / 'lib/probe.js').read_text(), 'old-one\nold-two\n')
        self.assertNotEqual(installer.generation_id(self.repo, 'dsh'), installer.generation_id(self.repo, 'dsh', datadir=False))
        recipe['old'] = 'wrong native version'
        installer.save(self.repo / 'integrations/dsh/tui-datadir.patch.json', recipe)
        with self.assertRaisesRegex(ValueError, 'preimage mismatch'), patch.object(installer, 'run', side_effect=self.fake_run):
            second = self.base / 'second-generation'
            second.mkdir()
            installer.install_dsh(self.repo, second, str(self.node), self.cache, datadir=True)

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
        self.assertFalse((self.root / 'evidence/upstream-runtime.json').exists())

    def test_hermes_pin_pm_arguments_settings_and_runtime(self):
        with patch.object(installer, 'run', side_effect=self.fake_run):
            details = installer.install_hermes(self.repo, self.root, sys.executable, self.cache)
            installer.validate(self.repo, 'hermes', {'harness': 'hermes', 'complete': True, 'details': details})
        config = installer.read(self.root / 'config.yaml')
        self.assertFalse(config['auth']['adopt_external_logins'])
        self.assertFalse(config['updates']['check'])
        self.assertEqual(config['telemetry']['shared_metrics'], {'enabled': False, 'send': False})
        self.assertFalse((self.root / '.env').exists())
        self.assertFalse((self.root / 'source/.env').exists())
        commands = [argv for argv, _, _ in self.calls if argv[1:4] == ['-m', 'pm.cli', 'install']]
        self.assertEqual(commands, [
            [sys.executable, '-m', 'pm.cli', 'install', '--without', 'agent-browser', '--without', 'cua-driver'],
            [sys.executable, '-m', 'pm.cli', 'install', '--extra', 'anthropic']])
        self.assertIn('/environments/with-anthropic/', details['hermes_executable'])
        self.assertEqual(details['install_stamp'], str(self.root / 'source/install-stamp.json'))
        self.assertIn('-ca1-', installer.generation_id(self.repo, 'hermes'))
        self.assertTrue(installer.read(Path(details['install_stamp']))['dirty'])
        self.assertEqual(details['ca_recipe'], self.ca.ca.load(self.repo)[2])
        self.assertEqual(self.pm_inputs, [details['ca_evidence']['inputs']] * 2)
        stamp = Path(details['install_stamp'])
        stamp.write_text(stamp.read_text() + ' ')
        with patch.object(installer, 'run', return_value=installer.HERMES_COMMIT):
            with self.assertRaisesRegex(ValueError, 'stamp receipt changed'):
                installer.validate(self.repo, 'hermes', {'harness': 'hermes', 'complete': True, 'details': details})

    def test_hermes_wrong_commit_stops_before_pm(self):
        self.commit = 'wrong-pin'
        with patch.object(installer, 'run', side_effect=self.fake_run):
            with self.assertRaisesRegex(ValueError, 'commit mismatch'):
                installer.install_hermes(self.repo, self.root, '/fixed/python', self.cache)
        self.assertEqual(len(self.calls), 3)

    def test_ca_preimage_postimage_and_patch_digest_fail_before_writes(self):
        source = self.base / 'exact-source'
        self.ca.source(source)
        original = {name: (source / name).read_bytes() for name in self.ca.ca.FILES}
        (source / 'pyproject.toml').write_text('wrong original')
        with self.assertRaisesRegex(ValueError, 'input hash mismatch'):
            self.ca.ca.apply(self.repo, source)
        (source / 'pyproject.toml').write_bytes(original['pyproject.toml'])
        policy_file = self.ca.repo / 'integrations/hermes/ca-inputs.json'
        saved_policy = policy_file.read_bytes()
        policy = installer.read(policy_file)
        policy['inputs']['uv.lock']['after'] = '0' * 64
        installer.save(policy_file, policy)
        with self.assertRaisesRegex(ValueError, 'postimage mismatch'):
            self.ca.ca.apply(self.repo, source)
        self.assertEqual({name: (source / name).read_bytes() for name in original}, original)
        policy_file.write_bytes(saved_policy)
        recipe_patch = self.ca.repo / 'integrations/hermes/ca-inputs.patch'
        recipe_patch.write_text(recipe_patch.read_text() + '# changed\n')
        with self.assertRaisesRegex(ValueError, 'recipe identity mismatch'):
            self.ca.ca.apply(self.repo, source)
        self.assertEqual({name: (source / name).read_bytes() for name in original}, original)

    def test_ca_unsupported_target_stops_before_stamp_or_pm(self):
        self.ca.target = 'linux-arm64-bionic'
        with patch.object(installer, 'run', side_effect=self.fake_run):
            with self.assertRaisesRegex(ValueError, 'does not support this target'):
                installer.install_hermes(self.repo, self.root, '/fixed/python', self.cache)
        self.assertFalse(any(argv[1:3] == ['-m', 'pm.cli'] or argv[1:3] == ['-c', installer.HERMES_STAMP_SCRIPT]
                             for argv, _, _ in self.calls))

    def test_ca_failed_evidence_preserves_old_pointers_profiles_and_v2(self):
        for damage in ('bundle', 'metadata', 'uv', 'lock', 'inputs', 'stamp'):
            with self.subTest(damage=damage):
                install = self.base / ('damaged-' + damage)
                base = install / 'harnesses/hermes'
                self.root = base / installer.generation_id(self.repo, 'hermes')
                pointer = base / 'current.json'
                installer.save(pointer, {'generation': 'old-v2'})
                setting = install / 'dashboard/hermes-provider.json'
                installer.save(setting, {'enabled': True})
                profile = base / 'profiles/keep/memory.txt'
                profile.parent.mkdir(parents=True)
                profile.write_text('existing memory')
                old = base / (installer.HERMES_COMMIT[:16] + '-v2/keep.txt')
                old.parent.mkdir(parents=True)
                old.write_text('old generation')
                originals = {path: path.read_bytes() for path in (pointer, setting, profile, old)}
                def acquire(argv, **kwargs):
                    result = self.fake_run(argv, **kwargs)
                    if argv[-1] == installer.HERMES_CA_PROBE_SCRIPT:
                        source = Path(kwargs['cwd'])
                        if damage in ('bundle', 'metadata'):
                            Path(self.paths[damage]).write_text('stale')
                        elif damage == 'uv':
                            (self.root / 'tools/uv-fixture/uv').write_text('old uv')
                        elif damage == 'lock':
                            key = hashlib.sha256(str(source.resolve()).encode()).hexdigest()[:16]
                            fact = installer.read(self.root / 'installs' / key / 'facts.json')
                            Path(fact['packages']['venv']['resolved_lock']).write_text('package=[]\n')
                        elif damage == 'inputs':
                            (source / 'pyproject.toml').write_text('changed after PM')
                        else:
                            value = installer.read(source / 'install-stamp.json')
                            value['dirty'] = False
                            installer.save(source / 'install-stamp.json', value)
                    return result
                with patch.object(installer, 'run', side_effect=acquire):
                    with self.assertRaises(ValueError):
                        installer.provision(self.repo, install, 'hermes', choice='enable', cache=self.cache)
                self.assertFalse(self.root.exists())
                for path, content in originals.items():
                    self.assertEqual(path.read_bytes(), content)

    def test_hermes_each_pm_failure_stays_disabled_and_preserves_both_log_tails(self):
        for option, count in (('--without', 1), ('--extra', 2)):
            with self.subTest(option=option):
                install = self.base / ('install-' + option[2:])
                self.root = install / 'harnesses/hermes' / installer.generation_id(self.repo, 'hermes')
                self.calls.clear()
                def failing_run(argv, *, cwd, env):
                    if argv[1:4] == ['-m', 'pm.cli', 'install'] and option in argv:
                        self.calls.append((argv, Path(cwd), dict(env)))
                        self.assertNotIn('DEEPSEEK_API_KEY', env)
                        if option == '--extra':
                            key = hashlib.sha256(str(Path(cwd).resolve()).encode()).hexdigest()[:16]
                            self.assertTrue((self.root / 'installs' / key / 'facts.json').is_file())
                        raise subprocess.CalledProcessError(1, argv,
                            output='x' * 4500 + 'PM_STDOUT_FAILURE', stderr='PM_STDERR_FAILURE')
                    return self.fake_run(argv, cwd=cwd, env=env)
                stderr = io.StringIO()
                with patch.object(installer, 'run', side_effect=failing_run), \
                     patch.dict(os.environ, {'DEEPSEEK_API_KEY': 'dummy-private-credential'}), \
                     patch('sys.stderr', stderr), patch('sys.argv', ['managed_harness_install.py',
                         '--repo', str(self.repo), '--install', str(install), '--harness', 'hermes',
                         '--choice', 'enable', '--python', '/fixed/python', '--cache', str(self.cache)]):
                    self.assertEqual(installer.main(), 1)
                self.assertFalse(self.root.exists())
                self.assertFalse((install / 'harnesses/hermes/current.json').exists())
                self.assertFalse((install / 'dashboard/hermes-provider.json').exists())
                self.assertEqual(sum(argv[1:4] == ['-m', 'pm.cli', 'install'] for argv, _, _ in self.calls), count)
                self.assertIn('stdout (tail):', stderr.getvalue())
                self.assertIn('PM_STDOUT_FAILURE', stderr.getvalue())
                self.assertIn('stderr (tail):', stderr.getvalue())
                self.assertIn('PM_STDERR_FAILURE', stderr.getvalue())
                self.assertNotIn('x' * 4001, stderr.getvalue())
                self.assertNotIn('dummy-private-credential', stderr.getvalue())


class DiagnosticTests(unittest.TestCase):
    def test_real_failed_command_retains_stdout_and_stderr_without_inherited_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env = installer.environment(root)
            code = ("import os,sys; assert 'DEEPSEEK_API_KEY' not in os.environ; "
                    "print('resolver stdout reason'); print('runtime stderr reason',file=sys.stderr); sys.exit(1)")
            stderr = io.StringIO()
            def fail(**kwargs):
                installer.run([sys.executable, '-c', code], cwd=root, env=env)
            with patch.object(installer, 'provision', side_effect=fail), \
                 patch.dict(os.environ, {'DEEPSEEK_API_KEY': 'dummy-private-credential'}), \
                 patch('sys.stderr', stderr), patch('sys.argv', ['managed_harness_install.py',
                     '--repo', str(ROOT), '--install', str(root), '--harness', 'hermes']):
                self.assertEqual(installer.main(), 1)
            self.assertIn('stdout (tail):\nresolver stdout reason', stderr.getvalue())
            self.assertIn('stderr (tail):\nruntime stderr reason', stderr.getvalue())
            self.assertNotIn('dummy-private-credential', stderr.getvalue())


class CatalogGateTests(unittest.TestCase):
    def test_independent_harness_opt_in_and_legacy_manifest(self):
        from dashboard import managed_catalog
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            template, manifest = root / 'template.json', root / 'manifest.json'
            installer.save(template, {})
            installer.save(manifest, {'enabled': True, 'backends': {
                backend: {'model': 'fixture'} for backend in ('strata', 'deepseek')}})
            for selected in ('dsh', 'hermes', 'neither', 'legacy'):
                for harness in ('dsh', 'hermes'):
                    file = root / 'dashboard' / (harness + '-provider.json')
                    if selected == 'legacy':
                        file.unlink(missing_ok=True)
                    else:
                        installer.save(file, {'enabled': harness == selected})
                with patch.object(managed_catalog, '__file__', str(root / 'dashboard/managed_catalog.py')), \
                     patch.object(managed_catalog.sys, 'platform', 'darwin'), \
                     patch.object(managed_catalog, '_thin_selection', return_value={'config_file': str(template)}):
                    self.assertEqual(managed_catalog.standard_catalog(None, mode='legacy'), [])
                    rows = managed_catalog.standard_catalog(str(manifest), mode='legacy')
                for provider in rows:
                    statuses = {h['id']: h for h in provider['models'][0]['harnesses']}
                    for harness in ('dsh', 'hermes'):
                        expected = selected == 'legacy' or selected == harness
                        self.assertEqual(statuses[harness]['available'], expected, (selected, harness))
                        if not expected:
                            self.assertEqual(statuses[harness]['reason'], 'disabled_by_opt_in')


if __name__ == '__main__':
    unittest.main()
