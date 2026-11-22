"""Authenticate old Claude child owners before migrating; never enroll or guess."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
from datetime import datetime, timezone, timedelta

import pytest

from dashboard import server
from hooks import child_resume
from test_claude_resume_mail import NAME, TOKEN, private, resume, tmux_resume

REAL_MCP_CALL = server._mcp_call


def owned_database(path, registration):
    with sqlite3.connect(path) as con:
        con.executescript('CREATE TABLE projects (id INTEGER PRIMARY KEY, human_key TEXT); '
                          'CREATE TABLE agents (id INTEGER PRIMARY KEY, name TEXT, project_id INTEGER, program TEXT, registration_token TEXT);')
        con.execute('INSERT INTO projects VALUES (5,?)', (registration['project_key'],))
        con.execute('INSERT INTO agents VALUES (?,?,?,?,?)', (registration['agent_id'], NAME, 5, registration['program'], TOKEN))


def legacy_files(runtime, registration):
    state = runtime / 'child-agents' / f'{NAME}.json'
    config = runtime / 'child-agents' / f'{NAME}.mcp.json'
    private(state, json.dumps({key: registration[key] for key in ('agent_name', 'project_key')} | {'registration_token': TOKEN}))
    private(config, '{"mcpServers":{"old-private-server":{"command":"fixture"}}}\n')
    state.chmod(0o400)
    config.chmod(0o400)
    return state, config


def snapshot(runtime):
    return {path: (path.read_bytes(), path.stat().st_mode, path.stat().st_mtime_ns)
            for path in runtime.rglob('*') if path.is_file()}


def same_material(before):
    for path, value in before.items():
        assert (path.read_bytes(), path.stat().st_mode, path.stat().st_mtime_ns) == value


@pytest.mark.parametrize('allowed', [None, {'project_key', 'name', 'registration_token'}])
def test_old_or_unknown_mail_schema_never_drops_existing_owner_guard(monkeypatch, allowed):
    monkeypatch.setattr(server, '_mcp_tool_parameters', lambda _tool: allowed)
    monkeypatch.setattr(server, '_mcp_jsonrpc', lambda *_a, **_k: pytest.fail('Unprotected registration called'))
    result = server._mcp_call('register_agent', {'existing_agent_id': 73, 'name': NAME})
    assert not result['ok']
    assert result['error_code'] == 'existing_owner_authentication_unavailable'


def test_supported_mail_schema_sends_existing_owner_guard(monkeypatch):
    monkeypatch.setattr(server, '_mcp_tool_parameters', lambda _tool: {'existing_agent_id', 'name'})
    calls = []
    def rpc(method, args, timeout):
        calls.append((method, args))
        return {'ok': True, 'result': {'structuredContent': {'id': 73, 'name': NAME}}}
    monkeypatch.setattr(server, '_mcp_jsonrpc', rpc)
    assert server._mcp_call('register_agent', {'existing_agent_id': 73, 'name': NAME})['ok']
    assert calls == [('tools/call', {'name': 'register_agent', 'arguments': {'existing_agent_id': 73, 'name': NAME}})]


@pytest.fixture
def legacy(resume, monkeypatch, tmp_path):
    runtime, registration, launches, calls = resume
    registration['project_id'] = 5
    state, config = legacy_files(runtime, registration)
    db = tmp_path / 'owned.sqlite3'
    owned_database(db, registration)
    monkeypatch.setattr(server, 'DB_PATH', str(db))
    monkeypatch.setattr(server, '_cached_claude_transcript_path', lambda _name: (True, server._transcript_path(NAME), ()))
    original = server._mcp_call
    def call(method, args):
        result = original(method, args)
        if method == 'register_agent':
            result['data'].update(program=registration['program'], project_id=5)
        return result
    monkeypatch.setattr(server, '_mcp_call', call)
    private(runtime / 'agent_token_OtherOwner', 'different-fixture-owner')
    private(runtime / 'child-agents' / 'OtherOwner.json', '{"unrelated":"state"}\n')
    return runtime, registration, launches, calls, state, config, db


@pytest.mark.parametrize('supported', [False, None, True])
def test_resume_old_mail_has_fixed_update_diagnosis_and_new_mail_still_launches(legacy, monkeypatch, supported):
    runtime, registration, launches, _, *_ = legacy
    before = snapshot(runtime)
    rpc_calls = []
    def parameters(tool):
        if tool != 'register_agent':
            return {'project_key', 'agent_name'}
        if supported is None:
            return None
        fields = {'project_key', 'name', 'program', 'model', 'task_description', 'registration_token'}
        return fields | {'existing_agent_id'} if supported else fields
    def rpc(method, params, timeout):
        rpc_calls.append((method, params))
        if params['name'] == 'register_agent':
            data = {'id': registration['agent_id'], 'name': NAME, 'program': 'claude-code',
                    'project_id': 5, 'retired_at': registration['retired_at']}
        else:
            data = {'status': 'active', 'agent_name': NAME, 'project_key': registration['project_key']}
        return {'ok': True, 'result': {'structuredContent': data}}
    monkeypatch.setattr(server, '_mcp_tool_parameters', parameters)
    monkeypatch.setattr(server, '_mcp_jsonrpc', rpc)
    monkeypatch.setattr(server, '_mcp_call', REAL_MCP_CALL)
    result = server.do_resume(NAME)
    if supported:
        assert result['ok'] and launches
        assert [params['name'] for _, params in rpc_calls] == ['register_agent', 'unretire_agent']
    elif supported is False:
        assert result['ok'] and result['resume_mode'] == 'conversation_only'
        assert result['mail_reason'] == 'mail_schema_unsupported'
        assert launches and not rpc_calls
        same_material(before)
    else:
        assert not result['ok'] and result['resume_capability'] == 'config_unrestorable'
        assert result['error'] == ('Update and restart the dashboard and bundled ORRERY Mail together; '
                                   'existing-owner authentication support could not be confirmed')
        assert not rpc_calls and not launches and snapshot(runtime) == before
    assert TOKEN not in json.dumps(result)


def test_remote_auth_error_cannot_spoof_update_diagnosis_or_disclose_token(legacy, monkeypatch):
    runtime, _, launches, _, *_ = legacy
    before = snapshot(runtime)
    monkeypatch.setattr(server, '_mcp_tool_parameters', lambda _tool: {'existing_agent_id', 'registration_token'})
    monkeypatch.setattr(server, '_mcp_jsonrpc', lambda *_a, **_k: {
        'ok': True, 'result': {'isError': True, 'content': [{'text': TOKEN + ' ORRERY Mail needs existing-owner authentication support'}]}})
    monkeypatch.setattr(server, '_mcp_call', REAL_MCP_CALL)
    result = server.do_resume(NAME)
    assert not result['ok'] and result['resume_capability'] == 'credential_missing'
    assert result['error'] == 'Claude owner authentication failed; refusing resume'
    assert not launches and snapshot(runtime) == before
    assert TOKEN not in json.dumps(result)


@pytest.mark.parametrize('verify', [False, True])
def test_legacy_roster_preflight_is_ready_without_writes_or_mail(legacy, verify):
    runtime, _, launches, calls, *_ = legacy
    before = snapshot(runtime)
    assert server._resume_capability(NAME, 'claude-code', category='finished', verify_transcript=verify) == 'ready'
    assert snapshot(runtime) == before
    assert not calls and not launches


def test_authenticated_legacy_migration_keeps_owner_and_then_resumes_normally(legacy):
    runtime, registration, launches, calls, state, config, _ = legacy
    token = runtime / ('agent_token_' + NAME)
    token_before = (token.read_bytes(), token.stat().st_mode, token.stat().st_mtime_ns)
    other_before = {path: value for path, value in snapshot(runtime).items() if 'OtherOwner' in path.name}
    before = datetime.now(timezone.utc)
    result = server.do_resume(NAME, open_terminal=False)
    assert result['ok'], result
    assert [method for method, _ in calls] == ['register_agent', 'unretire_agent']
    assert calls[0][1]['existing_agent_id'] == registration['agent_id']
    current = json.loads(state.read_text())
    assert current['schema_version'] == 1 and current['provider'] == 'claude'
    assert current['agent_id'] == registration['agent_id'] and current['project_id'] == 5
    assert current['registration_token'] == TOKEN and current['resume_in_progress_at']
    assert 'legacy_migration_generation' not in current
    expires = datetime.fromisoformat(current['resume_expires_at'].replace('Z', '+00:00'))
    assert before + timedelta(days=30) - timedelta(seconds=1) <= expires <= datetime.now(timezone.utc) + timedelta(days=30)
    proxy = json.loads(config.read_text())['mcpServers']['orrery-mail']
    assert proxy['env']['AGENTSTACK_PROXY_AGENT_NAME'] == NAME
    assert TOKEN not in json.dumps(result) and TOKEN not in repr(launches)
    assert (token.read_bytes(), token.stat().st_mode, token.stat().st_mtime_ns) == token_before
    same_material(other_before)
    assert not list(state.parent.glob('*.legacy-migration.json'))
    # Normal completion and the next resume use the strict modern path.
    assert child_resume.mark_retired(runtime, NAME, retention_days=30)
    calls.clear()
    assert server._resume_capability(NAME, 'claude-code', category='finished') == 'ready'
    assert server.do_resume(NAME)['ok']
    assert [method for method, _ in calls] == ['register_agent', 'unretire_agent']


@pytest.mark.parametrize('change', ['missing_row', 'null_owner', 'empty_owner', 'wrong_provider', 'wrong_project', 'wrong_name'])
def test_no_legacy_registration_or_owner_claim_is_attempted(legacy, change):
    runtime, _, launches, calls, _, _, db = legacy
    with sqlite3.connect(db) as con:
        if change == 'missing_row':
            con.execute('DELETE FROM agents')
        elif change == 'null_owner':
            con.execute('UPDATE agents SET registration_token=NULL')
        elif change == 'empty_owner':
            con.execute("UPDATE agents SET registration_token=''")
        elif change == 'wrong_provider':
            con.execute("UPDATE agents SET program='codex'")
        elif change == 'wrong_project':
            con.execute("UPDATE projects SET human_key='/different/project'")
        else:
            con.execute("UPDATE agents SET name='OtherOwner'")
    before = snapshot(runtime)
    with sqlite3.connect(db) as con:
        db_before = con.execute('SELECT * FROM agents').fetchall()
    result = server.do_resume(NAME)
    assert not result['ok'] and not calls and not launches
    same_material(before)
    with sqlite3.connect(db) as con:
        assert con.execute('SELECT * FROM agents').fetchall() == db_before


@pytest.mark.parametrize('failure', ['auth', 'wrong_id', 'wrong_name', 'wrong_project_id', 'wrong_provider', 'changed_claude_program'])
def test_legacy_owner_authentication_failure_never_migrates_or_launches(legacy, monkeypatch, failure):
    runtime, _, launches, calls, *_ = legacy
    before = snapshot(runtime)
    original = server._mcp_call
    def call(method, args):
        result = original(method, args)
        if method == 'register_agent':
            if failure == 'auth':
                return {'ok': False, 'error': TOKEN}
            result['data'].update({'wrong_id': {'id': 99}, 'wrong_name': {'name': 'OtherOwner'},
                                   'wrong_project_id': {'project_id': 999}, 'wrong_provider': {'program': 'codex'},
                                   'changed_claude_program': {'program': 'claude'}}[failure])
        return result
    monkeypatch.setattr(server, '_mcp_call', call)
    result = server.do_resume(NAME)
    assert not result['ok'] and not launches
    assert result['resume_capability'] == ('credential_missing' if failure == 'auth' else 'identity_mismatch')
    assert [m for m, _ in calls] == ['register_agent']
    assert TOKEN not in json.dumps(result)
    same_material(before)


@pytest.mark.parametrize('damage', ['token_mismatch', 'wrong_name', 'wrong_project', 'state_public', 'token_public', 'mcp_public', 'state_symlink', 'token_symlink', 'mcp_symlink', 'modern_incomplete', 'tombstone', 'pending'])
def test_invalid_legacy_material_does_not_bypass_validation(legacy, damage):
    runtime, registration, launches, calls, state, config, _ = legacy
    token = runtime / ('agent_token_' + NAME)
    data = json.loads(state.read_text())
    if damage == 'token_mismatch':
        private(token, 'another-owner')
    elif damage == 'missing_token':
        token.unlink()
    elif damage in {'wrong_name', 'wrong_project'}:
        data['agent_name' if damage == 'wrong_name' else 'project_key'] = 'different-value'
        state.chmod(0o600)
        private(state, json.dumps(data))
    elif damage in {'state_public', 'token_public', 'mcp_public'}:
        {'state_public':state,'token_public':token,'mcp_public':config}[damage].chmod(0o644)
    elif damage.endswith('_symlink'):
        path = {'state_symlink': state, 'token_symlink': token, 'mcp_symlink': config}[damage]
        target = path.with_suffix('.target')
        path.rename(target)
        path.symlink_to(target)
    elif damage == 'modern_incomplete':
        state.chmod(0o600)
        private(state, json.dumps({**data, 'schema_version': 1}))
    elif damage == 'tombstone':
        private(runtime / 'child-resume-tombstones' / f"{registration['agent_id']}.json", '{}')
    else:
        private(state.with_name(f'.{NAME}.registration-pending.json'), '{}')
    before = snapshot(runtime)
    assert server._resume_capability(NAME, 'claude-code', category='finished') != 'ready'
    result = server.do_resume(NAME)
    assert not result['ok'] and not calls and not launches
    same_material(before)


@pytest.mark.parametrize('days', ['0', '-1', 'bad'])
def test_legacy_retention_disabled_or_invalid_never_authenticates(legacy, monkeypatch, days):
    runtime, _, launches, calls, *_ = legacy
    before = snapshot(runtime)
    monkeypatch.setenv('AGENTSTACK_CHILD_RESUME_RETENTION_DAYS', days)
    assert server._resume_capability(NAME, 'claude-code', category='finished') != 'ready'
    assert not server.do_resume(NAME)['ok'] and not calls and not launches
    same_material(before)


@pytest.mark.parametrize('failure', ['unretire', 'config', 'launch', 'save'])
def test_migration_failure_restores_exact_old_material_and_mail(legacy, monkeypatch, failure):
    runtime, _, launches, calls, state, _, _ = legacy
    before = snapshot(runtime)
    original = server._mcp_call
    if failure == 'unretire':
        monkeypatch.setattr(server, '_mcp_call', lambda method,args:
                            original(method,args) if method != 'unretire_agent' else calls.append((method,args)) or {'ok': False})
    elif failure == 'config':
        monkeypatch.setattr(server, '_write_claude_resume_mcp_config', lambda *args: (_ for _ in ()).throw(OSError('fixture')))
    elif failure == 'launch':
        monkeypatch.setattr(server, '_launch_claude_resume_tmux', lambda *args,**kwargs: {'ok': False, 'error': 'fixture launch failure'})
    else:
        module = server._child_resume_module()
        original_write = module._atomic_json
        def write(path,payload):
            if path == state and payload.get('legacy_migration_generation'):
                raise OSError('fixture atomic save failure')
            return original_write(path,payload)
        monkeypatch.setattr(module, '_atomic_json', write)
    result = server.do_resume(NAME)
    assert not result['ok'], result
    same_material(before)
    assert not list(state.parent.glob('*.legacy-migration.json'))
    methods = [method for method,_ in calls]
    assert ('retire_agent' in methods) is (failure in {'unretire','launch'})
    assert TOKEN not in json.dumps(result)


def test_superseded_migration_undo_never_restores_a_purged_or_retried_identity(legacy):
    runtime, registration, _, _, state, _, _ = legacy
    candidate = child_resume.inspect_legacy_claude(runtime, NAME, agent_id=registration['agent_id'], project_key=registration['project_key'], program='claude-code')
    child_resume.begin_legacy_claude_migration(runtime, NAME, registration=registration, legacy=candidate, generation='a'*32, retention_days=30)
    child_resume.cancel_resume(runtime, NAME, agent_id=registration['agent_id'], project_key=registration['project_key'])
    assert child_resume.purge_one(runtime, NAME, reason='purged')
    assert not list(state.parent.glob('*.legacy-migration.json'))
    # Operator starts the same formal registration again; old migration cannot revive undo.
    tombstone = runtime / 'child-resume-tombstones' / f"{registration['agent_id']}.json"
    tombstone.unlink()
    private(runtime / ('agent_token_' + NAME), TOKEN)
    legacy_files(runtime, registration)
    candidate = child_resume.inspect_legacy_claude(runtime, NAME, agent_id=registration['agent_id'], project_key=registration['project_key'], program='claude-code')
    child_resume.begin_legacy_claude_migration(runtime, NAME, registration=registration, legacy=candidate, generation='b'*32, retention_days=30)
    before = snapshot(runtime)
    assert not child_resume.finish_legacy_claude_migration(runtime, NAME, generation='a'*32, rollback=True)
    same_material(before)
    assert child_resume.finish_legacy_claude_migration(runtime, NAME, generation='b'*32, rollback=True)
    assert set(json.loads(state.read_text())) == {'agent_name','project_key','registration_token'}


@pytest.fixture
def legacy_tmux(tmux_resume, monkeypatch, tmp_path):
    runtime, registration, calls, sessions, status = tmux_resume
    registration['project_id'] = 5
    state, config = legacy_files(runtime, registration)
    db = tmp_path / 'owned.sqlite3'
    owned_database(db, registration)
    monkeypatch.setattr(server, 'DB_PATH', str(db))
    original = server._mcp_call
    def call(method, args):
        result = original(method, args)
        if method == 'register_agent':
            result['data'].update(program=registration['program'], project_id=5)
        return result
    monkeypatch.setattr(server, '_mcp_call', call)
    sessions['$99'] = NAME + 'Sibling'
    private(runtime / ('agent_token_' + NAME + 'Sibling'), 'another-owner')
    return runtime, registration, calls, sessions, status, state, config


@pytest.mark.parametrize('failure', [None, 'unretire', 'create', 'window', 'rename', 'release'])
@pytest.mark.parametrize('was_active', [False, True])
def test_legacy_migration_keeps_husk_original_mail_and_other_agent_on_failure(legacy_tmux, failure, was_active):
    runtime, _, calls, sessions, status, state, _ = legacy_tmux
    before = snapshot(runtime)
    status.update(fail=failure, active=was_active)
    result = server.do_jump(NAME, open_terminal=True)
    assert result['ok'] is (failure is None), result
    assert status['started'] is (failure is None)
    assert status['active'] is (True if failure is None else was_active)
    assert sessions['$99'] == NAME + 'Sibling'
    assert all('$99' not in argv for argv in status['commands'])
    if failure is not None:
        assert sessions['$1'] == NAME and len(sessions) == 2
        same_material(before)
    assert not list(state.parent.glob('*.legacy-migration.json'))
    assert ('retire_agent' in [method for method, _ in calls]) is (failure is not None and not was_active)


def test_legacy_authentication_cannot_overwrite_material_changed_during_the_call(legacy, monkeypatch):
    runtime, _, launches, _, state, _, _ = legacy
    original = server._mcp_call
    newer = {}
    def call(method, args):
        result = original(method, args)
        if method == 'register_agent':
            state.chmod(0o600)
            data = json.loads(state.read_text())
            private(state, json.dumps({**data, 'schema_version': 1, 'operator_new_material': True}))
            newer.update(snapshot(runtime))
        return result
    monkeypatch.setattr(server, '_mcp_call', call)
    assert not server.do_resume(NAME)['ok'] and not launches
    same_material(newer)


def test_legacy_uid_mismatch_never_authenticates(legacy, monkeypatch):
    runtime, _, launches, calls, *_ = legacy
    before = snapshot(runtime)
    uid = os.getuid()
    monkeypatch.setattr(server._child_resume_module().os, 'getuid', lambda: uid + 1)
    result = server.do_resume(NAME)
    assert not result['ok'] and result['resume_capability'] == 'credential_permission'
    assert not calls and not launches
    same_material(before)


@pytest.mark.parametrize('success', [False, True])
def test_missing_old_mcp_is_regenerated_only_for_the_authenticated_attempt(legacy, monkeypatch, success):
    runtime, _, _, _, state, config, _ = legacy
    config.unlink()
    before = snapshot(runtime)
    if not success:
        monkeypatch.setattr(server, '_launch_claude_resume_tmux', lambda *a,**k: {'ok':False,'error':'fixture failed'})
    result = server.do_resume(NAME)
    assert result['ok'] is success
    if success:
        assert config.exists() and 'orrery-mail' in json.loads(config.read_text())['mcpServers']
    else:
        assert not config.exists()
        same_material(before)
    assert not list(state.parent.glob('*.legacy-migration.json'))


def test_pending_legacy_migration_rejects_a_second_preregistration_or_resume(legacy):
    runtime, registration, _, _, state, _, _ = legacy
    candidate = child_resume.inspect_legacy_claude(runtime, NAME, agent_id=registration['agent_id'], project_key=registration['project_key'], program='claude-code')
    child_resume.begin_legacy_claude_migration(runtime, NAME, registration=registration, legacy=candidate, generation='a'*32, retention_days=7)
    before = snapshot(runtime)
    with pytest.raises(child_resume.ResumeStateError):
        child_resume.stage_registration(runtime, NAME, project_key=registration['project_key'], program='claude-code', generation='b'*32)
    with pytest.raises(child_resume.ResumeStateError):
        child_resume.begin_resume(runtime, NAME, agent_id=registration['agent_id'], project_key=registration['project_key'], program='claude-code')
    with pytest.raises(child_resume.ResumeStateError):
        child_resume.prepare_active_state(runtime, NAME, project_key=registration['project_key'], program='claude-code')
    same_material(before)
    assert child_resume.finish_legacy_claude_migration(runtime, NAME, generation='a'*32, rollback=True)


def test_manual_legacy_rollback_requires_the_matching_generation(legacy):
    import subprocess, sys
    runtime, registration, _, _, state, config, _ = legacy
    before = snapshot(runtime)
    candidate = child_resume.inspect_legacy_claude(runtime, NAME, agent_id=registration['agent_id'], project_key=registration['project_key'], program='claude-code')
    child_resume.begin_legacy_claude_migration(runtime, NAME, registration=registration, legacy=candidate, generation='a'*32, retention_days=30)
    config.chmod(0o600)
    private(config, '{"generated":"fixture"}')
    def run(*args):
        return subprocess.run([sys.executable, str(Path(child_resume.__file__)), 'finish-legacy-migration',
                               '--runtime-dir', str(runtime), '--agent-name', NAME, '--rollback', *args],
                              capture_output=True, text=True, timeout=5)
    during = snapshot(runtime)
    assert run().returncode == 2
    assert run('--generation', 'b'*32).returncode == 3
    same_material(during)
    result = run('--generation', 'a'*32)
    assert result.returncode == 0 and TOKEN not in result.stdout + result.stderr
    same_material(before)
    assert state.stat().st_mode & 0o777 == 0o400
    assert config.stat().st_mode & 0o777 == 0o400


def test_superseded_failed_migration_does_not_retire_the_new_attempt(legacy, monkeypatch):
    runtime, registration, _, calls, state, config, _ = legacy
    module = server._child_resume_module()
    newer = {}
    def launch(*args, **kwargs):
        # The old CLI never started; operator purge and retry interleave here.
        module.cancel_resume(runtime, NAME, agent_id=registration['agent_id'], project_key=registration['project_key'])
        assert module.purge_one(runtime, NAME, reason='purged')
        (runtime / 'child-resume-tombstones' / f"{registration['agent_id']}.json").unlink()
        private(runtime / ('agent_token_' + NAME), TOKEN)
        legacy_files(runtime, registration)
        candidate = module.inspect_legacy_claude(runtime, NAME, agent_id=registration['agent_id'], project_key=registration['project_key'], program='claude-code')
        module.begin_legacy_claude_migration(runtime, NAME, registration=registration, legacy=candidate, generation='b'*32, retention_days=30)
        newer.update(snapshot(runtime))
        return {'ok': False, 'error': 'old fixture launch failed'}
    monkeypatch.setattr(server, '_launch_claude_resume_tmux', launch)
    assert not server.do_resume(NAME)['ok']
    same_material(newer)
    assert [method for method, _ in calls] == ['register_agent','unretire_agent']
    assert json.loads(state.read_text())['legacy_migration_generation'] == 'b'*32


def test_retention_grace_uses_authentication_time_and_configured_days(legacy):
    runtime, registration, _, _, state, *_ = legacy
    candidate = child_resume.inspect_legacy_claude(runtime, NAME, agent_id=registration['agent_id'], project_key=registration['project_key'], program='claude-code')
    migrated_at = datetime(2026, 9, 30, 0, 0, tzinfo=timezone.utc)
    os.utime(state, (1, 1))  # mtime cannot become retirement provenance.
    current = child_resume.begin_legacy_claude_migration(runtime, NAME, registration=registration, legacy=candidate,
                                                       generation='a'*32, retention_days=7, now=migrated_at)
    assert current['retired_at'] == '2026-09-30T00:00:00Z'
    assert current['resume_expires_at'] == '2026-10-07T00:00:00Z'
    assert child_resume.finish_legacy_claude_migration(runtime, NAME, generation='a'*32, rollback=True)
