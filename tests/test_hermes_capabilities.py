"""Native effort declarations are data; catalog reads never execute Hermes."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import shutil
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dashboard import managed_capabilities as capabilities
from dashboard import managed_catalog as catalog


class HermesCapabilities(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / 'source'
        route = {'provider': 'fixture-api', 'base_url': 'https://fixture.invalid/v1',
                 'api_mode': 'chat_completions', 'key_cmd': '/fixture/key-helper'}
        self.template = {'hermes_source': str(self.source), 'profiles': {'deepseek': dict(route), 'strata': dict(route)}}
        self.write('hermes_constants.py', '''raise RuntimeError("must never import")
VALID_REASONING_EFFORTS = ("low", "medium", "high", "max", "ultra")
def parse_reasoning_effort(effort):
    if effort in {"none", "false", "disabled"}:
        return {"enabled": False}
''')
        self.write('agent/reasoning_effort.py', 'OPENAI_COMPAT_WIRE_EFFORTS = ("none", "low", "medium", "high", "max")\n')
        self.write('plugins/model-providers/custom/__init__.py', '''raise RuntimeError("must never import")
class CustomProfile:
    def supported_reasoning_efforts(self, model):
        return OPENAI_COMPAT_WIRE_EFFORTS
    def default_reasoning_config(self, model=None):
        return {"enabled": True, "effort": "medium"}
''')

    def write(self, name, text):
        path = self.source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def load(self, backend='deepseek'):
        with patch.object(subprocess, 'Popen', side_effect=AssertionError('no process')), \
                patch.object(socket, 'socket', side_effect=AssertionError('no network')):
            return capabilities.load_effort_capability(self.template, harness='hermes', backend_id=backend, model='fixture-model')

    def test_source_supplies_vocabulary_without_guessing_effective_default(self):
        self.template['profiles']['deepseek']['reasoning_effort'] = 'low'
        expected = {'efforts': ['none', 'low', 'medium', 'high', 'max'], 'default_effort': None, 'effort_source': 'harness'}
        self.assertEqual(self.load(), expected)
        self.assertEqual(self.load('strata'), expected)
        # A new upstream declaration is used directly, without copied product levels.
        path = self.source / 'hermes_constants.py'
        self.write('hermes_constants.py', path.read_text().replace('("low", "medium", "high", "max", "ultra")', '("medium", "high")'))
        self.assertEqual(self.load()['efforts'], ['none', 'medium', 'high'])
        self.assertEqual(self.load('missing'), capabilities.UNKNOWN)

    def test_only_effective_chat_route_uses_custom_wire_metadata(self):
        for backend in ('deepseek', 'strata'):
            for base, mode, known in (
                    ('https://fixture.invalid/anthropic', 'anthropic_messages', False),
                    ('https://fixture.invalid/v1', 'chat_completions', True),
                    ('https://api.deepseek.com/anthropic', 'anthropic_messages', backend == 'deepseek')):
                with self.subTest(backend=backend, mode=mode, base=base):
                    self.template['profiles'][backend].update(base_url=base, api_mode=mode)
                    result = self.load(backend)
                    self.assertEqual(result['effort_source'], 'harness' if known else 'unknown')
                    self.assertIsNone(result['default_effort'])
        inherited = self.root / 'inherited.json'
        inherited.write_text(json.dumps({'model': {'provider': 'fixture-api'},
            'providers': {'fixture-api': {'api': 'https://fixture.invalid/anthropic',
                'transport': 'anthropic_messages', 'key_cmd': '/fixture/helper'}}}))
        self.template['profiles']['strata'] = {'template': str(inherited)}
        self.assertEqual(self.load('strata'), capabilities.UNKNOWN)

    def test_saved_global_and_model_override_cannot_be_mislabelled_medium(self):
        self.template['managed_root'] = str(self.root / 'runtime')
        profile = self.root / 'runtime/profiles/deepseek/config.yaml'
        profile.parent.mkdir(parents=True)
        for settings in ({'reasoning_effort': 'high'}, {'reasoning_overrides': {'fixture-model': 'max'}}):
            with self.subTest(settings=settings):
                profile.write_text(json.dumps({'agent': settings}))
                original = profile.read_bytes()
                self.assertIsNone(self.load()['default_effort'])
                self.assertEqual(profile.read_bytes(), original)

    def test_unknown_or_changed_policy_never_reuses_known_values(self):
        self.assertEqual(self.load()['effort_source'], 'harness')
        self.write('plugins/model-providers/custom/__init__.py', '''class CustomProfile:
    def supported_reasoning_efforts(self, model):
        return OPENAI_COMPAT_WIRE_EFFORTS
    def default_reasoning_config(self, model=None):
        if model:
            return {"enabled": True, "effort": "high"}
        return None
''')
        self.assertEqual(self.load(), capabilities.UNKNOWN)
        self.write('hermes_constants.py', 'not valid python!')
        self.assertEqual(self.load(), capabilities.UNKNOWN)

    def test_standard_catalog_projects_hermes_only_on_its_model_harness_row(self):
        template = self.root / 'template.json'
        template.write_text(json.dumps(self.template))
        manifest = self.root / 'manifest.json'
        manifest.write_text(json.dumps({'enabled': True, 'harnesses': {},
            'backends': {'deepseek': {'model': 'fixture-model'}, 'strata': {'model': 'fixture-local'}}}))
        def selection(path, harness, backend, model):
            if harness != 'hermes':
                raise ValueError('runtime_not_prepared')
            return {'config_file': str(template)}
        with patch.object(catalog, '_thin_selection', side_effect=selection), patch.object(catalog.sys, 'platform', 'darwin'):
            rows = catalog.standard_catalog(manifest, mode='legacy')
        for row in rows:
            dsh, hermes = row['models'][0]['harnesses']
            self.assertIsNone(dsh['efforts'])
            self.assertEqual(hermes['efforts'], ['none', 'low', 'medium', 'high', 'max'])
            self.assertIsNone(hermes['default_effort'])
            self.assertEqual(hermes['effort_source'], 'harness')
        for harness in ('claude', 'codex', 'unknown'):
            self.assertEqual(capabilities.load_effort_capability(self.template, harness=harness, backend_id='deepseek', model='fixture-model'), capabilities.UNKNOWN)

    def test_fixed_native_source_is_read_without_imports_or_credentials(self):
        source = os.environ.get('ORRERY_HERMES_TEST_SOURCE')
        if not source:
            self.skipTest('Explicit installed dependency interpreter/source required')
        self.template['hermes_source'] = source
        self.assertEqual(self.load(), {'efforts': ['none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max'],
                                      'default_effort': None, 'effort_source': 'harness'})

    def test_installed_dashboard_only_import_and_metadata_from_root_directory(self):
        installed = self.root / 'installed/dashboard'
        installed.mkdir(parents=True)
        repo = Path(__file__).resolve().parents[1]
        for name in ('managed_capabilities.py', 'managed_catalog.py', 'harness_helpers.py'):
            shutil.copyfile(repo / 'dashboard' / name, installed / name)
        (installed.parent / 'hooks').mkdir()
        shutil.copyfile(repo / 'hooks/native_connections.py', installed.parent / 'hooks/native_connections.py')
        generator = installed.parent / 'integrations/hermes/thin_config.py'
        generator.parent.mkdir(parents=True)
        shutil.copyfile(repo / 'integrations/hermes/thin_config.py', generator)
        self.template['hermes_source'] = os.environ.get('ORRERY_HERMES_TEST_SOURCE', str(self.source))
        expected = self.load()
        script = '''import json,sys
sys.path.insert(0,sys.argv[1])
def audit(event,args):
    if event in ('subprocess.Popen','socket.connect','socket.getaddrinfo'):
        raise AssertionError('No process, credential helper or network during catalog read')
sys.addaudithook(audit)
import managed_catalog
print(json.dumps(managed_catalog.load_effort_capability(json.loads(sys.argv[2]),
    harness='hermes',backend_id='deepseek',model='fixture-model')))
'''
        home = self.root / 'empty-home'
        home.mkdir()
        result = subprocess.run([sys.executable, '-I', '-B', '-c', script, str(installed), json.dumps(self.template)],
            cwd='/', env={'HOME': str(home), 'PATH': os.defpath}, stdin=subprocess.DEVNULL,
            capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), expected)


if __name__ == '__main__':
    unittest.main()
