"""Standard spawn contracts, using fixture catalogs and no live runtimes."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from dashboard import managed_catalog, server


@pytest.fixture
def catalog(monkeypatch):
    rows = [
        {'id': provider, 'kind': 'deepseek' if provider == 'deepseek' else 'strata',
         'models': [{'id': provider + '-fixture', 'harnesses': [
            {'id': 'dsh', 'available': True, 'efforts': ['low', 'high'],
             'default_effort': 'high', 'effort_source': 'harness'},
            {'id': 'hermes', 'available': True, 'efforts': None,
             'default_effort': None, 'effort_source': 'unknown'},
        ]}]}
        for provider in ('deepseek', 'strata', 'strata-epyc')
    ]
    monkeypatch.setattr(server, '_spawn_catalog_providers', lambda: rows)
    monkeypatch.setattr(server, '_mcp_call', lambda *a, **k: pytest.fail('unexpected registration'))
    return rows


@pytest.fixture
def preview(monkeypatch, tmp_path, catalog):
    launcher = tmp_path / 'spawn_child.sh'
    launcher.write_text('#!/bin/sh\nexit 0\n')
    monkeypatch.setattr(server, 'SPAWN_SCRIPT', str(launcher))
    monkeypatch.setattr(server, '_project_key', lambda: '/fixture/project')
    return {'standalone': True, 'task': 'Original task with no managed prefix',
            'dir': str(tmp_path), 'dry_run': True}


@pytest.mark.parametrize('provider', ['deepseek', 'strata'])
@pytest.mark.parametrize('harness', ['dsh', 'hermes'])
def test_four_pairs_use_standard_launcher_and_unchanged_options(preview, provider, harness):
    result = server.do_spawn({**preview, 'provider': provider, 'harness': harness,
                             'model': provider + '-fixture', 'worktree': True,
                             'worktree_base': 'origin/main', 'headless': False})
    assert result['ok'] and result['dry_run']
    assert result['argv'][5:] == [
        '--standalone', '--harness', harness, '--backend', provider,
        '--model', provider + '-fixture', '--worktree', '--worktree-base', 'origin/main',
        preview['task'], preview['dir'],
    ]
    assert result['effort'] == ''
    assert result['launcher_env'] == {'AGENTSTACK_AUTO_OPEN_CHILD': '1'}
    assert not {'attempt_id', 'managed'} & result.keys()


def test_parent_task_and_explicit_effort_are_standard(preview):
    task = 'The task is passed unchanged'
    result = server.do_spawn({**preview, 'standalone': False, 'parent': 'FixtureParent',
                             'provider': 'deepseek', 'harness': 'dsh',
                             'model': 'deepseek-fixture', 'effort': 'high', 'task': task})
    assert result['ok']
    assert '--standalone' not in result['argv']
    assert result['argv'][5:13] == [
        '--harness', 'dsh', '--backend', 'deepseek', '--model', 'deepseek-fixture', '--effort', 'high']
    assert result['argv'][-2:] == [task, preview['dir']]


@pytest.mark.parametrize('changes', [
    {'harness': None}, {'harness': 'claude-code'}, {'harness': []},
    {'model': 'different-fixture'}, {'model': ''}, {'model': []},
    {'effort': 'invented'}, {'effort': []}, {'harness': 'hermes', 'effort': 'high'},
    {'helper_model': 'another-model'},
    {'allowed_paths': ['result.txt']}, {'tools': {'mcp': 1}},
    {'claude_chrome': True},
])
def test_invalid_or_unavailable_capabilities_never_register(preview, changes):
    response = server.do_spawn({**preview, 'provider': 'deepseek', 'harness': 'dsh',
                                'model': 'deepseek-fixture', **changes})
    assert response['ok'] is False


def test_availability_is_rechecked_at_post(preview, catalog):
    catalog[0]['models'][0]['harnesses'][0]['available'] = False
    result = server.do_spawn({**preview, 'provider': 'deepseek', 'harness': 'dsh',
                             'model': 'deepseek-fixture'})
    assert result['error_code'] == 'spawn_pair_unavailable'


@pytest.mark.parametrize(('provider', 'aliases'), [
    ('claude', ['claude-code', 'claudecode']), ('codex', ['codex-cli', 'codex']),
])
def test_native_aliases_keep_existing_dispatch(monkeypatch, provider, aliases):
    monkeypatch.setattr(server, '_spawn_request', lambda payload: (None, {'snapshot': payload}))
    payload = {'provider': provider, 'task': 'existing task', 'parent': 'FixtureParent'}
    expected = server.do_spawn(payload)
    for alias in aliases:
        assert server.do_spawn({**payload, 'harness': alias, 'helper_model': 'same'}) == expected


@pytest.mark.parametrize('provider,harness', [('deepseek', 'dsh'), ('strata', 'hermes'),
                                            ('strata-epyc', 'dsh'), ('strata-epyc', 'hermes')])
@pytest.mark.parametrize('asynchronous', [False, True])
def test_registration_binding_and_response_use_standard_name_contract(
        preview, monkeypatch, tmp_path, provider, harness, asynchronous):
    calls, launched = [], []
    monkeypatch.setattr(server, 'RUNTIME_DIR', str(tmp_path / 'runtime'))
    monkeypatch.setattr(server, 'HERE', str(tmp_path))
    monkeypatch.setattr(server, '_spawn_name_status', lambda _: 'available')
    monkeypatch.setattr(server, '_tmux_run', lambda *a, **k: SimpleNamespace(returncode=0))
    def mcp(method, args, **kwargs):
        calls.append((method, args))
        return {'ok': True, 'data': {'name': 'BrightCurie', 'id': 42}}
    monkeypatch.setattr(server, '_mcp_call', mcp)
    monkeypatch.setattr(server.subprocess, 'Popen', lambda args, **kwargs: launched.append((args, kwargs)))
    # No real launcher process: the shared readiness probe is also synthetic.
    result = server.do_spawn({**preview, 'dry_run': False, 'provider': provider, 'harness': harness,
                             'model': provider + '-fixture', 'name': 'Bright-Curie',
                             'async': asynchronous})
    assert result['ok']
    assert result['child_name'] == result['tmux_session'] == 'BrightCurie'
    assert not {'managed', 'attempt_id', 'state'} & result.keys()
    assert calls[0][0] == 'register_agent'
    assert calls[0][1]['program'] == harness
    args, env = launched[0]
    assert args[5:12] == ['--standalone', '--harness', harness, '--backend', provider,
                          '--model', provider + '-fixture']
    assert 'PARENT_AGENT' not in env['env']
    binding = json.loads(Path(args[4] + '.binding.json').read_text())
    assert binding == {'agent_name': 'BrightCurie', 'agent_id': 42,
                       'project_key': '/fixture/project', 'program': harness}
    if asynchronous:
        # Join the existing async launch worker before monkeypatch teardown.
        import threading
        for worker in threading.enumerate():
            if worker.name == 'spawn-BrightCurie':
                worker.join(timeout=2)
        assert server.spawn_launch_status('BrightCurie')['state'] == 'ready'


def test_standard_catalog_shares_i5_without_managed_ui_metadata(monkeypatch, tmp_path):
    capability = {'available': True, 'efforts': ['low', 'high'],
                  'default_effort': 'high', 'effort_source': 'harness',
                  '_selection': {'config_file': 'fixture'}}
    monkeypatch.setattr(managed_catalog, '__file__', str(tmp_path / 'managed_catalog.py'))
    monkeypatch.setattr(managed_catalog, 'load_standard_inputs', lambda *a, **k: (
        {'deepseek': {'model': 'deepseek-fixture', 'effort': 'low'}},
        {'deepseek': {('deepseek-fixture', 'dsh'): capability}}, True))
    row = managed_catalog.standard_catalog('fixture-manifest', mode='legacy')[0]
    assert 'managed' not in row and 'helper_models' not in row
    selected = row['models'][0]['harnesses'][0]
    assert selected['efforts'] == ['low', 'high']
    assert selected['default_effort'] == 'high'  # cache, not the trial input
    assert selected['effort_source'] == 'harness'
    assert 'task_modes' not in selected


def test_standard_catalog_is_off_without_config():
    assert managed_catalog.standard_catalog(None, mode='legacy') == []


def test_explicit_helper_uses_selected_harness_catalog(preview, catalog):
    selected = catalog[0]['models'][0]['harnesses'][0]
    selected['helper_models'] = [{'id': 'same'}, {'id': 'small-fixture'}]
    payload = {**preview, 'provider': 'deepseek', 'harness': 'dsh',
               'model': 'deepseek-fixture', 'helper_model': 'small-fixture'}
    result = server.do_spawn(payload)
    assert result['ok']
    index = result['argv'].index('--helper-model')
    assert result['argv'][index + 1] == 'small-fixture'
    assert '--helper-model' not in server.do_spawn({**payload, 'helper_model': 'same'})['argv']
    selected['helper_models'] = [{'id': 'same'}]
    rejected = server.do_spawn(payload)
    assert not rejected['ok'] and rejected['error_code'] == 'invalid_helper_model'


@pytest.mark.parametrize('provider,harness', [('claude', None), ('codex', None), ('deepseek', 'dsh')])
@pytest.mark.parametrize('cleanup_ok', [False, True])
def test_cleanup_result_preserves_canonical_state(preview, monkeypatch, tmp_path, provider, harness, cleanup_ok):
    runtime = tmp_path / 'runtime'
    monkeypatch.setattr(server, 'RUNTIME_DIR', str(runtime))
    monkeypatch.setattr(server, 'HERE', str(tmp_path))
    monkeypatch.setattr(server, '_spawn_name_status', lambda _: 'available')
    monkeypatch.setattr(server, '_mcp_call', lambda *a, **k: {'ok': True, 'data': {
        'id': 73, 'name': 'FixtureAgent', 'registration_token': 'fixture-token'}})
    class Proc:
        def wait(self, timeout=None):
            return 2
    monkeypatch.setattr(server.subprocess, 'Popen', lambda *a, **k: Proc())
    monkeypatch.setattr(server, '_tmux_run', lambda *a, **k: SimpleNamespace(returncode=0))
    owner = runtime / 'agent_token_FixtureAgent'
    state = runtime / 'child-agents/FixtureAgent.json'
    def cleanup(script, name, environment):
        owner.write_text('fixture-token')
        state.parent.mkdir(parents=True, exist_ok=True)
        state.write_text('{"fixture":true}')
        return cleanup_ok
    monkeypatch.setattr(server, '_failed_spawn_cleanup', cleanup)
    payload = {**preview, 'provider': provider, 'name': 'FixtureAgent', 'dry_run': False}
    spec = server.SpawnLaunchSpec(provider=provider,
        program=harness or ('claude-code' if provider == 'claude' else 'codex-cli'),
        model='fixture-model', script=server.SPAWN_SCRIPT)
    result = server.spawn_with_launch_spec(payload, spec)
    assert not result['ok']
    if cleanup_ok:
        assert 'cleanup_incomplete' not in result
    else:
        assert result['cleanup_state'] == 'unknown'
    assert owner.read_text() == 'fixture-token' and state.read_text() == '{"fixture":true}'
    assert len(list((runtime / 'spawn-tokens').glob('*.token'))) == (0 if cleanup_ok else 1)


def test_cleanup_helper_uses_only_launcher_sibling_and_fixed_timeout(monkeypatch, tmp_path):
    launcher = tmp_path / 'spawn_child.sh'
    assert not server._failed_spawn_cleanup(str(launcher), 'FixtureAgent', {})
    helper = tmp_path / 'cleanup-child-agent.sh'
    helper.write_text('#!/bin/bash\nexit 1\n')
    def run(argv, **kwargs):
        assert argv == ['/bin/bash', str(helper), 'FixtureAgent']
        assert kwargs['timeout'] == 30 and kwargs['env'] == {'FIXTURE': '1'}
        return SimpleNamespace(returncode=1)
    monkeypatch.setattr(server.subprocess, 'run', run)
    assert not server._failed_spawn_cleanup(str(launcher), 'FixtureAgent', {'FIXTURE': '1'})


@pytest.mark.parametrize('harness', ['dsh', 'hermes'])
def test_native_tools_use_shared_program_contract(preview, harness):
    result = server.do_spawn({**preview, 'provider': 'strata', 'harness': harness,
        'model': 'strata-fixture', 'base': 'mail-only', 'tools': {'mcp': ['fixture']}})
    assert result['ok']
    argv = result['argv']
    assert argv[argv.index('--base') + 1] == 'mail-only'
    assert argv[argv.index('--tools') + 1] == 'mcp:fixture'
    default = server.do_spawn({**preview, 'provider': 'strata', 'harness': harness,
        'model': 'strata-fixture', 'tools': {}})
    assert default['argv'][default['argv'].index('--base') + 1] == 'default'


@pytest.mark.parametrize('mode', ['kill_failed', 'update', 'creation_pending',
    'probe_unknown', 'promote_failed', 'confirmed', 'confirmed_absent', 'retire_failed'])
def test_failed_spawn_uses_real_cleanup_only_after_stop_and_owner_confirmation(
        preview, monkeypatch, tmp_path, mode):
    import os
    import subprocess
    import sys
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    root = Path(__file__).resolve().parents[1]
    runtime = tmp_path / 'runtime'
    home = tmp_path / 'home'
    home.mkdir()
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.setenv('AGENTSTACK_HOME', str(root))
    monkeypatch.setenv('AGENTSTACK_RUNTIME_MODE', 'legacy')
    monkeypatch.setenv('AGENTSTACK_LABEL_PREFIX', 'org.agentstack.test-spawn-review')
    monkeypatch.setenv('AGENTSTACK_MAIL_HTTP_BEARER_MODE', 'disabled')
    monkeypatch.setenv('AGENTSTACK_CHILD_RESUME_RETENTION_DAYS', '0')
    monkeypatch.setenv('AGENTSTACK_PYTHON', sys.executable)
    monkeypatch.setenv('PATH', str(Path(sys.executable).parent) + os.pathsep + '/usr/bin:/bin')
    monkeypatch.setattr(server, 'RUNTIME_DIR', str(runtime))
    monkeypatch.setattr(server, 'HERE', str(tmp_path))
    monkeypatch.setattr(server, '_spawn_name_status', lambda _: 'available')
    monkeypatch.setattr(server, '_mcp_call', lambda *a, **k: {'ok': True, 'data': {
        'id': 73, 'name': 'FixtureAgent', 'registration_token': 'fixture-token'}})
    owner = runtime / 'agent_token_FixtureAgent'
    state = runtime / 'child-agents/FixtureAgent.json'
    calls, cleanup_calls, killed = [], [], []
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            method = request['params']['name']
            calls.append(method)
            if method == 'release_file_reservations':
                result = {'released': 0}
            elif method == 'retire_agent':
                assert request['params']['arguments']['registration_token'] == 'fixture-token'
                result = ({'isError': True} if mode == 'retire_failed' else {
                    'status': 'retired', 'agent_name': 'FixtureAgent', 'project_key': '/fixture/project'})
            else:
                assert method == 'whois'
                result = {'name': 'FixtureAgent', 'retired_at': '2026-10-20T00:00:00Z'}
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps({'result': result}).encode())
        def log_message(self, *args):
            pass
    mail = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=mail.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv('AGENTSTACK_MCP_URL', 'http://127.0.0.1:' + str(mail.server_port) + '/mcp')
    launcher = str(root / 'hooks/spawn_child.sh')
    real_popen = subprocess.Popen
    class Proc:
        def wait(self, timeout=None):
            return 2
    def popen(argv, **kwargs):
        if argv[0] != launcher:
            return real_popen(argv, **kwargs)
        if mode in ('kill_failed', 'update', 'creation_pending', 'probe_unknown'):
            owner.write_text('fixture-token')
            state.parent.mkdir(parents=True, exist_ok=True)
            state.write_text(json.dumps({'project_key': '/fixture/project', 'registration_token': 'fixture-token'}))
        if mode == 'creation_pending':
            kwargs['stderr'].write(b'Harness start/stop unconfirmed; recovery state retained\n')
            kwargs['stderr'].flush()
        return Proc()
    monkeypatch.setattr(server.subprocess, 'Popen', popen)
    def tmux(argv, **kwargs):
        if argv[0] == 'has-session':
            if mode == 'probe_unknown':
                return SimpleNamespace(returncode=1, stderr='socket permission denied')
            if mode == 'confirmed_absent':
                return SimpleNamespace(returncode=1, stderr="can't find session: FixtureAgent")
            return SimpleNamespace(returncode=0, stderr='')
        assert argv[0] == 'kill-session'
        killed.append(argv)
        return SimpleNamespace(returncode=1 if mode == 'kill_failed' else 0)
    monkeypatch.setattr(server, '_tmux_run', tmux)
    monkeypatch.setattr(server, '_codex_self_update_on_screen', lambda _: mode == 'update')
    real_cleanup = server._failed_spawn_cleanup
    def cleanup(*args):
        cleanup_calls.append(args[1])
        return real_cleanup(*args)
    monkeypatch.setattr(server, '_failed_spawn_cleanup', cleanup)
    if mode == 'promote_failed':
        monkeypatch.setattr(server, '_promote_spawn_credential', lambda *a: False)
    spec = server.SpawnLaunchSpec(provider='codex', program='codex-cli',
        model='fixture-model', script=launcher)
    try:
        result = server.spawn_with_launch_spec({**preview, 'dry_run': False,
            'provider': 'codex', 'name': 'FixtureAgent'}, spec)
    finally:
        mail.shutdown()
        mail.server_close()
        thread.join(timeout=5)
    assert not result['ok']
    handoffs = list((runtime / 'spawn-tokens').glob('*.token'))
    if mode in ('confirmed', 'confirmed_absent', 'retire_failed'):
        assert cleanup_calls == ['FixtureAgent']
        assert calls == (['release_file_reservations', 'retire_agent'] if mode == 'retire_failed'
                         else ['release_file_reservations', 'retire_agent', 'whois'])
        assert bool(handoffs) is (mode == 'retire_failed')
    else:
        assert not cleanup_calls and not calls
        assert result['cleanup_state'] == 'unknown'
        assert len(handoffs) == 1 and handoffs[0].read_text() == 'fixture-token'
        assert Path(str(handoffs[0]) + '.binding.json').exists()
        if mode != 'promote_failed':
            assert owner.read_text() == 'fixture-token' and state.exists()
    if mode in ('update', 'creation_pending', 'probe_unknown', 'confirmed_absent'):
        assert not killed



def test_log_directory_failure_returns_unknown_and_preserves_handoff(preview, monkeypatch, tmp_path):
    runtime = tmp_path / 'runtime'
    monkeypatch.setattr(server, 'RUNTIME_DIR', str(runtime))
    monkeypatch.setattr(server, 'HERE', str(tmp_path))
    monkeypatch.setattr(server, '_spawn_name_status', lambda _: 'available')
    monkeypatch.setattr(server, '_mcp_call', lambda *a, **k: {'ok': True, 'data': {
        'id': 73, 'name': 'FixtureAgent', 'registration_token': 'fixture-token'}})
    monkeypatch.setattr(server.subprocess, 'Popen', lambda *a, **k: pytest.fail('launcher started without log'))
    monkeypatch.setattr(server, '_tmux_run', lambda *a, **k: pytest.fail('session ownership unknown before launch'))
    monkeypatch.setattr(server, '_failed_spawn_cleanup', lambda *a: pytest.fail('cleanup before launch'))
    real_makedirs = server.os.makedirs
    def makedirs(path, *args, **kwargs):
        if Path(path) == tmp_path / 'logs':
            raise PermissionError('fixture logs directory denied')
        return real_makedirs(path, *args, **kwargs)
    monkeypatch.setattr(server.os, 'makedirs', makedirs)
    spec = server.SpawnLaunchSpec(provider='deepseek', program='dsh',
        model='deepseek-fixture', script=server.SPAWN_SCRIPT)
    result = server.spawn_with_launch_spec({**preview, 'dry_run': False,
        'provider': 'deepseek', 'name': 'FixtureAgent'}, spec)
    assert result['ok'] is False and result['cleanup_state'] == 'unknown'
    assert 'fixture logs directory denied' in result['error']
    handoffs = list((runtime / 'spawn-tokens').glob('*.token'))
    assert len(handoffs) == 1 and handoffs[0].read_text() == 'fixture-token'
    assert Path(str(handoffs[0]) + '.binding.json').exists()


def test_retired_attempt_request_is_rejected_before_standard_spawn(catalog):
    result = server.do_spawn({'attempt_id': 'old-attempt', 'provider': 'deepseek',
                              'harness': 'dsh', 'model': 'deepseek-fixture'})
    assert result['ok'] is False
    assert result['error_code'] == 'unsupported_attempt'


@pytest.mark.parametrize('query', ['attempt_id=old', 'attempt_id=', 'name=Child&attempt_id=old'])
def test_retired_attempt_status_is_rejected_without_standard_lookup(monkeypatch, query):
    monkeypatch.setattr(server, 'GLOBAL_MODE', False)
    monkeypatch.setattr(server, 'spawn_launch_status', lambda *a: pytest.fail('unexpected name lookup'))
    monkeypatch.setattr(server, 'spawn_launch_statuses', lambda: pytest.fail('unexpected list lookup'))
    responses = []
    request = SimpleNamespace(path='/api/spawn-status?' + query,
                              _send=lambda *args: responses.append(args))
    server.Handler.do_GET(request)
    code, body, _ = responses.pop()
    assert code == 404
    assert json.loads(body)['error_code'] == 'unsupported_attempt'
