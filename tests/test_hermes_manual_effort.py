"""Manual Anthropic capabilities and native serialized budgets, offline only."""
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from dashboard import managed_capabilities as capabilities
from dashboard import managed_catalog as catalog


class UnknownManualPolicy(unittest.TestCase):
    def test_unreviewed_native_policy_is_unknown(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'agent').mkdir()
            (root / 'agent/anthropic_adapter.py').write_text('def _thinking_kwargs(*args): return {}\n')
            template = {'hermes_source': str(root), 'profiles': {'strata': {
                'provider': 'fixture', 'base_url': 'https://fixture.invalid/anthropic',
                'api_mode': 'anthropic_messages', 'key_cmd': '/fixture/helper'}}}
            self.assertEqual(capabilities.load_effort_capability(template,
                harness='hermes', backend_id='strata', model='fixture-model'), capabilities.UNKNOWN)

    def test_generator_preserves_saved_settings_and_only_explicit_argv(self):
        from test_hermes_thin_config import ThinConfig, config
        fixture = ThinConfig()
        fixture.setUp()
        try:
            fixture.profile_spec.update(base_url='https://fixture.invalid/anthropic', api_mode='anthropic_messages')
            fixture.save(fixture.template, fixture.template_data)
            saved = fixture.profile / 'config.yaml'
            fixture.save(saved, {'agent': {'reasoning_effort': 'high',
                'reasoning_overrides': {'fixture-main': 'medium'}}})
            default = fixture.generate()
            self.assertNotIn('--reasoning', default['argv'])
            self.assertEqual(config.read(saved)['agent'], {'reasoning_effort': 'high',
                'reasoning_overrides': {'fixture-main': 'medium'}})
            # First generation may add the existing native telemetry defaults.
            before = saved.read_bytes()
            for effort in ('low', 'medium', 'high', 'xhigh'):
                plan = fixture.generate(effort=effort)
                self.assertEqual(plan['argv'][plan['argv'].index('--reasoning') + 1], effort)
                self.assertEqual(saved.read_bytes(), before)
                overlay = config.read(Path(plan['config_dir']) / 'config.yaml')
                self.assertEqual(overlay['providers'][plan['argv'][3]]['api_mode'], 'anthropic_messages')
            reset = fixture.generate()
            self.assertEqual(default['argv'], reset['argv'])
            self.assertEqual(saved.read_bytes(), before)
        finally:
            fixture.doCleanups()


class ManualEffort(unittest.TestCase):
    def setUp(self):
        source = os.environ.get('ORRERY_HERMES_TEST_SOURCE')
        if not source:
            self.skipTest('Explicit isolated pinned native source required')
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / 'source'
        for filename in capabilities._HERMES_MANUAL_POLICY:
            target = self.source / filename
            target.parent.mkdir(parents=True, exist_ok=True)
            # Fixture copies only reviewed source data, never an installed tree.
            shutil.copyfile(Path(source) / filename, target)
        self.template = {'hermes_source': str(self.source), 'profiles': {'strata': {
            'provider': 'fixture', 'model': 'claude-fixture-primary',
            'base_url': 'https://fixture.invalid/anthropic', 'api_mode': 'anthropic_messages',
            'key_cmd': '/fixture/helper'}}}
        self.model = 'qwen3.8-flash-next-iq3_xxs'

    def load(self, model=None):
        with patch.object(subprocess, 'Popen', side_effect=AssertionError('no process')), \
                patch.object(socket, 'socket', side_effect=AssertionError('no network')):
            return capabilities.load_effort_capability(self.template, harness='hermes',
                backend_id='strata', model=self.model if model is None else model)

    def test_selected_model_manual_four_and_neutral_default_without_import(self):
        # These would fail immediately if the metadata reader imported Hermes.
        for filename in capabilities._HERMES_MANUAL_POLICY:
            path = self.source / filename
            path.write_text('raise RuntimeError("native import forbidden")\n' + path.read_text())
        self.assertEqual(self.load(), {'efforts': ['low', 'medium', 'high', 'xhigh'],
            'default_effort': None, 'effort_source': 'harness'})
        self.assertEqual(self.load('anthropic/' + self.model), self.load())
        self.assertEqual(self.load('vendor/ordinary-manual-model'), self.load())

    def test_excludes_native_adaptive_haiku_and_empty_names(self):
        for name in ('claude-opus-5-5', 'vendor/CLAUDE-future', 'claude-3-7-sonnet',
                     'haiku-next', 'moonshotai/kimi-k2.5', ' vendor/K3 ', 'MOONSHOT-v1',
                     'k2-thinking', 'kimi_thinking', '', '   '):
            with self.subTest(model=name):
                self.assertEqual(self.load(name), capabilities.UNKNOWN)

    def test_policy_change_duplicate_and_missing_declaration_never_reuse_metadata(self):
        path = self.source / 'agent/anthropic_adapter.py'
        original = path.read_text()
        for altered in (original.replace('budget + 4096', 'budget + 1024'),
                        original + '\ndef _thinking_kwargs(*args): return {}\n',
                        original.replace('THINKING_BUDGET =', 'CHANGED_BUDGET ='),
                        original + '\nTHINKING_BUDGET = {}\n', 'not valid python!'):
            with self.subTest(change=altered[-45:]):
                self.assertEqual(self.load()['effort_source'], 'harness')
                path.write_text(altered)
                self.assertEqual(self.load(), capabilities.UNKNOWN)
                path.write_text(original)

    def test_invalid_budgets_and_family_declarations_are_unknown(self):
        for filename, before, after in (
                ('agent/anthropic_adapter.py', '"xhigh": 32000', '"xhigh": True'),
                ('agent/anthropic_adapter.py', '"xhigh": 32000', '"xhigh": 8000'),
                ('agent/anthropic_adapter.py', '"xhigh": 32000', '"xhigh": -1'),
                ('agent/anthropic_endpoints.py', 'frozenset({"k3"})', 'custom_family()'),
                ('agent/anthropic_endpoints.py', '"kimi-",', 'False,'),
                ('agent/anthropic_endpoints.py', 'm.startswith(', 'not m.startswith(')):
            path = self.source / filename
            original = path.read_text()
            self.assertIn(before, original)
            with self.subTest(filename=filename, change=after):
                path.write_text(original.replace(before, after, 1))
                self.assertEqual(self.load(), capabilities.UNKNOWN)
                path.write_text(original)

    def test_native_budget_declaration_is_used_in_numeric_order(self):
        path = self.source / 'agent/anthropic_adapter.py'
        path.write_text(path.read_text().replace('"low": 4000', '"low": 12000'))
        self.assertEqual(self.load()['efforts'], ['medium', 'low', 'high', 'xhigh'])

    def test_model_rows_keep_each_selected_model_policy(self):
        template = self.root / 'template.json'
        template.write_text(json.dumps(self.template))
        manifest = self.root / 'manifest.json'
        manifest.write_text(json.dumps({'enabled': True, 'harnesses': {}, 'backends': {
            'strata': {'model': self.model, 'models': [self.model, 'vendor/k3']}}}))
        def selection(path, harness, backend, model):
            if harness != 'hermes':
                raise ValueError('runtime_not_prepared')
            return {'config_file': str(template)}
        with patch.object(catalog, '_thin_selection', side_effect=selection), \
                patch.object(catalog.sys, 'platform', 'darwin'):
            rows = catalog.standard_catalog(manifest, mode='legacy')[0]['models']
        self.assertEqual(rows[0]['harnesses'][1]['efforts'], ['low', 'medium', 'high', 'xhigh'])
        self.assertIsNone(rows[1]['harnesses'][1]['efforts'])

    def test_installed_metadata_in_minimal_environment(self):
        installed = self.root / 'installed'
        (installed / 'dashboard').mkdir(parents=True)
        (installed / 'integrations/hermes').mkdir(parents=True)
        for name in ('dashboard/managed_capabilities.py', 'integrations/hermes/thin_config.py'):
            shutil.copyfile(ROOT / name, installed / name)
        script = '''import json,sys
sys.path.insert(0,sys.argv[1])
def audit(event,args):
    if event in ('subprocess.Popen','socket.connect','socket.getaddrinfo'):
        raise AssertionError('metadata must not execute helpers or connect')
sys.addaudithook(audit)
import managed_capabilities as c
print(json.dumps(c.load_effort_capability(json.loads(sys.argv[2]),harness='hermes',backend_id='strata',model=sys.argv[3])))
'''
        home = self.root / 'home'
        home.mkdir()
        result = subprocess.run([sys.executable, '-I', '-B', '-c', script,
            str(installed / 'dashboard'), json.dumps(self.template), self.model], cwd='/',
            env={'HOME': str(home), 'PATH': os.defpath}, stdin=subprocess.DEVNULL,
            capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), self.load())


NATIVE_SCRIPT = r'''
import json,sys
from pathlib import Path
def audit(event,args):
    if event in ('subprocess.Popen','socket.connect','socket.getaddrinfo'):
        raise AssertionError('no helper or network')
sys.addaudithook(audit)
sys.path.insert(0,sys.argv[1])
from hermes_constants import parse_reasoning_effort,resolve_reasoning_config
from agent.transports.anthropic import AnthropicTransport
from agent.reasoning_params import unset_reasoning_default
from types import SimpleNamespace
import anthropic,httpx
model='qwen3.8-flash-next-iq3_xxs'
records=[]
def handler(request):
    records.append(json.loads(request.content))
    return httpx.Response(200,json={'id':'msg_fixture','type':'message','role':'assistant',
        'model':model,'content':[{'type':'text','text':'synthetic'}],
        'stop_reason':'end_turn','usage':{'input_tokens':1,'output_tokens':1}})
with anthropic.Anthropic(api_key='synthetic',base_url='https://fixture.invalid',max_retries=0,timeout=5,
        http_client=httpx.Client(transport=httpx.MockTransport(handler))) as client:
    transport=AnthropicTransport()
    cases=[({},value,value,budget) for value,budget in [('low',4000),('medium',8000),('high',16000),('xhigh',32000)]]
    cases += [({},None,None,None),({'reasoning_effort':'low'},None,'low',4000),
        ({'reasoning_effort':'high','reasoning_overrides':{model:'medium'}},None,'medium',8000),
        ({'reasoning_effort':'high','reasoning_overrides':{model:'medium'}},'low','low',4000)]
    for saved,explicit,expected,budget in cases:
        before=json.dumps(saved,sort_keys=True)
        reasoning=resolve_reasoning_config({'agent':saved},model)
        if explicit is not None: reasoning=parse_reasoning_effort(explicit)
        if reasoning is None:
            reasoning=unset_reasoning_default(SimpleNamespace(api_mode='anthropic_messages'))
        kwargs=transport.build_kwargs(model,[{'role':'user','content':'synthetic'}],None,
            max_tokens=1024,context_length=8192,reasoning_config=reasoning,
            base_url='https://fixture.invalid/anthropic')
        count=len(records)
        client.messages.create(**kwargs)
        assert len(records)==count+1  # no fallback/retry may establish success
        wire=records[-1]
        if budget is None:
            assert 'thinking' not in wire and wire['max_tokens']==1024,wire
        else:
            assert wire['thinking']=={'type':'enabled','budget_tokens':budget},wire
            assert wire['temperature']==1 and wire['max_tokens']==budget+4096,wire
        assert 'output_config' not in wire and 'reasoning_effort' not in wire
        assert json.dumps(saved,sort_keys=True)==before
print(json.dumps({'cases':len(records),'budgets':[r.get('thinking',{}).get('budget_tokens') for r in records],
    'external_requests':0,'helper_calls':0,'fallback_requests':0}))
'''


class NativeManualWire(unittest.TestCase):
    def test_actual_resolver_transport_and_sdk_serialization(self):
        source = os.environ.get('ORRERY_HERMES_TEST_SOURCE')
        python = os.environ.get('ORRERY_HERMES_TEST_PYTHON')
        if not source or not python:
            self.skipTest('Explicit isolated native source and dependency interpreter required')
        with tempfile.TemporaryDirectory() as home:
            result = subprocess.run([python, '-I', '-B', '-c', NATIVE_SCRIPT, source], cwd='/',
                env={'HOME': home, 'PATH': os.defpath, 'PYTHONDONTWRITEBYTECODE': '1',
                     'HERMES_DISABLE_LAZY_INSTALLS': '1', 'UV_OFFLINE': '1'},
                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        result = json.loads(result.stdout.splitlines()[-1])
        self.assertEqual(result, {'cases': 8, 'budgets': [4000,8000,16000,32000,None,4000,8000,4000],
            'external_requests': 0, 'helper_calls': 0, 'fallback_requests': 0})


if __name__ == '__main__':
    unittest.main()
