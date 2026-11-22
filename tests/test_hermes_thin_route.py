"""Actual pinned provider resolution with synthetic credentials; no SDK/network."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]

ROUTE_SCRIPT = r'''import importlib.util, json, os, shlex, shutil, socket, subprocess, sys
from pathlib import Path
real_popen, real_socket = subprocess.Popen, socket.socket
sys.path.insert(0, str(Path(os.environ['FIXTURE_PRODUCT']) / 'tests'))
from test_hermes_thin_config import ThinConfig
fixture = ThinConfig()
fixture.setUp()
try:
    native = Path(os.environ['ORRERY_HERMES_TEST_SOURCE'])
    for name in ('auth.py', 'config_defaults.py'):
        shutil.copyfile(native / 'hermes_cli' / name, fixture.source / 'hermes_cli' / name)
    selected_url = 'https://api.deepseek.com/v1'
    selected_key = 'synthetic-selected-command-token'
    case, failure = sys.argv[1:3]
    helper_case = case.startswith(('helper_', 'delegation_', 'aux_'))
    helper = fixture.base / 'fake_helper.py'
    called = fixture.base / 'helper-calls'
    helper.write_text(chr(10).join([
        'import pathlib,sys', 'p=pathlib.Path(' + repr(str(called)) + ')',
        'p.write_text((p.read_text() if p.exists() else "")+"called"+chr(10))',
        'if sys.argv[1]=="nonzero": raise SystemExit(7)',
        'if sys.argv[1]=="blank": raise SystemExit(0)',
        'print(' + repr(selected_key) + ')', '']))
    command = shlex.join([sys.executable, str(helper), failure])
    fixture.profile_spec.update(base_url='https://api.deepseek.com/anthropic',
                                api_mode='anthropic_messages', key_cmd=command)
    fixture.save(fixture.template, fixture.template_data)
    profile = fixture.profile
    old = {'memory': {'memory_enabled': True},
           'telemetry': {'shared_metrics': {'enabled': False, 'send': False, 'offer_version': 2}}}
    entry = {'base_url': selected_url}
    if case in ('api', 'url'):
        entry = {case: 'https://old-endpoint.invalid/v1'}
    if case == 'config_key':
        entry.update(api_key='synthetic-config-canary', key_env='OLD_PROVIDER_KEY')
    if case not in ('fresh', 'helper_fresh'):
        old['providers'] = {'fixture-api': entry}
    if helper_case:
        fixture.document['helper_model'] = 'fixture-helper'
    if case in ('delegation_url', 'delegation_key'):
        old['delegation'] = {'base_url': 'https://old-helper.invalid/v1'}
        if case == 'delegation_key':
            old['delegation'].update(api_key='synthetic-old-helper-key', api_mode='codex_responses')
    if case.startswith('aux_'):
        task = 'fixture_plugin_task' if case == 'aux_custom' else 'vision'
        row = {'base_url': 'https://old-helper.invalid/v1'}
        if case in ('aux_key', 'aux_custom'):
            row['api_key'] = 'synthetic-old-helper-key'
        if case in ('aux_key_env', 'aux_api_key_env'):
            row['key_env' if case == 'aux_key_env' else 'api_key_env'] = 'OLD_PROVIDER_KEY'
        if case == 'aux_api_mode':
            row = {'api_mode': 'codex_responses'}
        old['auxiliary'] = {task: row}
    if case == 'model_key':
        old['model'] = {'provider': 'custom', 'base_url': selected_url,
                        'api_key': 'synthetic-model-key-canary', 'api': 'synthetic-model-api-canary',
                        'key_env': 'OLD_PROVIDER_KEY'}
    fixture.save(profile / 'config.yaml', old)
    if case in ('pool', 'legacy_pool'):
        key = 'fixture-api' if case == 'pool' else 'custom:fixture-api'
        fixture.save(profile / 'auth.json', {'version': 1, 'providers': {}, 'credential_pool': {key: [{
            'id': 'fixture-old', 'source': 'manual', 'auth_type': 'api_key',
            'access_token': 'synthetic-auth-pool-canary', 'base_url': selected_url}]}})
    (profile / '.env').write_text('DEEPSEEK_API_KEY=synthetic-dotenv-canary' + chr(10) +
                                'OLD_PROVIDER_KEY=synthetic-old-env-canary' + chr(10))
    memory = profile / 'memories/MEMORY.md'
    memory.parent.mkdir()
    memory.write_text('persistent memory must stay')
    preserved = {p: p.read_bytes() for p in (profile / 'config.yaml', profile / '.env', memory)}
    if (profile / 'auth.json').exists():
        preserved[profile / 'auth.json'] = (profile / 'auth.json').read_bytes()
    spec = importlib.util.spec_from_file_location('route_generator', os.environ['FIXTURE_GENERATOR'])
    generator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(generator)
    plan = generator.generate(fixture.document)
    subprocess.Popen, socket.socket = real_popen, real_socket
    os.environ.update(plan['env'], DEEPSEEK_API_KEY='synthetic-ambient-canary',
                      OPENAI_API_KEY='synthetic-openai-canary', OLD_PROVIDER_KEY='synthetic-old-env-canary')
    def audit(event, args):
        if event in ('socket.connect', 'socket.getaddrinfo'):
            raise AssertionError('provider network forbidden')
        if event == 'subprocess.Popen':
            argv = args[1]
            executable = Path(argv[0]).name
            if executable == 'git' and any(part in argv for part in ('rev-parse', 'describe', 'log', 'status', 'show', 'rev-list', 'branch', 'tag')):
                return  # installed v1 fixture has no version stamp; read-only provenance
            if executable == 'sh' and argv[1:] == ['-c', command]:
                return  # only this fixture's selected credential helper
            raise AssertionError('unexpected subprocess')
    sys.addaudithook(audit)
    sys.path.insert(0, str(native))
    from hermes_cli.config import load_config
    from hermes_cli.runtime_provider import resolve_runtime_provider
    from agent.command_token_source import CommandTokenError
    loaded = load_config()
    provider = plan['argv'][plan['argv'].index('--provider') + 1]
    runtime = resolve_runtime_provider(requested=provider, target_model='fixture-main')
    assert runtime['base_url'] == selected_url, 'resolved a stale endpoint: ' + case
    assert runtime['api_mode'] == 'chat_completions'
    assert callable(runtime['api_key']), 'selected file/pool/env auth instead of key_cmd: ' + case
    assert runtime['api_key'].cache_identity == 'cmd:' + command
    if failure == 'ok':
        assert runtime['api_key']() == selected_key
        assert runtime['api_key']() == selected_key
        assert called.read_text().splitlines() == ['called']  # native cache, not another helper
    else:
        try:
            runtime['api_key']()
        except CommandTokenError:
            pass
        else:
            raise AssertionError('credential failure fell back to a canary')
        assert called.read_text().splitlines() == ['called']
    if helper_case:
        from types import SimpleNamespace
        from tools.delegate_tool_config import _resolve_delegation_credentials, _resolve_child_runtime
        from agent.auxiliary_client import _resolve_task_provider_model, _normalize_aux_provider, _named_custom_api_key
        from hermes_cli.runtime_provider_custom import _get_named_custom_provider
        parent = SimpleNamespace(model='fixture-main', provider=runtime['provider'], requested_provider=provider,
                                 base_url=runtime['base_url'], api_key=runtime['api_key'], api_mode=runtime['api_mode'])
        cfg = loaded['delegation']
        bundle = _resolve_delegation_credentials(cfg, parent)
        child = _resolve_child_runtime(parent, cfg, parent.api_key, model=bundle['model'],
            override_provider=bundle['provider'], override_base_url=bundle['base_url'],
            override_api_key=bundle['api_key'], override_api_mode=bundle['api_mode'],
            override_acp_command=bundle.get('command'), override_acp_args=bundle.get('args'))
        assert child['base_url'] == selected_url, 'delegation inherited a stale endpoint'
        assert child['requested_provider'] == provider and child['model'] == 'fixture-helper'
        assert child['api_mode'] == runtime['api_mode']
        assert callable(child['api_key']) and child['api_key'].cache_identity == 'cmd:' + command
        assert child['api_key']() == selected_key
        tasks = [name for name, row in loaded['auxiliary'].items() if isinstance(row, dict)]
        for task in tasks:
            aux_provider, aux_model, aux_url, aux_key, aux_mode = _resolve_task_provider_model(task)
            aux_provider = _normalize_aux_provider(aux_provider)
            assert aux_provider == provider and aux_model == 'fixture-helper', 'wrong auxiliary model/provider: ' + task
            assert not aux_url and not aux_key, 'auxiliary inherited direct endpoint/auth: ' + task
            assert not aux_mode, 'auxiliary inherited a stale protocol: ' + task
            named = _get_named_custom_provider(aux_provider)
            assert named['base_url'] == selected_url and named['api_mode'] == runtime['api_mode']
            key = _named_custom_api_key(named, aux_provider, named['base_url'])
            assert callable(key) and key.cache_identity == 'cmd:' + command
            assert key() == selected_key
    assert all(path.read_bytes() == original for path, original in preserved.items()), 'persistent profile was rewritten'
    print(json.dumps({'case': case, 'helper': failure, 'endpoint': 'selected', 'credential': 'selected-key-cmd',
                      'profile_preserved': True, 'provider_requests': 0}))
finally:
    fixture.doCleanups()
'''


class NativeProviderRoute(unittest.TestCase):
    def route(self, case, failure='ok'):
        source = os.environ.get('ORRERY_HERMES_TEST_SOURCE')
        python = os.environ.get('ORRERY_HERMES_TEST_PYTHON')
        if not source or not python:
            self.skipTest('Explicit installed dependency interpreter/source required')
        with tempfile.TemporaryDirectory() as home:
            result = subprocess.run([python, '-B', '-c', ROUTE_SCRIPT, case, failure], cwd='/',
                env={'HOME': home, 'PATH': os.defpath, 'PYTHONDONTWRITEBYTECODE': '1',
                     'HERMES_DISABLE_LAZY_INSTALLS': '1', 'UV_OFFLINE': '1',
                     'FIXTURE_PRODUCT': str(ROOT), 'ORRERY_HERMES_TEST_SOURCE': source,
                     'FIXTURE_GENERATOR': os.environ.get('ORRERY_HERMES_ROUTE_GENERATOR', str(ROOT / 'integrations/hermes/thin_config.py'))},
                capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_native_endpoint_and_credential_source_ignore_each_stale_profile_source(self):
        for case in ('fresh', 'api', 'url', 'config_key', 'pool', 'legacy_pool', 'model_key'):
            with self.subTest(case=case):
                self.route(case)

    def test_failed_or_empty_selected_helper_never_falls_back_to_canaries(self):
        for case in ('fresh', 'config_key', 'pool', 'legacy_pool'):
            for failure in ('nonzero', 'blank'):
                with self.subTest(case=case, failure=failure):
                    self.route(case, failure)

    def test_actual_delegation_and_auxiliary_resolvers_ignore_stale_direct_routes(self):
        for case in ('helper_fresh', 'delegation_url', 'delegation_key', 'aux_url', 'aux_key',
                     'aux_key_env', 'aux_api_key_env', 'aux_api_mode', 'aux_custom'):
            with self.subTest(case=case):
                self.route(case)


if __name__ == '__main__':
    unittest.main()
