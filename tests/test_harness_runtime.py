import json
from types import SimpleNamespace

import pytest

from dashboard import harness_runtime as runtime, managed_catalog, server


def test_ss_hermes_route_metadata_does_not_require_legacy_key_cmd():
    from test_hermes_capabilities import HermesCapabilities
    fixture = HermesCapabilities()
    fixture.setUp()
    try:
        for profile in fixture.template['profiles'].values():
            del profile['key_cmd']
        assert fixture.load()['efforts'] == ['none', 'low', 'medium', 'high', 'max']
        assert fixture.load()['default_effort'] is None
    finally:
        fixture.doCleanups()


@pytest.mark.parametrize('with_ref,mode,expected', [(True, 'legacy', True), (False, 'legacy', False),
                                               (True, 'global', False)])
def test_linux_catalog_requires_explicit_ss_reference_without_looking_up_key(tmp_path, monkeypatch, with_ref, mode, expected):
    manifest, config = tmp_path / 'manifest.json', tmp_path / 'config.json'
    config.write_text('{}')
    backend = {'model': 'fixture'}
    if with_ref:
        backend['credential_ref'] = {'store': 'secret-service', 'attributes': {'service': 'fixture'}}
    manifest.write_text(json.dumps({'enabled': True, 'backends': {'deepseek': backend}}))
    monkeypatch.setattr(managed_catalog.sys, 'platform', 'linux')
    monkeypatch.setattr(managed_catalog, '__file__', str(tmp_path / 'managed_catalog.py'))
    calls = []
    def selection(*args):
        calls.append(args)
        return {'config_file': str(config)}
    monkeypatch.setattr(managed_catalog, '_thin_selection', selection)
    monkeypatch.setattr(managed_catalog, 'load_effort_capability', lambda *a, **k: {})
    rows = managed_catalog.standard_catalog(str(manifest), mode=mode)
    choices = rows[0]['models'][0]['harnesses']
    assert all(row['available'] is expected for row in choices)
    assert len(calls) == (2 if expected else 0)


@pytest.mark.parametrize('program,command,expected', [
    ('dsh', '/fixed/node /fixed/node_modules/@deepseek-ai/dsh/lib/bin.js --profile x', True),
    ('dsh', '/fixed/node /fixed/mcp/server.js', False),
    ('dsh', 'node', False),
    ('hermes', '/fixed/venv/bin/python /fixed/venv/bin/hermes chat', True),
    ('hermes', 'python3 -m hermes_cli.main chat', True),
    ('hermes', 'hermes', True),
    ('hermes', 'python3 /fixed/mcp.py', False),
    ('hermes', 'zsh', False),
])
def test_native_entry_identity(program, command, expected):
    assert runtime.matches_command(program, command) is expected


@pytest.mark.parametrize('command,returncode,expected', [
    ('node /fixed/node_modules/@deepseek-ai/dsh/lib/bin.js', 0, True),
    ('node /fixed/mcp.js', 0, False),
    ('python /fixed/node_modules/@deepseek-ai/dsh/lib/bin.js', 0, False),
    ('node', 0, False),
    ('node "unterminated', 0, False),
    ('', 1, None),
])
@pytest.mark.parametrize('platform,comm', [
    ('linux', 'node'), ('linux', 'MainThread'), ('darwin', 'node'),
])
def test_only_pane_descendants_count(monkeypatch, command, returncode, expected, platform, comm):
    monkeypatch.setattr(runtime, 'sys', SimpleNamespace(platform=platform))
    server._NATIVE_COMMAND_CACHE.clear()
    tree = server._parse_process_tree(f'10 1 bash\n11 10 {comm}\n50 1 {comm}\n')
    calls = []

    def ps(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=returncode, stdout=command)

    monkeypatch.setattr(server.subprocess, 'run', ps)
    assert server._agent_process_alive(10, tree, 'dsh') is expected
    assert calls == [['ps', '-p', '11', '-o', 'args=']]
    assert server.classify('FixtureAgent', 'node', '', True, 'dsh', expected) == (
        'finished' if expected is False else 'agent')


@pytest.mark.parametrize('platform,program,comm', [
    ('darwin', 'dsh', 'MainThread'),
    ('linux', 'dsh', 'OtherThread'),
    ('linux', 'hermes', 'MainThread'),
])
def test_unrelated_process_names_never_reach_native_argv(monkeypatch, platform, program, comm):
    monkeypatch.setattr(runtime, 'sys', SimpleNamespace(platform=platform))
    server._NATIVE_COMMAND_CACHE.clear()
    tree = server._parse_process_tree(f'10 1 bash\n11 10 {comm}\n')
    monkeypatch.setattr(server.subprocess, 'run', lambda *a, **k: pytest.fail('unexpected args probe'))
    assert server._agent_process_alive(10, tree, program) is False


@pytest.mark.parametrize('comm', ['node', 'MainThread'])
def test_native_process_outside_the_pane_is_not_probed(monkeypatch, comm):
    monkeypatch.setattr(runtime, 'sys', SimpleNamespace(platform='linux'))
    server._NATIVE_COMMAND_CACHE.clear()
    tree = server._parse_process_tree(f'10 1 bash\n11 10 sh\n50 1 {comm}\n')
    monkeypatch.setattr(server.subprocess, 'run', lambda *a, **k: pytest.fail('foreign process probe'))
    assert server._agent_process_alive(10, tree, 'dsh') is False


def test_backend_metadata_never_uses_model_vendor(tmp_path):
    directory = tmp_path / 'child-agents'
    directory.mkdir()
    file = directory / 'FixtureAgent.json'
    record = dict(agent_name='FixtureAgent', program='hermes', harness='hermes',
                  harness_backend='strata', model='claude-looking-model')
    file.write_text(json.dumps(record))
    assert runtime.metadata(tmp_path, 'FixtureAgent', 'hermes') == {
        'harness': 'hermes', 'provider': 'strata', 'locality': 'local'}
    record['agent_name'] = 'OtherAgent'
    file.write_text(json.dumps(record))
    assert runtime.metadata(tmp_path, 'FixtureAgent', 'hermes')['provider'] == ''
    file.unlink()
    file.symlink_to('/missing-fixture')
    assert runtime.metadata(tmp_path, 'FixtureAgent', 'hermes')['provider'] == ''


@pytest.mark.parametrize('alive', [True, False, None])
def test_views_and_controls_agree_on_native_liveness(monkeypatch, tmp_path, alive):
    name = 'FixtureAgent'
    session = dict(name=name, created=100, session_id='$1', activity=200,
                   attached=False, client_tty=None, cmd='node', pane_pid=10,
                   title='unparsed native TUI')
    registration = dict(model='gpt-fixture', model_raw='gpt-fixture', program='dsh',
                        task='fixture', last_active=150)
    directory = tmp_path / 'child-agents'
    directory.mkdir()
    (directory / (name + '.json')).write_text(json.dumps(dict(
        agent_name=name, program='dsh', harness='dsh', harness_backend='strata')))
    monkeypatch.setattr(server, 'RUNTIME_DIR', str(tmp_path))
    monkeypatch.setattr(server, 'tmux_state', lambda: {name: session})
    monkeypatch.setattr(server, 'agentmail_state', lambda: ({name: registration}, {}))
    monkeypatch.setattr(server, '_mail_agent_for', lambda _: registration)
    monkeypatch.setattr(server, '_process_tree_snapshot', lambda: ({10: 'node'}, {}))
    monkeypatch.setattr(server, '_agent_process_alive', lambda *a: alive)
    for function in ('_codex_app_runtimes', '_deliverables_index', '_name_substitutions',
                     '_annotations', '_persistent_profiles'):
        monkeypatch.setattr(server, function, lambda: {})
    monkeypatch.setattr(server, '_retired_names', lambda *a: set())
    monkeypatch.setattr(server, '_project_key', lambda: '')
    monkeypatch.setattr(server, '_ctx_window', lambda *a: 'FORBIDDEN_FALLBACK')
    monkeypatch.setattr(server, '_resume_capability_for_row', lambda *a, **k: 'not_required')
    monkeypatch.setattr(server, '_provider_stall_view', lambda *a: None)
    monkeypatch.setattr(server, '_agent_runtime', lambda *a: pytest.fail('foreign TUI parser'))
    monkeypatch.setattr(server, '_native_context_runtime', lambda *a: {'ctx_used': None, 'ctx_window': None})
    monkeypatch.setattr(server, '_raw_graph', lambda: dict(nodes=[dict(
        name=name, program='dsh', model='gpt-fixture', last_active=200, retired=False)],
        edges=[], spawn=[]))
    roster = server.build_agents()[0]
    lookup = server.lookup_agent(name)
    node = server.graph_payload(4, True)['nodes'][0]
    for row in (roster, lookup, node):
        assert row['running'] is (alive is True)
        assert row['provider'] == 'strata' and row['harness'] == 'dsh'
        assert row.get('ctx_window') is None
    assert roster['category'] == lookup['category'] == ('finished' if alive is False else 'agent')
    assert node['state'] == ('unknown' if alive is None else 'run' if alive else 'finished')
    if alive is not False:
        assert server.do_kill(name)['ok'] is False


def test_catalog_uses_shared_selection_and_capability_cache(monkeypatch, tmp_path):
    manifest = tmp_path / 'manifest.json'
    config = tmp_path / 'config.json'
    config.write_text('{}')
    manifest.write_text(json.dumps({'enabled': True, 'backends': {
        'deepseek': {'model': 'fixture-model'}}}))
    monkeypatch.setattr(managed_catalog, '__file__', str(tmp_path / 'managed_catalog.py'))
    monkeypatch.setattr(managed_catalog.sys, 'platform', 'darwin')
    calls = []

    def select(file, harness, backend, model):
        calls.append((file, harness, backend, model))
        if harness == 'hermes':
            raise ValueError('plain_generation_required')
        return {'config_file': str(config)}

    monkeypatch.setattr(managed_catalog, '_thin_selection', select)
    monkeypatch.setattr(managed_catalog, 'load_effort_capability', lambda *a, **k: {
        'efforts': ['low', 'high'], 'default_effort': 'high', 'effort_source': 'harness'})
    row = managed_catalog.standard_catalog(str(manifest), mode='legacy')[0]
    choices = row['models'][0]['harnesses']
    assert choices[0]['available'] is True and choices[0]['efforts'] == ['low', 'high']
    assert choices[1]['available'] is False
    assert choices[1]['reason'] == 'plain_generation_required'
    assert len(calls) == 2
    (tmp_path / 'dsh-provider.json').write_text('{"enabled":false}')
    assert not managed_catalog.standard_catalog(str(manifest), mode='legacy')[0]['models'][0]['harnesses'][0]['available']


def test_shared_helper_import_is_independent_of_cwd(monkeypatch, tmp_path):
    hooks = tmp_path / 'hooks'
    hooks.mkdir()
    dashboard = tmp_path / 'dashboard'
    dashboard.mkdir()
    (hooks / 'child_resume.py').write_text('FIXTURE = 17\n')
    (hooks / 'harness_child.py').write_text(
        'from . import child_resume\n'
        'def selection(*args): return child_resume.FIXTURE, args\n')
    monkeypatch.setattr(managed_catalog, '__file__', str(dashboard / 'managed_catalog.py'))
    monkeypatch.chdir('/')
    assert managed_catalog._thin_selection('manifest', 'dsh', 'strata', 'model') == (
        17, ('manifest', 'dsh', 'strata', 'model'))


def test_each_model_gets_its_own_capability_and_helper_choices(monkeypatch, tmp_path):
    manifest = tmp_path / 'manifest.json'
    config = tmp_path / 'config.json'
    config.write_text('{}')
    manifest.write_text(json.dumps({'enabled': True, 'backends': {'strata': {
        'model': 'main-model', 'models': ['main-model', 'small-model']}}}))
    monkeypatch.setattr(managed_catalog, '__file__', str(tmp_path / 'managed_catalog.py'))
    monkeypatch.setattr(managed_catalog.sys, 'platform', 'darwin')
    monkeypatch.setattr(managed_catalog.harness_helpers, 'generator_supports_helpers', lambda *a: True)
    selected = []
    def select(file, harness, backend, model):
        selected.append((model, harness))
        if harness == 'hermes':
            raise ValueError('generator_unavailable')
        return {'config_file': str(config)}
    monkeypatch.setattr(managed_catalog, '_thin_selection', select)
    monkeypatch.setattr(managed_catalog, 'load_effort_capability', lambda *a, **k: {
        'efforts': ['high'] if k['model'] == 'main-model' else ['low'],
        'default_effort': None, 'effort_source': 'harness'})
    row = managed_catalog.standard_catalog(str(manifest), mode='legacy')[0]
    first, second = row['models']
    assert first['harnesses'][0]['efforts'] == ['high']
    assert second['harnesses'][0]['efforts'] == ['low']
    assert [entry['id'] for entry in second['harnesses'][0]['helper_models']] == [
        'same', 'main-model', 'small-model']
    assert second['harnesses'][1]['available'] is False
    assert len(selected) == 4
    assert '_selection' not in json.dumps(row)


def test_native_context_unknown_never_reuses_previous_footer(monkeypatch):
    server._NATIVE_CONTEXT_CACHE.clear()
    clock = [100.0]
    capture = ['ctx 19% (24k/128k)']
    monkeypatch.setattr(server.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(server, '_tmux_run', lambda *a, **k: SimpleNamespace(
        returncode=0, stdout=capture[0]))
    first = server._native_context_runtime('FixtureAgent', 100, '$1', 'dsh')
    assert first == {'ctx_used': 19, 'ctx_window': '128K', 'act_state': None,
                     'dsh_card_effort': None}
    capture[0] = 'footer hidden'
    clock[0] += server._RT_TTL + 1
    assert server._native_context_runtime('FixtureAgent', 100, '$1', 'dsh') == {
        'ctx_used': None, 'ctx_window': None, 'act_state': None, 'dsh_card_effort': None}
