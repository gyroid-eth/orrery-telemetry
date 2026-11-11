"""共通設定の投影と4系統の回帰。実資格・実サービスは使わない。"""
from contextlib import contextmanager
import importlib.util
import json
import os
from pathlib import Path
import runpy
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest

import test_dsh_thin_config as dsh
import test_hermes_thin_config as hermes
import test_harness_child as harness_child
from test_spawn_parity import catalog, preview

ROOT = Path(__file__).resolve().parents[1]
WEB = runpy.run_path(str(ROOT / 'hooks/native_web_search.py'))
SETTINGS = {'provider': 'exa', 'exa': {'credential_ref': {
    'store': 'keychain', 'service': 'fixture-exa', 'account': 'fixture-account'}},
    'searxng': {'url': 'http://127.0.0.1:54321'}}


@contextmanager
def fixture_for(harness):
    fixture = dsh.ThinTests() if harness == 'dsh' else hermes.ThinConfig()
    fixture.setUp()
    try:
        yield fixture
    finally:
        fixture.doCleanups()


@pytest.mark.parametrize('provider', ['exa', 'searxng'])
def test_fresh_telegram_bot_keeps_web_search_and_both_credential_references(provider):
    with fixture_for('hermes') as f:
        client = f.base / 'fresh-client'
        client.mkdir()
        (client / 'context.json').write_text('{}')
        document = dict(f.document, fresh_client_dir=str(client), web_search=provider,
            telegram={'token_ref': {'store': 'keychain', 'service': 'fixture.telegram',
                                   'account': 'fixture'}, 'allow_from': [123]})
        manifest = json.loads(f.manifest.read_text())
        manifest['web_search'] = SETTINGS
        f.manifest.write_text(json.dumps(manifest))
        plan = hermes.config.generate(document)
        overlay = hermes.config.read(Path(plan['config_dir']) / 'config.yaml')
        definitions = hermes.config.read(Path(plan['env']['AGENTSTACK_LLM_KEY_DEFINITIONS']))
        assert '--tui' in plan['argv']
        assert overlay['telegram'] == document['telegram']
        assert 'orrery-telegram' in overlay['plugins']['enabled']
        assert overlay['web']['search_backend'] == ('orrery-searxng' if provider == 'searxng' else 'exa')
        assert set(definitions['backends']) == {'hermes', 'telegram', 'web-search'}
        if provider == 'searxng':
            assert 'orrery-web-search' in overlay['plugins']['enabled']
        else:
            assert overlay['secrets']['command']['enabled']


@pytest.mark.parametrize('harness', ['dsh', 'hermes'])
@pytest.mark.parametrize('provider', ['exa', 'searxng', None])
def test_shared_settings_and_launch_override_without_credential_or_network(harness, provider):
    with fixture_for(harness) as f:
        generator = dsh.thin if harness == 'dsh' else hermes.config
        document = dict(f.doc if harness == 'dsh' else f.document)
        manifest = json.loads(f.manifest.read_text())
        if provider:
            manifest['web_search'] = SETTINGS
            document['web_search'] = provider
            f.manifest.write_text(json.dumps(manifest))
        plan = generator.generate(document)
        serialized = json.dumps(plan)
        assert 'EXA_API_KEY' not in plan['env']
        if harness == 'dsh':
            overlay = generator.read(Path(plan['config_dir']) / 'native.patch.json')
            selected = [r for r in overlay if r.get('id') == 'web']
            assert selected == ([{'id': 'web', 'config': {'searchProvider': 'orrery-' + provider}}] if provider else [])
        else:
            overlay = generator.read(Path(plan['config_dir']) / 'config.yaml')
            assert overlay.get('web', {}).get('search_backend') == ('orrery-searxng' if provider == 'searxng' else provider)
            if provider == 'exa':
                command = overlay['secrets']['command']
                raw = generator.read(Path(plan['env']['HERMES_HOME']) / 'config.yaml')
                assert raw['secrets']['command'] == command
                assert command['enabled'] and command['override_existing']
                assert '--emit-env' in command['command']
                assert overlay['web']['provider_tier']['exa'] == 'paid'
            elif provider == 'searxng':
                assert plan['env']['SEARXNG_URL'] == SETTINGS['searxng']['url']
            if provider:
                assert overlay['web']['keyless_rescue'] is False
        if provider:
            definitions = generator.read(plan['env']['AGENTSTACK_LLM_KEY_DEFINITIONS'])
            search = definitions['backends']['web-search']
            assert '--config' in search['auth_command']
            if provider == 'exa':
                assert search['keychain']['service'] == 'fixture-exa'
            settings = Path(document['state_dir']) / 'web-search.json'
            assert str(settings) in json.loads(plan['env']['AGENTSTACK_LLM_KEY_FILES'])
            assert settings.stat().st_mode & 0o777 == 0o600
        else:
            assert 'web-search' not in serialized


def test_shared_default_and_missing_key_reference():
    assert WEB['select']({}, {}) is None
    assert WEB['select']({}, {'web_search': SETTINGS})['provider'] == 'exa'
    assert WEB['select']({'web_search': 'searxng'}, {'web_search': SETTINGS}) == {
        'provider': 'searxng', 'url': SETTINGS['searxng']['url']}
    assert WEB['select']({}, {'web_search': {'provider': 'exa'}}) == {'provider': 'exa'}
    linux = {'provider': 'exa', 'exa': {'credential_ref': {'store': 'secret-service',
             'attributes': {'service': 'fixture-exa', 'account': 'fixture-account'}}}}
    assert WEB['select']({}, {'web_search': linux})['credential_ref'] == linux['exa']['credential_ref']


def test_missing_key_helper_is_nonfatal_for_hermes(tmp_path):
    settings = tmp_path / 'search.json'
    settings.write_text('{"provider":"exa"}')
    command = [sys.executable, '-I', '-B', str(ROOT / 'hooks/native_web_search.py'), '--config', str(settings)]
    missing = subprocess.run(command, capture_output=True, text=True, check=False)
    assert missing.returncode == 1 and 'Exa' in missing.stderr and not missing.stdout
    startup = subprocess.run([*command, '--emit-env'], capture_output=True, text=True, check=False)
    assert startup.returncode == 0 and startup.stdout == 'EXA_API_KEY=\n'


def test_keychain_reader_captures_only_the_selected_fake_key(monkeypatch):
    monkeypatch.setattr(sys, 'platform', 'darwin')
    calls = []
    def fake(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0, stdout=b'fixture-exa-token\n')
    assert WEB['read_key'](WEB['select']({}, {'web_search': SETTINGS}), runner=fake) == 'fixture-exa-token'
    assert calls[0][0] == ['/usr/bin/security', 'find-generic-password', '-s', 'fixture-exa', '-a', 'fixture-account', '-w']
    assert calls[0][1]['capture_output'] and 'fixture-exa-token' not in json.dumps(calls[0][0])


@pytest.mark.parametrize('harness', ['dsh', 'hermes'])
@pytest.mark.parametrize('provider', ['exa', 'searxng'])
def test_spawn_api_and_saved_native_selection(preview, monkeypatch, tmp_path, harness, provider):
    from dashboard import server
    result = server.do_spawn({**preview, 'provider': 'deepseek', 'harness': harness,
        'model': 'deepseek-fixture', 'web_search': provider})
    assert result['ok']
    index = result['argv'].index('--web-search')
    assert result['argv'][index + 1] == provider
    args = harness_child.staged(tmp_path, harness)
    args.web_search = provider
    captured = {}
    monkeypatch.setattr(harness_child.child, 'selection', lambda *a: {})
    monkeypatch.setattr(harness_child.child, 'generate_plan', lambda document, runner: captured.update(document))
    harness_child.child.prepare(args)
    assert captured['web_search'] == provider


def test_search_reader_uses_existing_credential_guard(tmp_path, monkeypatch):
    guard = runpy.run_path(str(ROOT / 'hooks/deny-llm-key-read.py'))
    settings = tmp_path / 'search.json'
    settings.write_text(json.dumps({'provider': 'exa'}))
    argv = WEB['command'](ROOT, settings, sys.executable)
    definitions = tmp_path / 'definitions.json'
    definitions.write_text(json.dumps({'backends': {'web-search': WEB['definition'](
        WEB['select']({}, {'web_search': SETTINGS}), argv)}}))
    monkeypatch.setenv('AGENTSTACK_LLM_KEY_DEFINITIONS', str(definitions))
    import shlex
    for command in (shlex.join(argv), shlex.join([sys.executable, '-B', argv[3], *argv[4:]]),
                    'security find-generic-password -s fixture-exa -a fixture-account -w'):
        assert guard['decide']({'tool_name': 'Bash', 'tool_input': {'command': command}})


@pytest.mark.parametrize('provider', ['exa', 'searxng'])
def test_fresh_saved_selection_reaches_generator(tmp_path, monkeypatch, provider):
    from agentstack_mail import fresh_proxy
    spec = importlib.util.spec_from_file_location('agentstack_mail.fresh_harness',
        ROOT / 'packages/agentstack_mail/src/agentstack_mail/fresh_harness.py')
    fresh_harness = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fresh_harness)
    captured = {}
    def generate(document, runner):
        captured.update(document)
        target = Path(document['state_dir']) / 'launch.json'
        target.write_text(json.dumps({'env': {}}))
    def instructions(state, root):
        path = state / 'instructions.md'
        path.write_text('試験の指示')
        return path
    api = {'fresh_instructions': instructions, 'dynamic_prelaunch': lambda d: None,
           'generate_plan': generate, 'read': lambda p: json.loads(Path(p).read_text()),
           'validate_plan': lambda *a: None}
    monkeypatch.setattr(fresh_harness, 'helper', lambda root: api)
    async def owner(client): return {'agent_name': 'FixtureAgent'}
    monkeypatch.setattr(fresh_proxy, 'verified_owner', owner)
    base = tmp_path / 'client'; base.mkdir(mode=0o700)
    run = tmp_path / 'run'; run.mkdir(mode=0o700)
    client = SimpleNamespace(path=base / 'context.json')
    settings = {'model': 'fixture-model', 'runner': {'connection_kind': 'strata'},
                'spec': {'selection': {'web_search': provider, 'manifest_file': '/fixture/manifest.json', 'backend_id': 'strata'}}}
    fresh_harness.plan(client, ROOT, 'dsh', settings, run, tmp_path, '試験', None)
    assert captured['web_search'] == provider


def test_dsh_fake_http():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node がありません')
    result = subprocess.run([node, '--test', str(ROOT / 'tests/test_native_web_search.mjs')],
        capture_output=True, text=True, timeout=40)
    assert result.returncode == 0, result.stdout + result.stderr


HERMES_SCRIPT = r'''
import json, os, pathlib, socket, subprocess, sys, threading, types, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
real_popen, real_socket = subprocess.Popen, socket.socket
sys.path.insert(0, os.environ['FIXTURE_PRODUCT'] + '/tests')
from test_hermes_thin_config import ThinConfig, config
f = ThinConfig(); f.setUp()
case = sys.argv[1]
try:
    calls = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_GET(self):
            calls.append(self.path)
            data = {'results': [{'title':'見本', 'url':'https://example.invalid/source', 'content':'本文'}]}
            if case == 'engines': data = {'results':[], 'unresponsive_engines':[['fixture-engine','blocked']]}
            self.send_response(200); self.end_headers(); self.wfile.write(json.dumps(data).encode())
        def do_POST(self):
            data = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            assert self.headers['Authorization'] == 'Bearer fixture-exa-token'
            assert data['query'] == 'fixture-query'
            calls.append(self.path)
            self.send_response(200); self.end_headers()
            self.wfile.write(json.dumps({'results':[{'title':'見本','url':'https://example.invalid/source','highlights':['本文']}]}).encode())
    subprocess.Popen, socket.socket = real_popen, real_socket
    server = ThreadingHTTPServer(('127.0.0.1',0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = 'http://127.0.0.1:' + str(server.server_port)
    provider = 'exa' if case in ('exa','missing') else 'searxng'
    f.manifest_data['web_search'] = {'provider':provider, 'searxng':{'url':url}}
    f.save(f.manifest, f.manifest_data)
    plan = config.generate(f.document)
    os.environ.update(plan['env'])
    sys.path.insert(0, os.environ['ORRERY_HERMES_TEST_SOURCE'])
    # SDK 依存だけを偽物にし、native の provider・dispatch・HTTP を通す。
    class Exa:
        def __init__(self, api_key): self.api_key=api_key; self.headers={}
        def search(self, query, **kwargs):
            request = urllib.request.Request(url+'/search', data=json.dumps({'query':query, **kwargs}).encode(),
                headers={'Authorization':'Bearer '+self.api_key, 'Content-Type':'application/json'})
            with urllib.request.urlopen(request) as response: data=json.load(response)
            return types.SimpleNamespace(results=[types.SimpleNamespace(**r) for r in data['results']])
    sys.modules['exa_py'] = types.SimpleNamespace(Exa=Exa)
    from agent.secret_sources import command
    def helper(*args):
        assert 'native_web_search.py' in args[0] and '--emit-env' in args[0]
        return 'EXA_API_KEY=' + ('' if case == 'missing' else 'fixture-exa-token') + '\n'
    command._run_helper = helper
    from hermes_cli.env_loader import _apply_external_secret_sources
    _apply_external_secret_sources(pathlib.Path(plan['env']['HERMES_HOME']))
    if case == 'exa': assert os.environ['EXA_API_KEY'] == 'fixture-exa-token'
    if case == 'stopped': server.shutdown(); server.server_close()
    from tools import web_tools
    assert web_tools.check_web_api_key(), '検索 tool が起動時に消えています'
    result = json.loads(web_tools.web_search_tool('fixture-query'))
    if case in ('exa','searxng'):
        assert result['success'], result
        assert result['data']['web'][0]['url'] == 'https://example.invalid/source', result
        assert len(calls) == 1, calls
    else:
        assert result['success'] is False, result
        assert ('EXA_API_KEY' if case=='missing' else 'fixture-engine' if case=='engines' else 'SearXNG') in result['error'], result
    from tools.environments.local import build_subprocess_env
    terminal_env = build_subprocess_env()
    assert 'EXA_API_KEY' not in terminal_env
    terminal = subprocess.run(['/usr/bin/printenv'], env=terminal_env, capture_output=True, text=True)
    assert 'fixture-exa-token' not in terminal.stdout
    assert 'fixture-exa-token' not in json.dumps(plan)
    if case != 'stopped': server.shutdown(); server.server_close()
finally:
    f.doCleanups()
'''


@pytest.mark.parametrize('case', ['exa', 'searxng', 'missing', 'stopped', 'engines'])
def test_hermes_native_fake_http(case, tmp_path):
    python = os.environ.get('ORRERY_HERMES_TEST_PYTHON')
    source = os.environ.get('ORRERY_HERMES_TEST_SOURCE')
    if not python or not source:
        pytest.skip('固定版 Hermes の依存 interpreter と source を明示してください')
    result = subprocess.run([python, '-I', '-B', '-c', HERMES_SCRIPT, case], cwd='/',
        env={'HOME': str(tmp_path), 'PATH': os.defpath, 'PYTHONDONTWRITEBYTECODE': '1',
             'HERMES_DISABLE_LAZY_INSTALLS': '1', 'UV_OFFLINE': '1',
             'FIXTURE_PRODUCT': str(ROOT), 'ORRERY_HERMES_TEST_SOURCE': source},
        capture_output=True, text=True, timeout=45)
    assert result.returncode == 0, result.stdout + result.stderr
