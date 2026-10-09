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
        (state['root'] / 'client-runtime').mkdir(mode=0o700)
        info = Path(c['authority_lock']).stat()
        config = {'kind': api['KIND'], 'mode': 'global', 'activation_enabled': False,
                  'wrapper_root': str(ROOT), 'isolation_root': c['isolation_root'],
                  'runtime_root': c['runtime_root'], 'runtime_dir': str(state['root'] / 'client-runtime'), 'authority': c['authority'],
                  'authority_lock': c['authority_lock'], 'lock_identity': [info.st_dev, info.st_ino],
                  'management_socket': c['management_socket'], 'credential_file': str(state['root'] / 'credential.json'),
                  'mcp_url': state['url'], **state['binding'],
                  'identity': {'agent_id': 1, 'credential_generation': 3, 'name': 'OldAlpha'}}
        # Read only synthetic migration fixture metadata to obtain preserved
        # credential values. Never query a real Mail database.
        from agentstack_mail.namespace_state_io import connection
        with connection(Path(c['database'])) as db:
            row = dict(db.execute('SELECT * FROM agents WHERE id=1').fetchone())
        config['identity']['credential_generation'] = row['credential_generation']
        private(state['root'] / 'client.json', config)
        private(Path(config['credential_file']), {'kind': 'orrery-global-credential-v1', **state['binding'],
             **config['identity'], 'registration_token': row['registration_token']})
        state.update(client_path=state['root'] / 'client.json', client_config=config,
                     token=row['registration_token'])
        monkeypatch.setenv('AGENTSTACK_CLIENT_CONFIG', str(state['client_path']))
        monkeypatch.setenv('AGENTSTACK_PROJECT_KEY', '/obsolete/scope')
        monkeypatch.setenv('PROJECT_KEY', '/another/obsolete/scope')
        monkeypatch.setenv('AGENT_NAME', 'UnrelatedName')
        monkeypatch.setenv('AGENTSTACK_MCP_URL', 'http://127.0.0.1:1/mcp')
        yield state
    finally:
        fixture.close()


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
    row = c.reconnect()
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
    path = Path(prepared['client_config']['credential_file'])
    credential = api['read_json'](path)
    credential[key] = value
    private(path, credential)
    with pytest.raises(ClientError, match='CREDENTIAL_BINDING_MISMATCH'):
        client(prepared).reconnect()


def test_new_registration_atomic_conflict_and_no_name_preflight(prepared):
    c = prepared['client_config']
    c['identity'] = None
    c['credential_file'] = str(prepared['root'] / 'new-credential.json')
    private(prepared['client_path'], c)
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
    profile = prepared['root'] / 'profile.json'
    r = run(prepared, 'bin/lib/runtime_client.py', 'save-profile', str(profile))
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
    credential_path = Path(config['credential_file'])
    private(credential_path, {**api['read_json'](credential_path), **config['identity']})
    private(prepared['client_path'], config)
    if valid:
        assert client(prepared).reconnect()['window_uuid'] == row['window_uuid']
    else:
        with pytest.raises(ClientError, match='WINDOW_OWNER_MISMATCH'):
            client(prepared).reconnect()


def test_stale_replayed_receipt_cannot_activate_old_token(prepared):
    c = client(prepared)
    token = 'first-recovery-token-fixture-123456'
    generation = c.identity['credential_generation']
    pending = c.credential.with_suffix('.enrollment-pending.json')
    private(pending, {'action': 'recover', 'agent_id': 1, 'request_id': 'stale-request',
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
    profile = prepared['root'] / 'mismatched-profile.json'
    private(profile, {'kind': api['PROFILE_KIND'], 'client_config': str(c.path), **row, 'agent_id': 2})
    revision = c.call('health_check')['mutation_revision']
    with pytest.raises(ClientError, match='PROFILE_BINDING_MISMATCH'):
        api['profile_operation'](profile, 'reconnect')
    assert c.call('health_check')['mutation_revision'] == revision


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
    credential = Path(config['credential_file'])
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
    credential = Path(config['credential_file'])
    private(credential, {**api['read_json'](credential), **config['identity']})
    private(prepared['client_path'], config)
    c = client(prepared)
    observed = c.observe()
    assert observed['window_verification'] == 'local-only-server-unverified'
    assert c.reconnect()['window_verification'] == 'server-verified-by-register'
    profile = prepared['root'] / 'window-profile.json'
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
    record = api['read_json'](Path(prepared['client_config']['runtime_dir']) / 'session_index/1.json')
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
    config = {**prepared['client_config'], 'wrapper_root': str(installed)}
    private(prepared['client_path'], config)
    r = subprocess.run(['/bin/bash', str(installed / 'bin/agentstack-reregister'), 'ObsoleteName'],
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
    config['credential_file'] = str(prepared['root'] / 'uncertain-credential.json')
    private(prepared['client_path'], config)
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
    credential = Path(config['credential_file'])
    original = api['read_private'](credential)
    with pytest.raises(ClientError, match='CREDENTIAL_ALREADY_PRESENT'):
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
    config['credential_file'] = str(prepared['root'] / 'recipient-credential.json')
    private(Path(config['credential_file']), {'kind': 'orrery-global-credential-v1',
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
    config['credential_file'] = str(prepared['root'] / 'daemon-credential.json')
    private(prepared['client_path'], config)
    profile = prepared['root'] / 'daemon-profile.json'
    r = run(prepared, 'bin/agentstack-daemon', 'create', '--connection', str(prepared['client_path']),
            '--name', 'PreparedDaemon', '--journal', str(profile), '--operator')
    assert r.returncode == 0, r.stderr
    row = json.loads(r.stdout)
    assert row['ready'] is False
    assert row['runtime_execution'] == 'requires-pr4c'
    saved = api['read_json'](profile)
    assert saved['kind'] == api['PROFILE_KIND'] and 'project_key' not in saved
    r = run(prepared, 'bin/agentstack-daemon', 'finalize-create', '--connection', str(prepared['client_path']),
            '--journal', str(profile), '--agent-id', str(row['agent_id']), '--operator')
    assert r.returncode == 0, r.stderr
    assert client(prepared).observe()['agent_id'] == row['agent_id']


def test_uncertain_registration_finalizes_same_id_without_register_retry(prepared, monkeypatch):
    config = prepared['client_config']
    config['identity'] = None
    config['credential_file'] = str(prepared['root'] / 'finalize-credential.json')
    private(prepared['client_path'], config)
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
