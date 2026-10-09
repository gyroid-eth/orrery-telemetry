"""PR4a actual entrypoints against the committed isolated S1 HTTP/socket.

The synthetic PR3 database is created by S1's canonical fixture. Providers
are shell stubs; no installed Mail DB or actual model process is used.
"""
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import runpy
import shlex
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
api = runpy.run_path(str(ROOT / 'bin/lib/runtime_client.py'))
RuntimeClient, ClientError = api['RuntimeClient'], api['ClientError']
private = api['atomic_json']
spec = importlib.util.spec_from_file_location('s1_client_fixture',
    ROOT / 'packages/agentstack_mail/tests/test_global_server.py')
s1 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(s1)


@pytest.fixture
def prepared(monkeypatch):
    fixture = s1.server.__wrapped__(monkeypatch)
    state = next(fixture)
    try:
        c = state['config']
        client_home = state['root'] / 'agentstack'
        client_home.mkdir(mode=0o700)
        (client_home / 'clients').mkdir(mode=0o700)
        client_root = client_home / 'clients/client'
        client_root.mkdir(mode=0o700)
        (client_root / 'runtime').mkdir(mode=0o700)
        info = Path(c['authority_lock']).stat()
        config = {'kind': api['KIND'], 'mode': 'global', 'activation_enabled': False,
                  'wrapper_root': str(ROOT), 'isolation_root': c['isolation_root'],
                  'runtime_root': c['runtime_root'], 'agentstack_home': str(client_home), 'client_name': 'client', 'authority': c['authority'],
                  'authority_lock': c['authority_lock'], 'lock_identity': [info.st_dev, info.st_ino],
                  'management_socket': c['management_socket'],
                  'mcp_url': state['url'], **state['binding'],
                  'identity': {'agent_id': 1, 'credential_generation': 3, 'name': 'OldAlpha'}}
        # Read only synthetic migration fixture metadata to obtain preserved
        # credential values. Never query a real Mail database.
        from agentstack_mail.namespace_state_io import connection
        with connection(Path(c['database'])) as db:
            row = dict(db.execute('SELECT * FROM agents WHERE id=1').fetchone())
        config['identity']['credential_generation'] = row['credential_generation']
        private(client_root / 'runtime-client.json', config)
        private(local_root(config) / 'credential.json', {'kind': 'orrery-global-credential-v1', **state['binding'],
             **config['identity'], 'registration_token': row['registration_token']})
        state.update(client_path=client_root / 'runtime-client.json', client_config=config,
                     token=row['registration_token'])
        monkeypatch.setenv('AGENTSTACK_CLIENT_CONFIG', str(state['client_path']))
        monkeypatch.setenv('AGENTSTACK_PROJECT_KEY', '/obsolete/scope')
        monkeypatch.setenv('PROJECT_KEY', '/another/obsolete/scope')
        monkeypatch.setenv('AGENT_NAME', 'UnrelatedName')
        monkeypatch.setenv('AGENTSTACK_MCP_URL', 'http://127.0.0.1:1/mcp')
        yield state
    finally:
        fixture.close()


def fresh_context(state, config, name):
    root = Path(config['agentstack_home']) / 'clients' / name
    root.mkdir(mode=0o700)
    (root / 'runtime').mkdir(mode=0o700)
    config['client_name'] = name
    state['client_path'] = root / 'runtime-client.json'
    private(state['client_path'], config)
    os.environ['AGENTSTACK_CLIENT_CONFIG'] = str(state['client_path'])


def local_root(config):
    return Path(config['agentstack_home']) / 'clients' / config['client_name']


def client(state):
    return RuntimeClient(state['client_path'])


def run(state, tool, *args, bash=False, extra=None):
    env = dict(os.environ)
    env['PATH'] = str(ROOT / '.venv/bin') + os.pathsep + env['PATH']
    if extra:
        env.update(extra)
    command = ['/bin/bash', str(ROOT / tool)] if bash else [sys.executable, str(ROOT / tool)]
    return subprocess.run([*command, *args], text=True, capture_output=True, env=env,
                          cwd=state['root'], timeout=30)


def test_real_s1_reconnect_ignores_old_scope_and_name(prepared):
    c = client(prepared)
    before = c.call('health_check')['mutation_revision']
    row = c.reconnect(program='fixture', model='fixture')
    assert row['agent_id'] == 1
    assert row['name'] != 'UnrelatedName'
    assert c.call('health_check')['mutation_revision'] == before + 1
    assert c.local_owner() == prepared['token']
    r = run(prepared, 'bin/agentstack-reregister', 'UnrelatedName', bash=True)
    assert r.returncode == 0, r.stderr
    assert row['name'] in r.stdout
    assert prepared['token'] not in r.stdout + r.stderr


def test_schema_matches_committed_s1(prepared):
    _, schemas = client(prepared).capabilities()
    fixture = json.loads((ROOT / 'packages/agentstack_mail/fixtures/global-server-s1.json').read_text())
    assert schemas == fixture['tool_schemas']
    assert 'set_contact_policy' not in schemas
    with pytest.raises(ClientError, match='CAPABILITY_UNAVAILABLE'):
        client(prepared).call('set_contact_policy', {'policy': 'open'})


def test_rename_is_display_not_owner_routing(prepared):
    from agentstack_mail.namespace_state_io import connection
    with connection(Path(prepared['config']['database']), write=True) as db:
        db.execute("UPDATE agents SET name='RenamedOwner',lookup_key='renamedowner' WHERE id=1")
    assert client(prepared).reconnect()['name'] == 'RenamedOwner'
    r = run(prepared, 'hooks/resolve-agent-name.sh', bash=True)
    assert r.returncode == 0
    payload = run(prepared, 'bin/lib/runtime_client.py', 'observe')
    assert json.loads(payload.stdout)['name'] == 'RenamedOwner'


@pytest.mark.parametrize('change', ['quiescing', 'retired', 'epoch', 'root'])
@pytest.mark.parametrize('mode', ['global', 'legacy'])
def test_old_wrapper_restart_fenced_before_any_registration(prepared, change, mode):
    c = prepared['client_config']
    c['mode'] = mode
    private(prepared['client_path'], c)
    authority = api['read_json'](c['authority'])
    key, value = {'quiescing': ('phase', 'quiescing'), 'retired': ('root_status', 'retired'),
                  'epoch': ('authority_epoch', 'stale'), 'root': ('runtime_root', '/old/root')}[change]
    authority[key] = value
    private(Path(c['authority']), authority)
    r = run(prepared, 'bin/agentstack-reregister', 'Ignored', bash=True)
    assert r.returncode != 0
    assert 'WRITER_FENCED' in r.stderr


def test_lock_replacement_and_exclusive_updater_gate(prepared):
    lock = Path(prepared['config']['authority_lock'])
    c = client(prepared)
    with lock.open('rb') as fd:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(ClientError, match='WRITER_FENCED'):
            c.reconnect()
        replacement = lock.with_suffix('.replacement')
        replacement.touch(mode=0o600)
        os.replace(replacement, lock)
        with pytest.raises(ClientError, match='AUTHORITY_LOCK_REPLACED'):
            client(prepared).reconnect()


@pytest.mark.parametrize('key,value', [('expected_server_instance_id', 'other-instance'),
                                     ('candidate_generation', 'other-candidate'),
                                     ('authority_epoch', 'other-epoch'),
                                     ('agent_id', 999), ('credential_generation', 99)])
def test_credential_binding_cannot_select_another_owner(prepared, key, value):
    path = local_root(prepared['client_config']) / 'credential.json'
    credential = api['read_json'](path)
    credential[key] = value
    private(path, credential)
    reason = 'OUTPUT_OWNER_CONFLICT' if key == 'agent_id' else 'CREDENTIAL_BINDING_MISMATCH' if key == 'credential_generation' else 'OUTPUT_SCHEMA_INVALID'
    with pytest.raises(ClientError, match=reason):
        client(prepared).reconnect()


def test_new_registration_atomic_conflict_and_no_name_preflight(prepared):
    c = prepared['client_config']
    c['identity'] = None
    fresh_context(prepared, c, 'new-client')
    instance = client(prepared)
    canonical = s1.call(prepared, 'whois', **prepared['binding'], agent_id=1, registration_token=prepared['token'])['name']
    with pytest.raises(ClientError, match='NAME_CONFLICT'):
        instance.create(canonical, 'codex', 'fixture')
    assert not instance.credential.with_suffix('.registration-pending.json').exists()
    created = instance.create('NewIndependent', 'codex', 'fixture')
    assert created['agent_id'] != 1
    assert created['credential_generation'] == 1
    assert client(prepared).observe()['name'] == 'NewIndependent'


def test_operator_recovery_in_dedicated_home(prepared, tmp_path):
    c = client(prepared)
    previous = c.identity['credential_generation']
    r = run(prepared, 'bin/agentstack-enroll', '--global-context', str(c.path), 'recover',
            'fixture-recover', str(previous), bash=True,
            extra={'HOME': str(tmp_path)})
    assert r.returncode == 0, r.stderr
    receipt = json.loads(r.stdout)
    assert receipt['new_generation'] == previous + 1
    assert client(prepared).observe()['credential_generation'] == previous + 1
    assert prepared['token'] not in r.stdout + r.stderr
    status = client(prepared).management('request_status', request_id='fixture-recover')
    assert status == receipt


def test_inbox_await_uses_bound_owner_and_preserves_read_ack(prepared):
    r = run(prepared, 'bin/agentstack-await-reply', '--agent-name', 'UnrelatedName',
            '--project-key', '/obsolete/scope', '--timeout', '0.1', '--interval', '0.02')
    assert r.returncode in {0, 124}, r.stderr
    assert 'TRANSPORT_FAILED' not in r.stderr
    actual = client(prepared).call('fetch_inbox')
    assert isinstance(actual, list)
    assert client(prepared).call('fetch_inbox') == actual


@pytest.mark.parametrize('provider,entry', [('codex', 'bin/agentstack-codex-bootstrap'),
                                         ('gemini', 'bin/agentstack-gemini-bootstrap')])
def test_source_bootstrap_exports_canonical_identity_without_exec(prepared, provider, entry):
    command = '. ' + shlex.quote(str(ROOT / entry)) + '; printf "%s|%s|%s" "$AGENT_NAME" "${AGENTSTACK_PROJECT_KEY-unset}" "$AGENTSTACK_AGENT_ID"'
    r = subprocess.run(['/bin/bash', '-c', command], text=True, capture_output=True,
                       env={**os.environ, 'PATH': str(ROOT / '.venv/bin') + ':' + os.environ['PATH']}, timeout=30)
    assert r.returncode == 0, r.stderr
    assert r.stdout.endswith('|unset|1')
    assert not r.stdout.startswith('UnrelatedName')


@pytest.mark.parametrize('provider,entry', [('claude', 'bin/agent-start'),
                                         ('codex', 'bin/agent-start-codex'),
                                         ('gemini', 'bin/agent-start-gemini')])
def test_launcher_runs_only_stub_with_context_and_sanitized_environment(prepared, provider, entry):
    stub = prepared['root'] / 'provider-stub'
    stub.write_text('#!/bin/sh\nprintf "%s|%s|%s" "$AGENT_NAME" "${PROJECT_KEY-unset}" "$AGENTSTACK_AGENT_ID"\n')
    stub.chmod(0o700)
    r = run(prepared, entry, str(prepared['root']), bash=True,
            extra={'AGENTSTACK_' + provider.upper() + '_BIN': str(stub)})
    assert r.returncode == 0, r.stderr
    assert r.stdout.endswith('|unset|1')


def test_profile_save_inspect_reconnect_and_explicit_runtime_boundary(prepared):
    profile = client(prepared).profile_path
    r = run(prepared, 'bin/lib/runtime_client.py', 'save-profile')
    assert r.returncode == 0, r.stderr
    assert 'project_key' not in api['read_json'](profile)
    for entry in ('bin/agentstack-persistent', 'bin/agentstack-daemon'):
        r = run(prepared, entry, 'inspect', '--profile', str(profile))
        assert r.returncode == 0, r.stderr
        assert json.loads(r.stdout)['runtime_execution'] == 'requires-pr4c'
    r = run(prepared, 'bin/agentstack-persistent', 'reconnect', '--profile', str(profile))
    assert r.returncode == 0, r.stderr
    r = run(prepared, 'bin/agentstack-persistent', 'run', '--profile', str(profile))
    assert r.returncode == 2
    assert 'GLOBAL_PROXY_RUNTIME_REQUIRES_PR4C' in r.stderr


def test_authenticated_session_index_rejects_foreign_response(prepared):
    c = client(prepared)
    row = c.call('register_agent', {'program': 'codex', 'model': 'fixture'})
    payload = {'session_id': 'fixture-session', 'tool_response': row, 'cwd': '/not-a-route'}
    record = c.record_session(payload)
    assert record['binding_kind'] == 'global-self'
    assert 'project_key' not in record
    payload['tool_response'] = {**row, 'agent_id': 2}
    with pytest.raises(ClientError, match='IDENTITY_BINDING_MISMATCH'):
        c.record_session(payload)


def test_absent_context_is_legacy_without_http_or_authority(monkeypatch, tmp_path):
    monkeypatch.delenv('AGENTSTACK_CLIENT_CONFIG', raising=False)
    monkeypatch.chdir(tmp_path)
    assert api['configured']() is None
    r = subprocess.run([sys.executable, str(ROOT / 'bin/lib/runtime_client.py'), 'mode'], capture_output=True, text=True)
    assert r.returncode == 0 and r.stdout.strip() == 'legacy'


@pytest.mark.parametrize('valid', [True, False])
def test_confirmed_window_owner_mapping(prepared, valid):
    from agentstack_mail.namespace_state_io import connection
    with connection(Path(prepared['config']['database'])) as db:
        row = dict(db.execute('SELECT * FROM window_identities WHERE agent_id=1 LIMIT 1').fetchone())
    config = prepared['client_config']
    config['identity'].update(window_row_id=row['id'], window_uuid=row['window_uuid'] if valid else 'wrong-window')
    credential_path = local_root(config) / 'credential.json'
    private(credential_path, {**api['read_json'](credential_path), **config['identity']})
    private(prepared['client_path'], config)
    if valid:
        assert client(prepared).reconnect(program='fixture', model='fixture')['window_uuid'] == row['window_uuid']
    else:
        with pytest.raises(ClientError, match='WINDOW_OWNER_MISMATCH'):
            client(prepared).reconnect(program='fixture', model='fixture')


def test_stale_replayed_receipt_cannot_activate_old_token(prepared):
    c = client(prepared)
    token = 'first-recovery-token-fixture-123456'
    generation = c.identity['credential_generation']
    pending = c.credential.with_suffix('.enrollment-pending.json')
    private(pending, {'kind': api['ENROLL_PENDING_KIND'], 'action': 'recover', 'agent_id': 1, 'request_id': 'stale-request',
                     'expected_generation': generation, 'new_credential': token, **c.binding})
    c.management('recover', agent_id=1, request_id='stale-request',
                 expected_generation=generation, new_credential=token)
    c.management('recover', agent_id=1, request_id='later-request',
                 expected_generation=generation + 1, new_credential='later-recovery-token-fixture-123456')
    original = api['read_private'](c.credential)
    with pytest.raises(ClientError, match='ENROLLMENT_RECEIPT_STALE'):
        c.enroll('recover', 'stale-request', generation)
    assert api['read_private'](c.credential) == original
    assert pending.exists()


def test_null_token_claim_and_generation_zero(prepared):
    from agentstack_mail.namespace_state_io import connection
    with connection(Path(prepared['config']['database']), write=True) as db:
        db.execute('UPDATE agents SET registration_token=NULL,credential_generation=0 WHERE id=1')
    config = prepared['client_config']
    config['identity']['credential_generation'] = 0
    private(prepared['client_path'], config)
    c = client(prepared)
    receipt = c.enroll('claim', 'null-claim', 0)
    assert receipt['new_generation'] == 1
    assert client(prepared).observe()['agent_id'] == 1


def test_late_context_replacement_blocks_callback(prepared):
    c = client(prepared)
    with pytest.raises(ClientError, match='CONTEXT_CHANGED'):
        with c.fence():
            private(c.path, {**c.config, 'authority_epoch': 'changed'})
            c.reconnect()
    # The outer fence also refuses to complete with the changed context.


def test_profile_mismatch_is_readonly_before_reconnect(prepared):
    c = client(prepared)
    row = c.observe()
    profile = c.profile_path
    revision = c.call('health_check')['mutation_revision']
    private(profile, {'kind': api['PROFILE_KIND'], 'client_config': str(c.path), **row, 'agent_id': 2})
    with pytest.raises(ClientError, match='OUTPUT_OWNER_CONFLICT'):
        api['profile_operation'](profile, 'reconnect')
    assert s1.call(prepared, 'health_check')['mutation_revision'] == revision


def test_gemini_stdio_global_is_explicitly_blocked_before_legacy_proxy(prepared):
    r = run(prepared, 'bin/agentstack-gemini-mcp', bash=True)
    assert r.returncode == 1
    assert r.stderr.strip() == 'agentstack-gemini-mcp: GLOBAL_STDIO_REQUIRES_PR4C'


def test_common_shell_register_does_not_preflight_names_or_scopes(prepared):
    command = '. ' + shlex.quote(str(ROOT / 'bin/lib/agentstack-register.sh')) + '; ags_register_session /old/project codex fixture Codex "$PWD" UnrelatedName; printf "%s|%s" "$AGS_REGISTERED_AGENT_NAME" "$AGS_REGISTERED_AGENT_ID"'
    r = subprocess.run(['/bin/bash', '-c', command], text=True, capture_output=True,
                       env={**os.environ, 'PATH': str(ROOT / '.venv/bin') + ':' + os.environ['PATH']}, timeout=30)
    assert r.returncode == 0, r.stderr
    assert r.stdout.endswith('|1')
    assert not r.stdout.startswith('UnrelatedName')


def test_matching_but_stale_local_generation_cannot_mutate_server(prepared):
    config = prepared['client_config']
    config['identity']['credential_generation'] += 1
    credential = local_root(config) / 'credential.json'
    private(credential, {**api['read_json'](credential), **config['identity']})
    private(prepared['client_path'], config)
    before = s1.call(prepared, 'health_check')['mutation_revision']
    with pytest.raises(ClientError, match='IDENTITY_BINDING_MISMATCH'):
        client(prepared).reconnect()
    assert s1.call(prepared, 'health_check')['mutation_revision'] == before


def test_window_inspect_reports_local_only_and_not_ready(prepared):
    from agentstack_mail.namespace_state_io import connection
    with connection(Path(prepared['config']['database'])) as db:
        window = dict(db.execute('SELECT * FROM window_identities WHERE agent_id=1 LIMIT 1').fetchone())
    config = prepared['client_config']
    config['identity'].update(window_row_id=window['id'], window_uuid=window['window_uuid'])
    credential = local_root(config) / 'credential.json'
    private(credential, {**api['read_json'](credential), **config['identity']})
    private(prepared['client_path'], config)
    c = client(prepared)
    observed = c.observe()
    assert observed['window_verification'] == 'local-only-server-unverified'
    assert c.reconnect(program='fixture', model='fixture')['window_verification'] == 'server-verified-by-register'
    profile = client(prepared).profile_path
    private(profile, {'kind': api['PROFILE_KIND'], 'client_config': str(c.path), **observed})
    inspected = api['profile_operation'](profile, 'inspect')
    assert inspected['window_verification'] == 'local-only-server-unverified'
    assert inspected['ready'] is False


def test_session_hook_records_only_authenticated_self(prepared):
    row = client(prepared).call('register_agent', {'program': 'codex', 'model': 'fixture'})
    data = json.dumps({'session_id': 'entrypoint-session', 'tool_response': row,
                       'tool_input': {'project_key': '/obsolete/project'}, 'cwd': '/work-metadata'})
    r = subprocess.run([sys.executable, str(ROOT / 'hooks/record-session-index.py')],
                       input=data, text=True, capture_output=True, env=os.environ, timeout=30)
    assert r.returncode == 0, r.stderr
    record = api['read_json'](local_root(prepared['client_config']) / 'runtime/session_index/self.json')
    assert record['binding_kind'] == 'global-self'
    assert record['agent_id'] == 1
    assert 'project_key' not in record


def test_tool_errors_never_echo_arbitrary_uppercase_secret():
    with pytest.raises(ClientError, match='^TOOL_REJECTED$'):
        api['result_value']({'result': {'isError': True, 'content': [
            {'type': 'text', 'text': 'Error: UPPERCASE_SECRET_SENTINEL'}]}})


def test_installed_payload_reconnects_from_its_own_root(prepared):
    import shutil
    installed = prepared['root'] / 'installed'
    (installed / 'bin/lib').mkdir(parents=True)
    (installed / 'hooks').mkdir()
    for path in ('bin/lib/runtime_client.py', 'bin/agentstack-runtime-client',
                 'bin/agentstack-reregister', 'bin/agentstack-codex-bootstrap'):
        shutil.copy2(ROOT / path, installed / path)
    # Mirror the installer payload, including the shared catalog validator.
    shutil.copy2(ROOT / 'packages/agentstack_mail/src/agentstack_mail/schema_contract.py',
                 installed / 'bin/lib/schema_contract.py')
    shutil.copy2(ROOT / 'packages/agentstack_mail/fixtures/global-server-s2a.json',
                 installed / 'bin/lib/global-server-s2a.json')
    config = {**prepared['client_config'], 'wrapper_root': str(installed)}
    private(prepared['client_path'], config)
    r = subprocess.run(['/bin/bash', str(installed / 'bin/agentstack-reregister'), 'ObsoleteName', 'codex', 'fixture'],
                       env={**os.environ, 'PATH': str(ROOT / '.venv/bin') + ':' + os.environ['PATH']},
                       text=True, capture_output=True, timeout=30)
    assert r.returncode == 0, r.stderr
    assert 'registered ' in r.stdout
    # The old checkout must not resume a context bound to another wrapper root.
    with pytest.raises(ClientError, match='WRAPPER_ROOT_MISMATCH'):
        client(prepared)


def test_actual_dedicated_tmux_server_stale_environment_does_not_route(prepared):
    import shutil
    import tempfile
    import time
    tmux = shutil.which('tmux')
    if tmux is None:
        pytest.skip('tmux unavailable; external runner must cover dedicated socket')
    socket_name = 'rh4a-' + str(os.getpid())
    tmux_root = tempfile.mkdtemp(prefix='rh4a-tm-')
    output = prepared['root'] / 'tmux-observation.json'
    env = {**os.environ, 'TMUX_TMPDIR': tmux_root}
    env.pop('TMUX', None)
    env.pop('TMUX_PANE', None)
    command = ' '.join(shlex.quote(x) for x in [sys.executable, str(ROOT / 'bin/lib/runtime_client.py'), 'observe'])
    command += ' > ' + shlex.quote(str(output.with_suffix('.pending'))) + ' && mv ' + shlex.quote(str(output.with_suffix('.pending'))) + ' ' + shlex.quote(str(output)) + '; sleep 10'
    try:
        result = subprocess.run([tmux, '-L', socket_name, 'new-session', '-d', '-s', 'fixture', command],
                                env=env, capture_output=True, text=True, timeout=10)
        assert result.returncode == 0, result.stderr
        # These are server-global values inherited by a newly created pane.
        for key, value in [('AGENT_NAME', 'WrongTmuxName'), ('AGENTSTACK_PROJECT_KEY', '/stale/tmux/scope'),
                           ('AGENTSTACK_MCP_URL', 'http://127.0.0.1:1/mcp')]:
            subprocess.run([tmux, '-L', socket_name, 'set-environment', '-g', key, value], env=env, check=True)
        second = prepared['root'] / 'tmux-second.json'
        second_command = ' '.join(shlex.quote(x) for x in [sys.executable, str(ROOT / 'bin/lib/runtime_client.py'), 'observe'])
        second_command += ' > ' + shlex.quote(str(second.with_suffix('.pending'))) + ' && mv ' + shlex.quote(str(second.with_suffix('.pending'))) + ' ' + shlex.quote(str(second)) + '; sleep 10'
        subprocess.run([tmux, '-L', socket_name, 'new-window', '-t', 'fixture', second_command], env=env, check=True)
        deadline = time.monotonic() + 8
        while not second.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert second.exists()
        row = json.loads(second.read_text())
        assert row['agent_id'] == 1
        assert row['name'] != 'WrongTmuxName'
    finally:
        subprocess.run([tmux, '-L', socket_name, 'kill-server'], env=env, capture_output=True, timeout=10)
        shutil.rmtree(tmux_root)


def test_unknown_registration_outcome_keeps_pending_and_refuses_retry(prepared, monkeypatch):
    config = prepared['client_config']
    config['identity'] = None
    fresh_context(prepared, config, 'uncertain-client')
    c = client(prepared)
    rpc = c.rpc
    def uncertain(method, params):
        if method == 'tools/call' and params.get('name') == 'register_agent':
            raise ClientError('TRANSPORT_FAILED')
        return rpc(method, params)
    monkeypatch.setattr(c, 'rpc', uncertain)
    with pytest.raises(ClientError, match='TRANSPORT_FAILED'):
        c.create('UncertainOwner', 'codex', 'fixture')
    journal = c.credential.with_suffix('.registration-pending.json')
    original = api['read_private'](journal)
    assert 'registration_token' in api['read_json'](journal)
    with pytest.raises(ClientError, match='REGISTRATION_PENDING_OPERATOR_REQUIRED'):
        c.create('AnotherOwner', 'codex', 'fixture')
    assert api['read_private'](journal) == original


def test_new_registration_cannot_overwrite_existing_credential(prepared):
    config = prepared['client_config']
    config['identity'] = None
    private(prepared['client_path'], config)
    credential = local_root(config) / 'credential.json'
    original = api['read_private'](credential)
    with pytest.raises(ClientError, match='RUNTIME_DIR_BELONGS_TO_OTHER_IDENTITY'):
        client(prepared).create('AnotherOwner', 'codex', 'fixture')
    assert api['read_private'](credential) == original


def test_provider_launch_requires_isolated_configuration_home(prepared, tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    revision = s1.call(prepared, 'health_check')['mutation_revision']
    with pytest.raises(ClientError, match='ISOLATED_PROVIDER_HOME_REQUIRED'):
        client(prepared).launch('codex', prepared['root'])
    assert s1.call(prepared, 'health_check')['mutation_revision'] == revision


def test_global_name_picker_refuses_before_legacy_bookkeeping(prepared):
    marker = prepared['root'] / 'must-not-pick'
    command = '. ' + shlex.quote(str(ROOT / 'bin/lib/agentstack-register.sh')) + '; ags_pick_adjective_scientist_name() { touch ' + shlex.quote(str(marker)) + '; }; ags_pick_available_agent_name /old/project Codex'
    r = subprocess.run(['/bin/bash', '-c', command], text=True, capture_output=True,
                       env={**os.environ, 'PATH': str(ROOT / '.venv/bin') + ':' + os.environ['PATH']}, timeout=30)
    assert r.returncode == 1
    assert r.stderr.strip() == 'agentstack: GLOBAL_NAME_REQUIRES_ATOMIC_REGISTER'
    assert not marker.exists()


def test_global_owned_retire_and_unretire_preserve_id_and_token(prepared):
    c = client(prepared)
    assert c.call('retire_agent')['status'] == 'retired'
    command = '. ' + shlex.quote(str(ROOT / 'bin/lib/agentstack-register.sh')) + '; ags_unretire_owned_identity /old/project WrongName'
    r = subprocess.run(['/bin/bash', '-c', command], text=True, capture_output=True,
                       env={**os.environ, 'PATH': str(ROOT / '.venv/bin') + ':' + os.environ['PATH']}, timeout=30)
    assert r.returncode == 0, r.stderr
    assert client(prepared).observe()['agent_id'] == 1
    assert client(prepared).local_owner() == prepared['token']


def test_explicit_active_legacy_context_never_falls_back_to_ambient_curl(prepared):
    config = {**prepared['client_config'], 'mode': 'legacy'}
    private(prepared['client_path'], config)
    r = run(prepared, 'bin/agentstack-reregister', 'WrongName', bash=True)
    assert r.returncode != 0
    assert r.stderr.strip() == 'runtime-client: LEGACY_CONTEXT_REQUIRES_PR7'
    with pytest.raises(ClientError, match='LEGACY_CONTEXT_REQUIRES_PR7'):
        client(prepared).reconnect()


def test_actual_global_await_returns_oldest_unseen_and_preserves_read_ack(prepared):
    from agentstack_mail.namespace_state_io import connection
    database = Path(prepared['config']['database'])
    with connection(database) as db:
        owner = dict(db.execute('SELECT * FROM agents WHERE id=2').fetchone())
        before = list(db.execute('SELECT message_id,read_ts,ack_ts FROM message_recipients WHERE agent_id=2'))
    config = prepared['client_config']
    config['identity'] = {'agent_id': 2, 'credential_generation': owner['credential_generation'], 'name': owner['name']}
    private(local_root(config) / 'credential.json', {'kind': 'orrery-global-credential-v1',
        **prepared['binding'], **config['identity'], 'registration_token': owner['registration_token']})
    private(prepared['client_path'], config)
    r = run(prepared, 'bin/agentstack-await-reply', '--agent-name', 'StaleDisplayName',
            '--project-key', '/stale/tmux/scope', '--after-id', '103', '--timeout', '1', '--interval', '0.1')
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)['id'] == 104
    with connection(database) as db:
        after = list(db.execute('SELECT message_id,read_ts,ack_ts FROM message_recipients WHERE agent_id=2'))
    assert [tuple(row) for row in after] == [tuple(row) for row in before]


def test_global_context_cannot_fall_back_to_legacy_profile(prepared):
    profile = prepared['root'] / 'legacy-profile.json'
    private(profile, {'kind': 'orrery-persistent-agent-v1'})
    for entry in ['bin/agentstack-persistent', 'bin/agentstack-daemon']:
        r = run(prepared, entry, 'inspect', '--profile', str(profile))
        assert r.returncode == 2
        assert 'LEGACY_PROFILE_CONTEXT_CONFLICT' in r.stderr


def test_operator_daemon_creation_uses_shared_global_profile_without_project(prepared):
    config = prepared['client_config']
    config['identity'] = None
    fresh_context(prepared, config, 'daemon-client')
    profile = client(prepared).profile_path
    r = run(prepared, 'bin/agentstack-daemon', 'create', '--connection', str(prepared['client_path']),
            '--name', 'PreparedDaemon', '--operator')
    assert r.returncode == 0, r.stderr
    row = json.loads(r.stdout)
    assert row['ready'] is False
    assert row['runtime_execution'] == 'requires-pr4c'
    saved = api['read_json'](profile)
    assert saved['kind'] == api['PROFILE_KIND'] and 'project_key' not in saved
    r = run(prepared, 'bin/agentstack-daemon', 'finalize-create', '--connection', str(prepared['client_path']),
            '--agent-id', str(row['agent_id']), '--operator')
    assert r.returncode == 0, r.stderr
    assert client(prepared).observe()['agent_id'] == row['agent_id']
    r = run(prepared, 'bin/agentstack-persistent', 'reconnect', '--profile', str(profile))
    assert r.returncode == 0, r.stderr
    from agentstack_mail.namespace_state_io import connection
    with connection(Path(prepared['config']['database'])) as db:
        metadata = tuple(db.execute('SELECT program, model FROM agents WHERE id=?', (row['agent_id'],)).fetchone())
    assert metadata == ('daemon', 'deterministic')


def test_uncertain_registration_finalizes_same_id_without_register_retry(prepared, monkeypatch):
    config = prepared['client_config']
    config['identity'] = None
    fresh_context(prepared, config, 'finalize-client')
    c = client(prepared)
    rpc = c.rpc
    registered = {}
    def uncertain(method, params):
        reply = rpc(method, params)
        if method == 'tools/call' and params.get('name') == 'register_agent':
            registered.update(api['result_value'](reply))
            raise ClientError('TRANSPORT_FAILED')
        return reply
    monkeypatch.setattr(c, 'rpc', uncertain)
    with pytest.raises(ClientError, match='TRANSPORT_FAILED'):
        c.create('FinalizedOwner', 'daemon', 'deterministic')
    revision = s1.call(prepared, 'health_check')['mutation_revision']
    row = c.finalize_registration(registered['agent_id'])
    assert row['agent_id'] == registered['agent_id']
    assert s1.call(prepared, 'health_check')['mutation_revision'] == revision
    assert c.local_owner() == registered['registration_token']


def metadata_row(state):
    from agentstack_mail.namespace_state_io import connection
    with connection(Path(state['config']['database'])) as db:
        return tuple(db.execute('SELECT program, model FROM agents WHERE id=1').fetchone())


@pytest.mark.parametrize('source', ['position', 'environment'])
def test_reregister_respects_explicit_metadata(prepared, source):
    if source == 'position':
        result = run(prepared, 'bin/agentstack-reregister', 'obsolete-name', 'claude-code', 'explicit-model', bash=True)
    else:
        result = run(prepared, 'bin/agentstack-reregister', 'obsolete-name', bash=True,
                     extra={'AGENTSTACK_REREGISTER_PROGRAM': 'claude-code',
                            'AGENTSTACK_REREGISTER_MODEL': 'explicit-model'})
    assert result.returncode == 0, result.stderr
    assert metadata_row(prepared) == ('claude-code', 'explicit-model')
    assert client(prepared).registration_metadata()['program'] == 'claude-code'
    assert client(prepared).registration_metadata()['model'] == 'explicit-model'


def test_unspecified_reconnect_observes_without_overwriting_unknown_metadata(prepared, monkeypatch):
    c = client(prepared)
    before = metadata_row(prepared)
    original = c.call
    def forbid_register(tool, *args, **kwargs):
        assert tool != 'register_agent'
        return original(tool, *args, **kwargs)
    monkeypatch.setattr(c, 'call', forbid_register)
    row = c.reconnect()
    assert row['reconnect_mode'] == 'observe-only'
    assert row['registration_verified'] is False
    assert metadata_row(prepared) == before


def test_saved_metadata_reconnect_resends_without_relabeling(prepared, monkeypatch):
    c = client(prepared)
    c.reconnect(program='claude-code', model='saved-model')
    c = client(prepared)
    original = c.call
    sent = []
    def capture(tool, args=None):
        if tool == 'register_agent':
            sent.append(args)
        return original(tool, args)
    monkeypatch.setattr(c, 'call', capture)
    assert c.reconnect()['reconnect_mode'] == 'register'
    assert sent == [{'program': 'claude-code', 'model': 'saved-model'}]
    assert metadata_row(prepared) == ('claude-code', 'saved-model')


@pytest.mark.parametrize('provider,program', [('claude', 'claude-code'), ('codex', 'codex'), ('gemini', 'antigravity')])
def test_exports_respects_provider_model_and_quotes_environment_names(prepared, monkeypatch, provider, program):
    marker = prepared['root'] / 'shell-injection-marker'
    malicious = 'AGENTSTACK_LOOKUP_X;touch ' + str(marker) + ';:'
    monkeypatch.setenv(malicious, 'unused')
    monkeypatch.setenv('AGENTSTACK_' + provider.upper() + '_MODEL', 'provider-specific-model')
    exports = client(prepared).exports(provider)
    result = subprocess.run(['/bin/bash', '-c', exports + '; printf shell-safe'],
                            text=True, capture_output=True, env=os.environ, timeout=10)
    assert result.returncode == 0
    assert result.stdout == 'shell-safe'
    assert not marker.exists()
    assert metadata_row(prepared) == (program, 'provider-specific-model')


def test_daemon_profile_reconnect_preserves_registration_metadata(prepared):
    c = client(prepared)
    c.reconnect(program='daemon', model='deterministic')
    profile = client(prepared).profile_path
    c.save_profile()
    result = run(prepared, 'bin/agentstack-persistent', 'reconnect', '--profile', str(profile))
    assert result.returncode == 0, result.stderr
    assert metadata_row(prepared) == ('daemon', 'deterministic')


def test_global_await_retries_transport_until_timeout(prepared):
    import time
    config = prepared['client_config']
    config['mcp_url'] = 'http://127.0.0.1:1/mcp'
    private(prepared['client_path'], config)
    start = time.monotonic()
    result = run(prepared, 'bin/agentstack-await-reply', '--agent-name', 'obsolete',
                 '--timeout', '0.4', '--interval', '0.1')
    assert result.returncode == 1, result.stderr
    assert time.monotonic() - start >= 0.4
    assert 'mail server unreachable for the whole wait' in result.stderr


def test_global_await_outage_does_not_retry_fenced_authority(prepared):
    config = prepared['client_config']
    config['mcp_url'] = 'http://127.0.0.1:1/mcp'
    private(prepared['client_path'], config)
    authority = Path(config['authority'])
    private(authority, {**api['read_json'](authority), 'phase': 'quiescing'})
    result = run(prepared, 'bin/agentstack-await-reply', '--agent-name', 'obsolete',
                 '--timeout', '10', '--interval', '1')
    assert result.returncode == 2
    assert 'WRITER_FENCED' in result.stderr


@pytest.mark.parametrize('role', ['authority', 'authority_lock', 'management_socket', 'client_context'])
def test_credential_role_overlap_rejected_before_server_recovery(prepared, role):
    config = prepared['client_config']
    target = prepared['client_path'] if role == 'client_context' else Path(config[role])
    config['credential_file'] = str(target)
    authority = Path(config['authority'])
    authority_before = authority.read_bytes()
    with pytest.raises(ClientError, match='FIXED_LAYOUT_CONTEXT_REQUIRED'):
        private(prepared['client_path'], config)
        client(prepared).enroll('recover', 'overlap-recovery', config['identity']['credential_generation'])
    assert authority.read_bytes() == authority_before
    inspected = s1.control(prepared, action='inspect', agent_id=1)
    assert inspected['credential_generation'] == config['identity']['credential_generation']


def test_profile_cannot_overwrite_authority_or_credentials(prepared):
    c = client(prepared)
    for path in [c.authority, c.path, c.credential, c.lock]:
        before = path.read_bytes()
        with pytest.raises(ClientError, match='FIXED_LAYOUT_PATH_NOT_SUPPORTED'):
            c.save_profile(path)
        assert path.read_bytes() == before


@pytest.mark.parametrize('link_kind', ['symlink', 'hardlink'])
def test_late_credential_role_alias_rejected_before_cas(prepared, link_kind):
    c = client(prepared)
    before = c.management('inspect', agent_id=1)['credential_generation']
    authority_before = c.authority.read_bytes()
    c.credential.unlink()
    if link_kind == 'symlink':
        c.credential.symlink_to(c.authority)
    else:
        os.link(c.authority, c.credential)
    try:
        with pytest.raises(ClientError, match='CONTEXT_PATH_ROLE_CONFLICT'):
            c.enroll('recover', 'late-alias-recovery', before)
        assert c.authority.read_bytes() == authority_before
    finally:
        c.credential.unlink()
    assert s1.control(prepared, action='inspect', agent_id=1)['credential_generation'] == before


def test_mail_state_cannot_be_client_output_before_recovery(prepared, monkeypatch):
    config = prepared['client_config']
    database = Path(prepared['config']['database'])
    database.parent.chmod(0o700)
    original_bytes = database.read_bytes()  # Synthetic fixture only.
    before = client(prepared).management('inspect', agent_id=1)['credential_generation']
    config['credential_file'] = str(database)
    private(prepared['client_path'], config)
    with pytest.raises(ClientError, match='FIXED_LAYOUT_CONTEXT_REQUIRED'):
        client(prepared).enroll('recover', 'mail-output-recovery', before)
    assert database.read_bytes() == original_bytes
    assert s1.control(prepared, action='inspect', agent_id=1)['credential_generation'] == before


def test_every_client_output_rejects_foreign_format_before_network(prepared, monkeypatch):
    import sqlite3
    c = client(prepared)
    before = c.call('health_check')['mutation_revision']
    sqlite_file = prepared['root'] / 'foreign-sqlite.db'
    with sqlite3.connect(sqlite_file) as db:
        db.execute('CREATE TABLE foreign_data(value TEXT)')
    payloads = [b'user-owned text\n', sqlite_file.read_bytes()]
    directory = c.runtime_dir / 'session_index'
    directory.mkdir(mode=0o700)
    paths = {'context': c.path, 'credential': c.credential,
             'registration': c.credential.with_suffix('.registration-pending.json'),
             'enrollment': c.credential.with_suffix('.enrollment-pending.json'),
             'metadata': c.metadata_path, 'profile': client(prepared).profile_path,
             'session': directory / 'self.json'}
    def no_network(*args, **kwargs):
        pytest.fail('network called before rejecting a foreign output format')
    monkeypatch.setattr(c, 'rpc', no_network)
    for role, path in paths.items():
        original = path.read_bytes() if path.exists() else None
        try:
            for payload in payloads:
                path.write_bytes(payload)
                path.chmod(0o600)
                with pytest.raises(ClientError, match='CONTEXT_CHANGED' if role == 'context' else 'OUTPUT_SCHEMA_INVALID'):
                    if role == 'profile':
                        c.save_profile()
                    else:
                        c.reconnect(program='fixture', model='fixture')
                assert path.read_bytes() == payload
        finally:
            if original is None:
                path.unlink()
            else:
                path.write_bytes(original)
    assert client(prepared).call('health_check')['mutation_revision'] == before


def test_client_output_in_mail_root_or_alias_is_rejected(prepared):
    c = client(prepared)
    database = Path(prepared['config']['database'])
    before = database.read_bytes()  # Synthetic fixture only.
    for path in [database, c.runtime / 'new-profile.json']:
        with pytest.raises(ClientError, match='FIXED_LAYOUT_PATH_NOT_SUPPORTED'):
            c.save_profile(path)
    alias = prepared['root'] / 'mail-alias'
    alias.symlink_to(c.runtime, target_is_directory=True)
    with pytest.raises(ClientError, match='FIXED_LAYOUT_PATH_NOT_SUPPORTED'):
        c.save_profile(alias / 'new-profile.json')
    outside = prepared['root'] / 'outside-hardlink.db'
    os.link(database, outside)
    try:
        with pytest.raises(ClientError, match='FIXED_LAYOUT_PATH_NOT_SUPPORTED'):
            c.save_profile(outside)
        assert database.read_bytes() == before
    finally:
        outside.unlink()


def test_reregister_observe_only_has_distinct_output_and_status(prepared):
    before = metadata_row(prepared)
    result = run(prepared, 'bin/agentstack-reregister', 'obsolete-name', bash=True)
    assert result.returncode == 3
    assert 'observed' in result.stdout and 'registration not refreshed; pass program/model' in result.stdout
    assert 'registered' not in result.stdout
    assert metadata_row(prepared) == before


def test_waiting_client_survives_another_process_metadata_update(prepared, monkeypatch):
    module = runpy.run_path(str(ROOT / 'bin/agentstack-await-reply'))
    context_before = prepared['client_path'].read_bytes()
    def waiting(fetch, **kwargs):
        assert isinstance(fetch(), list)
        result = run(prepared, 'bin/agentstack-reregister', 'obsolete-name', 'claude-code', 'changed-model', bash=True)
        assert result.returncode == 0, result.stderr
        assert isinstance(fetch(), list)
        return 124, None
    monkeypatch.setitem(module['main'].__globals__, 'wait_for_reply', waiting)
    assert module['main'](['--agent-name', 'obsolete-name', '--timeout', '5']) == 124
    assert prepared['client_path'].read_bytes() == context_before
    assert metadata_row(prepared) == ('claude-code', 'changed-model')
    assert client(prepared).registration_metadata()['model'] == 'changed-model'



def test_foreign_file_appearing_before_rename_is_not_overwritten(prepared, monkeypatch):
    c = client(prepared)
    destination = c.profile_path
    original = api['atomic_json'].__globals__['os'].fsync
    def create_foreign_file(fd):
        original(fd)
        destination.write_text('user-owned concurrent file')
        destination.chmod(0o600)
    monkeypatch.setattr(api['atomic_json'].__globals__['os'], 'fsync', create_foreign_file)
    with pytest.raises(ClientError, match='OUTPUT_SCHEMA_INVALID'):
        c.write_output(destination, {'kind': api['PROFILE_KIND'], **c.binding,
            'agent_id': 1, 'client_config': str(c.path)}, 'profile')
    assert destination.read_text() == 'user-owned concurrent file'


@pytest.mark.parametrize('role', ['credential', 'profile', 'session'])
def test_released_files_require_explicit_import_before_network(prepared, monkeypatch, role):
    c = client(prepared)
    before = c.call('health_check')['mutation_revision']
    files = []
    if role == 'credential':
        path = c.credential
        path.write_text('released-owner-token-00000001' + '\n')
        sidecar = path.with_name(path.name + '.identity.json')
        private(sidecar, {'kind': 'orrery-enrollment-active-v1',
                          'server_instance_id': prepared['binding']['expected_server_instance_id'],
                          'project_key': '/legacy/scope', 'agent_id': 1, 'agent_name': 'OldAlpha',
                          'request_id': 'released-request', 'credential_generation': c.identity['credential_generation'],
                          'credential_fingerprint': 'preserved-fingerprint'})
        files.append(sidecar)
    elif role == 'profile':
        path = c.profile_path
        private(path, {'kind': 'orrery-persistent-agent-v1', 'name': 'OldAlpha',
                       'agent_id': 1, 'project_key': '/legacy/scope', 'provider': 'codex',
                       'parentless': True, 'lifecycle': 'persistent', 'interaction': 'headless',
                       'connection': 'connection.json', 'state_dir': 'state',
                       'working_directory': '.', 'command': ['fixture-provider'], 'environment': {}})
    else:
        directory = c.runtime_dir / 'session_index'
        directory.mkdir(mode=0o700)
        path = directory / 'self.json'
        private(path, {'schema_version': 2, 'binding_kind': 'self', 'agent_id': 1,
                       'agent_name': 'OldAlpha', 'session_id': 'released-session',
                       'project_key': '/legacy/scope', 'registered_by': 'OldAlpha',
                       'transcript_path': 'fixture.jsonl', 'cwd': '.', 'ts': '2026-10-01T00:00:00Z'})
    files.append(path)
    original = {p: p.read_bytes() for p in files}
    def no_network(*args, **kwargs):
        pytest.fail('network called before rejecting a released output format')
    monkeypatch.setattr(c, 'rpc', no_network)
    with pytest.raises(ClientError, match='LEGACY_FILE_REQUIRES_IMPORT'):
        if role == 'profile':
            c.save_profile()
        else:
            c.reconnect(program='fixture', model='fixture')
    assert {p: p.read_bytes() for p in files} == original
    for p in files:
        p.unlink()
    if role == 'credential':
        private(path, {'kind': 'orrery-global-credential-v1', **c.binding,
                       **c.identity, 'registration_token': prepared['token']})
    assert client(prepared).call('health_check')['mutation_revision'] == before


@pytest.mark.parametrize('role', ['metadata', 'session'])
def test_new_identity_requires_a_fresh_runtime_directory(prepared, monkeypatch, role):
    c = client(prepared)
    if role == 'metadata':
        c.reconnect(program='fixture', model='fixture')
        saved = c.metadata_path
    else:
        c.record_session({'session_id': 'fresh-directory-session', 'cwd': '.',
                          'tool_response': c.call('register_agent', {'program': 'fixture', 'model': 'fixture'})})
        saved = c.runtime_dir / 'session_index/self.json'
    original = saved.read_bytes()
    before = c.call('health_check')['mutation_revision']
    original_credential = c.credential.read_bytes()
    c.credential.unlink()
    config = {**prepared['client_config'], 'identity': None}
    private(prepared['client_path'], config)
    monkeypatch.setattr(RuntimeClient, 'rpc', lambda *a, **k: pytest.fail('network before fresh-runtime refusal'))
    with pytest.raises(ClientError, match='RUNTIME_DIR_BELONGS_TO_OTHER_IDENTITY'):
        client(prepared)
    assert saved.read_bytes() == original
    private(prepared['client_path'], prepared['client_config'])
    c.credential.write_bytes(original_credential)
    c.credential.chmod(0o600)
    monkeypatch.undo()
    assert client(prepared).call('health_check')['mutation_revision'] == before

@pytest.mark.parametrize('target', ['authority', 'mail_db', 'session_index'])
def test_old_arbitrary_output_context_is_rejected_before_network(prepared, monkeypatch, target):
    c = client(prepared)
    before = c.call('health_check')['mutation_revision']
    paths = {'authority': c.authority, 'mail_db': Path(prepared['config']['database']),
             'session_index': c.runtime_dir / 'session_index/new-credential.json'}
    destination = paths[target]
    original = destination.read_bytes() if destination.exists() else None
    config = {**prepared['client_config'], 'credential_file': str(destination)}
    private(c.path, config)
    monkeypatch.setattr(RuntimeClient, 'rpc', lambda *a, **k: pytest.fail('network before layout refusal'))
    with pytest.raises(ClientError, match='FIXED_LAYOUT_CONTEXT_REQUIRED'):
        client(prepared)
    assert (destination.read_bytes() if destination.exists() else None) == original
    private(c.path, prepared['client_config'])
    assert s1.call(prepared, 'health_check')['mutation_revision'] == before


@pytest.mark.parametrize('target', ['authority', 'mail_db', 'session_index'])
def test_daemon_arbitrary_profile_slot_is_rejected_before_registration(prepared, target):
    config = prepared['client_config']
    config['identity'] = None
    fresh_context(prepared, config, 'daemon-path-refusal')
    c = client(prepared)
    before = s1.call(prepared, 'health_check')['mutation_revision']
    paths = {'authority': c.authority, 'mail_db': Path(prepared['config']['database']),
             'session_index': c.runtime_dir / 'session_index/new-profile.json'}
    path = paths[target]
    original = path.read_bytes() if path.exists() else None
    result = run(prepared, 'bin/agentstack-daemon', 'create', '--connection', str(c.path),
                 '--name', 'RefusedProfileOwner', '--journal', str(path), '--operator')
    assert result.returncode == 2
    assert 'FIXED_LAYOUT_PATH_NOT_SUPPORTED' in result.stderr
    assert (path.read_bytes() if path.exists() else None) == original
    assert not c.credential.exists() and not c.profile_path.exists()
    assert client(prepared).identity is None
    assert s1.call(prepared, 'health_check')['mutation_revision'] == before


@pytest.mark.parametrize('overlap', ['same_mail_root', 'contains_mail_root', 'inside_mail_root'])
def test_client_root_and_mail_root_must_be_disjoint(prepared, monkeypatch, overlap):
    c = client(prepared)
    before = s1.call(prepared, 'health_check')['mutation_revision']
    target = c.clients_parent if overlap == 'same_mail_root' else c.client_home if overlap == 'contains_mail_root' else c.client_root / 'mail'
    config = {**prepared['client_config'], 'runtime_root': str(target)}
    private(c.path, config)
    monkeypatch.setattr(RuntimeClient, 'rpc', lambda *a, **k: pytest.fail('network before parent refusal'))
    with pytest.raises(ClientError, match='CLIENT_ROOT_OVERLAP'):
        client(prepared)
    assert s1.call(prepared, 'health_check')['mutation_revision'] == before


@pytest.mark.parametrize('role', ['authority', 'authority_lock', 'management_socket'])
def test_client_root_cannot_own_external_fence_inputs(prepared, monkeypatch, role):
    c = client(prepared)
    before = c.call('health_check')['mutation_revision']
    config = {**prepared['client_config'], role: str(c.client_root / 'external-input')}
    private(c.path, config)
    monkeypatch.setattr(RuntimeClient, 'rpc', lambda *a, **k: pytest.fail('network before root refusal'))
    with pytest.raises(ClientError, match='CLIENT_ROOT_OVERLAP'):
        client(prepared)
    private(c.path, prepared['client_config'])
    assert s1.call(prepared, 'health_check')['mutation_revision'] == before


def test_bad_fixed_profile_preflight_cannot_commit_a_new_identity(prepared):
    config = prepared['client_config']
    config['identity'] = None
    fresh_context(prepared, config, 'invalid-profile-client')
    path = local_root(config) / 'profile.json'
    path.write_text('user-owned notes\n')
    path.chmod(0o600)
    before = s1.call(prepared, 'health_check')['mutation_revision']
    result = run(prepared, 'bin/agentstack-daemon', 'create', '--connection', str(prepared['client_path']),
                 '--name', 'NotCommittedOwner', '--operator')
    assert result.returncode == 2 and 'OUTPUT_SCHEMA_INVALID' in result.stderr
    assert path.read_text() == 'user-owned notes\n'
    assert not (path.parent / 'credential.json').exists()
    assert s1.call(prepared, 'health_check')['mutation_revision'] == before


def test_fixed_writer_rejects_unknown_role_and_another_role_slot(prepared):
    c = client(prepared)
    before = c.call('health_check')['mutation_revision']
    for path, role in [(c.runtime_dir / 'session_index/self.json', 'profile'),
                       (c.profile_path, 'unknown')]:
        with pytest.raises(ClientError, match='FIXED_LAYOUT_PATH_NOT_SUPPORTED'):
            c.write_output(path, {}, role)
        assert not path.exists()
    assert c.call('health_check')['mutation_revision'] == before


@pytest.mark.parametrize('sidecar', [True, False])
def test_released_raw_token_in_fixed_slot_requires_import(prepared, sidecar):
    c = client(prepared)
    before = s1.call(prepared, 'health_check')['mutation_revision']
    raw = 'released-owner-token-00000001\n'
    c.credential.write_text(raw)
    identity = c.credential.with_name(c.credential.name + '.identity.json')
    if sidecar:
        private(identity, {'kind': 'orrery-enrollment-active-v1'})
    with pytest.raises(ClientError, match='LEGACY_FILE_REQUIRES_IMPORT'):
        client(prepared)
    assert c.credential.read_text() == raw
    assert s1.call(prepared, 'health_check')['mutation_revision'] == before


def test_documented_codex_recovery_refreshes_without_saved_metadata(prepared):
    template = (ROOT / 'codex/AGENTS.md').read_text()
    command = next(line for line in template.splitlines()
                   if 'bin/agentstack-reregister "$AGENT_NAME"' in line)
    assert command.endswith('"$AGENT_NAME" "${AGENTSTACK_REREGISTER_PROGRAM:-codex}"')
    result = run(prepared, 'bin/agentstack-reregister', 'OldAlpha', 'codex', bash=True,
                 extra={'AGENTSTACK_CODEX_MODEL': 'documented-model'})
    assert result.returncode == 0 and 'registered' in result.stdout
    from agentstack_mail.namespace_state_io import connection
    with connection(Path(prepared['config']['database'])) as db:
        row = db.execute('SELECT program,model FROM agents WHERE id=1').fetchone()
    assert tuple(row) == ('codex', 'documented-model')


@pytest.mark.parametrize('role', ['runtime_root', 'authority'])
def test_filesystem_case_alias_cannot_hide_root_overlap(prepared, monkeypatch, role):
    c = client(prepared)
    alias = c.client_root.with_name(c.client_root.name.upper())
    if not alias.exists() or not alias.samefile(c.client_root):
        pytest.skip('filesystem distinguishes these directory names')
    before = s1.call(prepared, 'health_check')['mutation_revision']
    config = {**prepared['client_config'], role: str(alias if role == 'runtime_root' else alias / 'authority.json')}
    private(c.path, config)
    monkeypatch.setattr(RuntimeClient, 'rpc', lambda *a, **k: pytest.fail('network before inode root refusal'))
    with pytest.raises(ClientError, match='CLIENT_ROOT_OVERLAP'):
        client(prepared)
    assert s1.call(prepared, 'health_check')['mutation_revision'] == before



@pytest.mark.parametrize('name', ['../other', 'a/b', '.', 'UPPER', '', 'é'])
def test_client_name_cannot_select_a_path(prepared, name):
    c = client(prepared)
    original = c.credential.read_bytes()
    private(c.path, {**prepared['client_config'], 'client_name': name})
    before = s1.call(prepared, 'health_check')['mutation_revision']
    with pytest.raises(ClientError, match='CLIENT_NAME_INVALID'):
        client(prepared)
    assert c.credential.read_bytes() == original
    assert s1.call(prepared, 'health_check')['mutation_revision'] == before


def test_arbitrary_client_root_and_home_cannot_create_nested_clients(prepared):
    c = client(prepared)
    for changes, reason in [({'client_root': str(c.client_root / 'nested')}, 'FIXED_LAYOUT_CONTEXT_REQUIRED'),
                            ({'agentstack_home': str(c.client_root)}, 'CLIENT_HOME_MISMATCH')]:
        private(c.path, {**prepared['client_config'], **changes})
        with pytest.raises(ClientError, match=reason):
            client(prepared)
    assert not (c.client_root / 'nested').exists()


@pytest.mark.parametrize('directory', ['parent', 'root'])
def test_client_fixed_parent_or_root_cannot_be_a_symlink(prepared, directory):
    c = client(prepared)
    path = c.clients_parent if directory == 'parent' else c.client_root
    original = path.with_name(path.name + '-original')
    path.rename(original)
    path.symlink_to(original, target_is_directory=True)
    with pytest.raises(ClientError, match='PRIVATE_DIRECTORY_REQUIRED'):
        client(prepared)


@pytest.mark.parametrize('population', ['provider_files', 'directories', 'session_history'])
def test_regular_growth_never_blocks_register_profile_session_or_fresh_client(prepared, population):
    config = prepared['client_config']
    config['identity'] = None
    fresh_context(prepared, config, 'growth-client')
    c = client(prepared)
    if population == 'provider_files':
        directory = c.client_root / 'provider-home/.codex/sessions'
        directory.mkdir(mode=0o700, parents=True)
        for index in range(1100):
            (directory / (str(index) + '.jsonl')).write_text('provider history')
    elif population == 'directories':
        for index in range(130):
            (c.client_root / ('extra-' + str(index))).mkdir(mode=0o700)
    else:
        directory = c.runtime_dir / 'session_index'
        directory.mkdir(mode=0o700)
        for index in range(1100):
            (directory / (str(index + 100) + '.json')).write_text('unrelated history')
    row = c.create('GrowthOwner', 'codex', 'growth-model')
    assert c.save_profile()['agent_id'] == row['agent_id']
    c.record_session({'session_id': 'growth-first', 'tool_response': c.call('whois')})
    for session in ['growth-second', 'growth-third']:
        active = client(prepared)
        assert active.observe()['agent_id'] == row['agent_id']
        assert active.reconnect()['agent_id'] == row['agent_id']
        active.record_session({'session_id': session, 'tool_response': active.call('whois')})
        assert active.save_profile()['agent_id'] == row['agent_id']
        assert active.call('health_check')['activation_enabled'] is False
        assert active.exports('codex')
    assert api['read_json'](c.runtime_dir / 'session_index/self.json')['session_id'] == 'growth-third'
    result = run(prepared, 'bin/agentstack-daemon', 'inspect', '--profile', str(c.profile_path))
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize('stage', ['registration', 'credential', 'context', 'metadata', 'profile'])
def test_each_registration_output_interruption_finalizes_same_identity(prepared, monkeypatch, stage):
    config = prepared['client_config']
    config['identity'] = None
    fresh_context(prepared, config, 'interrupted-client')
    c = client(prepared)
    before = s1.call(prepared, 'health_check')['mutation_revision']
    original = c.write_output
    def interrupted(path, value, role):
        original(path, value, role)
        if role == stage and (role != 'registration' or 'row' in value):
            raise OSError('synthetic saved-slot interruption')
    monkeypatch.setattr(c, 'write_output', interrupted)
    with pytest.raises(OSError, match='saved-slot'):
        c.create('InterruptedOwner', 'daemon', 'deterministic')
        c.save_profile()
    from agentstack_mail.namespace_state_io import connection
    with connection(Path(prepared['config']['database'])) as db:
        agent_id = db.execute("SELECT id FROM agents WHERE name='InterruptedOwner'").fetchone()[0]
    assert s1.call(prepared, 'health_check')['mutation_revision'] == before + 1
    result = run(prepared, 'bin/agentstack-daemon', 'finalize-create', '--connection', str(c.path),
                 '--agent-id', str(agent_id), '--operator')
    assert result.returncode == 0, result.stderr
    active = client(prepared)
    assert active.observe()['agent_id'] == agent_id
    assert active.registration_metadata() == {'kind': api['METADATA_KIND'], **active.binding,
        'agent_id': agent_id, 'program': 'daemon', 'model': 'deterministic'}
    assert api['read_json'](active.profile_path)['agent_id'] == agent_id
    assert not active.outputs['registration'].exists()
    assert s1.call(prepared, 'health_check')['mutation_revision'] == before + 1


@pytest.mark.parametrize('action', ['recover', 'claim'])
@pytest.mark.parametrize('stage', ['enrollment', 'credential', 'context'])
def test_each_enrollment_output_interruption_recovers_same_cas(prepared, monkeypatch, stage, action):
    if action == 'claim':
        from agentstack_mail.namespace_state_io import connection
        with connection(Path(prepared['config']['database']), write=True) as db:
            db.execute('UPDATE agents SET registration_token=NULL,credential_generation=0 WHERE id=1')
        config = prepared['client_config']
        config['identity']['credential_generation'] = 0
        private(prepared['client_path'], config)
    c = client(prepared)
    generation = c.identity['credential_generation']
    before = s1.call(prepared, 'health_check')['mutation_revision']
    original = c.write_output
    def interrupted(path, value, role):
        original(path, value, role)
        if role == stage:
            raise OSError('synthetic saved-slot interruption')
    monkeypatch.setattr(c, 'write_output', interrupted)
    with pytest.raises(OSError, match='saved-slot'):
        c.enroll(action, 'interrupted-recovery', generation)
    result = run(prepared, 'bin/agentstack-enroll', '--global-context', str(c.path),
                 action, 'interrupted-recovery', str(generation), bash=True)
    assert result.returncode == 0, result.stderr
    active = client(prepared)
    assert active.observe()['agent_id'] == 1
    assert active.identity['credential_generation'] == generation + 1
    assert not active.outputs['enrollment'].exists()
    assert s1.call(prepared, 'health_check')['mutation_revision'] == before + 1


@pytest.mark.parametrize('mismatch', ['token', 'agent_id', 'generation', 'pending_binding', 'server_token'])
def test_explicit_finalize_rejects_inconsistent_interrupted_state(prepared, monkeypatch, mismatch):
    config = prepared['client_config']
    config['identity'] = None
    fresh_context(prepared, config, 'inconsistent-client')
    c = client(prepared)
    original = c.write_output
    def interrupted(path, value, role):
        original(path, value, role)
        if role == 'credential':
            raise OSError('synthetic interruption')
    monkeypatch.setattr(c, 'write_output', interrupted)
    with pytest.raises(OSError):
        c.create('InconsistentOwner', 'daemon', 'deterministic')
    credential = api['read_json'](c.credential)
    agent_id = credential['agent_id']
    if mismatch == 'pending_binding':
        saved = api['read_json'](c.outputs['registration'])
        private(c.outputs['registration'], {**saved, 'authority_epoch': 'other-epoch'})
    elif mismatch == 'server_token':
        s1.control(prepared, action='recover', agent_id=agent_id, request_id='foreign-recovery',
                     expected_generation=1, new_credential='different-owner-token-000000001')
    else:
        key, value = {'token': ('registration_token', 'different-owner-token-000000001'),
                      'agent_id': ('agent_id', 1), 'generation': ('credential_generation', 8)}[mismatch]
        private(c.credential, {**credential, key: value})
    paths = [c.path, c.credential, c.outputs['registration']]
    original_bytes = {p: p.read_bytes() for p in paths}
    before = s1.call(prepared, 'health_check')['mutation_revision']
    result = run(prepared, 'bin/agentstack-daemon', 'finalize-create', '--connection', str(c.path),
                 '--agent-id', str(agent_id), '--operator')
    assert result.returncode == 2
    assert {p: p.read_bytes() for p in paths} == original_bytes
    assert s1.call(prepared, 'health_check')['mutation_revision'] == before


def test_journal_saved_before_request_is_not_proof_of_registration(prepared, monkeypatch):
    config = prepared['client_config']
    config['identity'] = None
    fresh_context(prepared, config, 'prepared-only-client')
    c = client(prepared)
    before = s1.call(prepared, 'health_check')['mutation_revision']
    original = c.write_output
    def interrupted(path, value, role):
        original(path, value, role)
        if role == 'registration':
            raise OSError('before first request')
    monkeypatch.setattr(c, 'write_output', interrupted)
    with pytest.raises(OSError):
        c.create('PreparedOnlyOwner', 'daemon', 'deterministic')
    saved = c.outputs['registration'].read_bytes()
    result = run(prepared, 'bin/agentstack-daemon', 'finalize-create', '--connection', str(c.path),
                 '--agent-id', '1', '--operator')
    assert result.returncode == 2
    assert not c.credential.exists()
    assert c.outputs['registration'].read_bytes() == saved
    assert s1.call(prepared, 'health_check')['mutation_revision'] == before


@pytest.mark.parametrize('stage', [None, 'enrollment', 'credential', 'context'])
def test_claim_without_local_credential_and_saved_slot_replay(prepared, monkeypatch, stage):
    from agentstack_mail.namespace_state_io import connection
    with connection(Path(prepared['config']['database']), write=True) as db:
        db.execute('UPDATE agents SET registration_token=NULL,credential_generation=0 WHERE id=1')
    config = prepared['client_config']
    config['identity']['credential_generation'] = 0
    private(prepared['client_path'], config)
    c = client(prepared)
    c.credential.unlink()
    before = s1.call(prepared, 'health_check')['mutation_revision']
    if stage is not None:
        original = c.write_output
        def interrupted(path, value, role):
            original(path, value, role)
            if role == stage:
                raise OSError('synthetic missing-credential saved-slot interruption')
        monkeypatch.setattr(c, 'write_output', interrupted)
        with pytest.raises(OSError, match='saved-slot'):
            c.enroll('claim', 'missing-credential-claim', 0)
        pending = api['read_json'](c.outputs['enrollment'])
        assert pending['previous_credential_generation'] is None
        assert pending['previous_credential_fingerprint'] is None
        assert s1.call(prepared, 'health_check')['mutation_revision'] == before + (stage != 'enrollment')
    result = run(prepared, 'bin/agentstack-enroll', '--global-context', str(c.path),
                 'claim', 'missing-credential-claim', '0', bash=True)
    assert result.returncode == 0, result.stderr
    active = client(prepared)
    assert active.observe()['agent_id'] == 1
    assert active.identity['credential_generation'] == 1
    assert not active.outputs['enrollment'].exists()
    assert s1.call(prepared, 'health_check')['mutation_revision'] == before + 1


def test_missing_credential_claim_pending_rejects_an_unrelated_appearing_credential(prepared, monkeypatch):
    from agentstack_mail.namespace_state_io import connection
    with connection(Path(prepared['config']['database']), write=True) as db:
        db.execute('UPDATE agents SET registration_token=NULL,credential_generation=0 WHERE id=1')
    config = prepared['client_config']
    config['identity']['credential_generation'] = 0
    private(prepared['client_path'], config)
    c = client(prepared)
    c.credential.unlink()
    original = c.write_output
    def interrupted(path, value, role):
        original(path, value, role)
        if role == 'enrollment':
            raise OSError('before claim request')
    monkeypatch.setattr(c, 'write_output', interrupted)
    with pytest.raises(OSError):
        c.enroll('claim', 'missing-credential-refusal', 0)
    private(c.credential, {'kind': 'orrery-global-credential-v1', **c.binding,
                          **c.identity, 'registration_token': 'unrelated-local-token-0000001'})
    files = [c.path, c.credential, c.outputs['enrollment']]
    before = {p: p.read_bytes() for p in files}
    revision = s1.call(prepared, 'health_check')['mutation_revision']
    result = run(prepared, 'bin/agentstack-enroll', '--global-context', str(c.path),
                 'claim', 'missing-credential-refusal', '0', bash=True)
    assert result.returncode == 2 and 'RECOVERY_CREDENTIAL_MISMATCH' in result.stderr
    assert {p: p.read_bytes() for p in files} == before
    assert s1.call(prepared, 'health_check')['mutation_revision'] == revision
