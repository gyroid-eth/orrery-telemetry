"""Conversation compatibility never authenticates or changes a Mail owner."""
from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta
import os
from pathlib import Path
import shlex
import subprocess

import pytest
from dashboard import server
from hooks import child_resume
from test_claude_resume_mail import NAME, TOKEN, ROOT, child, private, resume
from test_claude_legacy_resume import legacy, snapshot, same_material


@pytest.mark.parametrize('kind', ['legacy', 'modern', 'standalone', 'expired', 'expired_removed', 'old_schema'])
def test_safe_missing_or_expired_material_resumes_without_any_mail(resume, monkeypatch, kind, tmp_path):
    runtime, registration, launches, calls = resume
    if kind in {'modern', 'expired', 'expired_removed'}:
        child(runtime, registration)
    elif kind in {'legacy', 'old_schema'}:
        private(runtime / 'child-agents' / f'{NAME}.json', json.dumps({
            'agent_name': NAME, 'project_key': registration['project_key'], 'registration_token': TOKEN}))
    if kind == 'old_schema':
        monkeypatch.setattr(server, '_legacy_claude_owned_registration', lambda _name, value: value)
        monkeypatch.setattr(server, '_mcp_tool_parameters', lambda _tool: {'name', 'registration_token'})
    elif kind == 'expired':
        state_path = runtime / 'child-agents' / f'{NAME}.json'
        state = json.loads(state_path.read_text()); state['resume_expires_at'] = '2000-01-01T00:00:00Z'
        private(state_path, json.dumps(state))
    elif kind == 'expired_removed':
        assert child_resume.purge_one(runtime, NAME, reason='retention_expired', now=datetime.now(timezone.utc)+timedelta(days=31))
    else:
        (runtime / f'agent_token_{NAME}').unlink()
    before = snapshot(runtime)
    monkeypatch.setattr(server, '_mcp_call', lambda *_a, **_k: pytest.fail('Mail tool called'))
    assert server._resume_capability(NAME, 'claude-code', category='gone') == 'ready'
    result = server.do_resume(NAME, open_terminal=False)
    assert result['ok'] and result['resume_mode'] == 'conversation_only'
    assert result['mail_status'] == 'unavailable' and 'cannot send or receive' in result['mail_message']
    expected = 'mail_schema_unsupported' if kind == 'old_schema' else 'retention_expired' if kind.startswith('expired') else 'credential_absent'
    assert result['mail_reason'] == expected
    assert not calls and launches
    command = launches[-1][-1]
    assert '--strict-mcp-config' in command and 'disableAllHooks' in command
    assert '--mcp-config' in command and '{"mcpServers":{}}' in command
    assert 'AGENTSTACK_MAIL_DISABLED=1' in command and 'cleanup-child-agent.sh' not in command
    assert TOKEN not in command + json.dumps(result)
    same_material(before)
    # Execute the actual produced login-shell body with only a synthetic CLI.
    capture = tmp_path / 'cli-capture.json'
    fake_cli = Path(server.ABS_CLAUDE)
    fake_cli.write_text('#!/usr/bin/env python3\nimport json, os, sys\n'
                        f'open({str(capture)!r}, "w").write(json.dumps({{"args":sys.argv[1:],"mode":os.environ.get("AGENTSTACK_MAIL_DISABLED")}}))\n')
    fake_cli.chmod(0o700)
    process = subprocess.run(['/bin/bash', '-c', command], capture_output=True, text=True, timeout=10)
    assert process.returncode == 0, process.stderr
    captured = json.loads(capture.read_text())
    assert captured['mode'] == '1'
    args = captured['args']
    assert json.loads(args[args.index('--mcp-config') + 1]) == {'mcpServers': {}}
    assert json.loads(args[args.index('--settings') + 1]) == {'disableAllHooks': True}
    assert 'cannot send or receive' in process.stdout
    same_material(before)


@pytest.mark.parametrize('damage', ['wrong_token', 'public_token', 'token_symlink', 'wrong_identity',
                                     'public_state', 'corrupt_state', 'pending', 'purged', 'empty_token',
                                     'invalid_token', 'invalid_saved_token', 'unsafe_lock', 'unreadable', 'resume_pending'])
@pytest.mark.parametrize('safe_condition', ['missing', 'expired'])
def test_danger_wins_over_missing_and_expiry(resume, monkeypatch, damage, safe_condition):
    runtime, registration, launches, calls = resume
    child(runtime, registration)
    state_path = runtime / 'child-agents' / f'{NAME}.json'
    token = runtime / f'agent_token_{NAME}'
    state = json.loads(state_path.read_text())
    if safe_condition == 'missing':
        token.unlink()
    else:
        state['resume_expires_at'] = '2000-01-01T00:00:00Z'
        private(state_path, json.dumps(state))
    if damage == 'wrong_token': private(token, 'another-owner')
    elif damage == 'public_token': private(token, TOKEN); token.chmod(0o644)
    elif damage == 'token_symlink': token.unlink(missing_ok=True); token.symlink_to(state_path)
    elif damage == 'wrong_identity': state['agent_id'] += 1; private(state_path, json.dumps(state))
    elif damage == 'public_state': state_path.chmod(0o644)
    elif damage == 'corrupt_state': private(state_path, '{broken')
    elif damage == 'pending': private(state_path.with_name(f'.{NAME}.registration-pending.json'), '{}')
    elif damage == 'purged':
        private(runtime / 'child-resume-tombstones' / f"{registration['agent_id']}.json", json.dumps({
            'agent_id': registration['agent_id'], 'agent_name': NAME, 'project_key': registration['project_key'], 'reason': 'purged'}))
    elif damage == 'empty_token': private(token, '  ')
    elif damage == 'invalid_token': private(token, 'valid'); token.write_bytes(b'\xff')
    elif damage == 'invalid_saved_token': state['registration_token'] = '\ud800'; private(state_path, json.dumps(state))
    elif damage == 'unsafe_lock':
        lock = state_path.with_name(f'.{NAME}.resume.lock'); lock.unlink(missing_ok=True); lock.symlink_to(token if token.exists() else state_path)
    elif damage == 'unreadable':
        original = server._read_private_regular
        def read(path, *args):
            if path == str(state_path): raise server._PrivateFileError('unavailable', 'fixture EIO')
            return original(path, *args)
        monkeypatch.setattr(server, '_read_private_regular', read)
    else: state['resume_in_progress_at'] = '2026-09-30T00:00:00Z'; private(state_path, json.dumps(state))
    before = snapshot(runtime)
    result = server.do_resume(NAME)
    assert not result['ok'], result
    assert result['resume_capability'] != 'ready'
    assert not launches and not calls
    same_material(before)


def test_old_schema_is_not_a_transport_or_authentication_failure(legacy, monkeypatch):
    runtime, _, launches, calls, *_ = legacy
    before = snapshot(runtime)
    monkeypatch.setattr(server, '_mcp_tool_parameters', lambda _tool: None)
    monkeypatch.setattr(server, '_mcp_call', lambda *_a, **_k: {'ok': False, 'error_code': 'existing_owner_authentication_unavailable'})
    result = server.do_resume(NAME)
    assert not result['ok'] and not launches and not calls
    same_material(before)


def test_fallback_revalidation_rejects_changed_material(legacy, monkeypatch):
    runtime, _, launches, calls, *_ = legacy
    monkeypatch.setattr(server, '_mcp_tool_parameters', lambda _tool: {'name'})
    original = server._claude_conversation_reason
    counter = 0
    def inspect(name):
        nonlocal counter
        counter += 1
        if counter == 2: private(runtime / f'agent_token_{NAME}', 'new-owner')
        return original(name)
    monkeypatch.setattr(server, '_claude_conversation_reason', inspect)
    result = server.do_resume(NAME)
    assert not result['ok'] and result['resume_capability'] == 'identity_mismatch'
    assert not launches and not calls
    assert (runtime / f'agent_token_{NAME}').read_text() == 'new-owner'


@pytest.mark.parametrize('replacement', ['symlink_0400', 'symlink_0640', 'public', 'directory', 'fifo', 'hardlink', 'wrong_uid'])
def test_lock_replaced_after_preflight_never_changes_other_material(legacy, monkeypatch, replacement):
    runtime, _, launches, calls, *_ = legacy
    (runtime / f'agent_token_{NAME}').unlink()
    lock = runtime / 'child-agents' / f'.{NAME}.resume.lock'
    lock.unlink(missing_ok=True)
    foreign = runtime / 'agent_token_OtherOwner'
    foreign.chmod(0o400 if replacement == 'symlink_0400' else 0o640)
    before = snapshot(runtime)
    directory_mode = lock.parent.stat().st_mode
    original = server._claude_conversation_reason
    replaced = False
    created = []
    def inspect(name):
        nonlocal replaced
        result = original(name)
        if not replaced:
            replaced = True
            if replacement.startswith('symlink'):
                lock.symlink_to(foreign)
            elif replacement == 'directory': lock.mkdir()
            elif replacement == 'fifo': os.mkfifo(lock, 0o600)
            elif replacement == 'hardlink': os.link(foreign, lock)
            else:
                private(lock, 'replacement lock')
                if replacement == 'public': lock.chmod(0o644)
                else:
                    from types import SimpleNamespace
                    fstat = child_resume.os.fstat
                    inode = lock.stat().st_ino
                    def wrong_uid(fd):
                        info = fstat(fd)
                        if info.st_ino == inode:
                            return SimpleNamespace(st_mode=info.st_mode, st_uid=os.getuid()+1)
                        return info
                    monkeypatch.setattr(child_resume.os, 'fstat', wrong_uid)
            created.append(lock.lstat())
        return result
    monkeypatch.setattr(server, '_claude_conversation_reason', inspect)
    result = server.do_resume(NAME, open_terminal=False)
    assert not result['ok'] and result['resume_capability'] == 'credential_permission', result
    assert not launches and not calls
    same_material(before)
    assert lock.lstat() == created[0]
    assert lock.parent.stat().st_mode == directory_mode


@pytest.mark.parametrize('replacement', ['symlink', 'public'])
def test_lock_changed_while_waiting_for_flock_is_rejected(legacy, monkeypatch, replacement):
    runtime, _, launches, calls, *_ = legacy
    (runtime / f'agent_token_{NAME}').unlink()
    lock = runtime / 'child-agents' / f'.{NAME}.resume.lock'
    private(lock, '')
    before = snapshot(runtime)
    foreign = runtime / 'agent_token_OtherOwner'
    flock = child_resume.fcntl.flock
    changed = []
    def change_after_wait(fd, operation):
        flock(fd, operation)
        if replacement == 'symlink':
            lock.unlink(); lock.symlink_to(foreign)
        else: lock.chmod(0o644)
        changed.append(lock.lstat())
    monkeypatch.setattr(child_resume.fcntl, 'flock', change_after_wait)
    result = server.do_resume(NAME, open_terminal=False)
    assert not result['ok'] and result['resume_capability'] == 'credential_permission', result
    assert not launches and not calls
    same_material({path:value for path,value in before.items() if path != lock})
    assert lock.lstat() == changed[0]


@pytest.mark.parametrize('mode', [0o400, 0o600])
def test_existing_private_lock_is_preserved_during_conversation_resume(legacy, monkeypatch, mode):
    runtime, _, launches, calls, *_ = legacy
    (runtime / f'agent_token_{NAME}').unlink()
    lock = runtime / 'child-agents' / f'.{NAME}.resume.lock'
    private(lock, '')
    lock.chmod(mode)
    before = snapshot(runtime)
    directory_mode = lock.parent.stat().st_mode
    result = server.do_resume(NAME, open_terminal=False)
    assert result['ok'] and result['resume_mode'] == 'conversation_only'
    assert launches and not calls
    same_material(before)
    assert lock.parent.stat().st_mode == directory_mode


@pytest.mark.parametrize('hook', ['session-start-reminder.sh', 'cleanup-child-agent.sh', 'release-all-reservations.sh'])
def test_conversation_mode_actual_hooks_do_not_call_mail_or_write_material(tmp_path, hook):
    runtime = tmp_path / 'runtime'
    private(runtime / f'agent_token_{NAME}', TOKEN)
    before = snapshot(runtime)
    binary = tmp_path / 'bin'; binary.mkdir()
    marker = tmp_path / 'network'
    for name in ('curl', 'agentstack-reregister'):
        path = binary / name
        path.write_text(f'#!/bin/bash\necho called >> {shlex.quote(str(marker))}\nexit 97\n'); path.chmod(0o700)
    result = subprocess.run(['/bin/bash', str(ROOT / 'hooks' / hook), NAME], input='{}', capture_output=True, text=True,
        env={**os.environ, 'HOME':str(tmp_path), 'PATH':str(binary)+':'+os.environ['PATH'],
             'AGENTSTACK_RUNTIME_DIR':str(runtime), 'AGENTSTACK_HOOKS_DIR':str(ROOT / 'hooks'),
             'AGENTSTACK_MAIL_DISABLED':'1', 'AGENT_NAME':NAME}, timeout=10)
    assert result.returncode == 0, result.stderr
    assert not marker.exists()
    same_material(before)


def test_conversation_mode_watcher_skips_delivery_without_consuming_signal(tmp_path):
    source = (ROOT / 'hooks/watch_agent_mail_signals.sh').read_text()
    start = source.index('deliver_worker() {')
    function = source[start:source.index('\n}\n', start)+3]
    signal = tmp_path / '777.signal'; signal.write_text('{"message":{"id":777}}')
    released = tmp_path / 'released'
    script = ('set -euo pipefail\nTMUX_TIMEOUT=5\nPERSISTENT_DELIVER=/nonexistent/fixture-helper\n' + function +
        '\nrun_to() { shift; "$@"; }\n'
        'tmux() { [[ "$1" == show-environment ]] || exit 98; echo AGENTSTACK_MAIL_DISABLED=1; }\n'
        f'release_delivery_lease() {{ echo "$*" > {shlex.quote(str(released))}; }}\n'
        f'deliver_worker {shlex.quote(str(signal))} {NAME} 777 Sender subject high 1 body 0 owner\n')
    result = subprocess.run(['/bin/bash', '-c', script], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert released.exists() and signal.read_text() == '{"message":{"id":777}}'


@pytest.mark.parametrize("headless_rc", [0, 20])
def test_headless_bridge_result_never_probes_tmux(tmp_path, headless_rc):
    source = (ROOT / 'hooks/watch_agent_mail_signals.sh').read_text()
    start = source.index('deliver_worker() {')
    function = source[start:source.index('\n}\n', start)+3]
    signal = tmp_path / '777.signal'; signal.write_text('{"message":{"id":777}}')
    helper = tmp_path / 'headless-deliver'
    helper.write_text(f'#!/bin/bash\nexit {headless_rc}\n'); helper.chmod(0o700)
    tmux_marker = tmp_path / 'tmux'
    released = tmp_path / 'released'
    failure = tmp_path / 'failed'
    script = ('set -euo pipefail\nTMUX_TIMEOUT=5\nHEADLESS_REPLY_TIMEOUT=1\n'
        f'PERSISTENT_DELIVER={shlex.quote(str(helper))}\nSTATE_DIR={shlex.quote(str(tmp_path))}\n' + function +
        '\nrun_to() { shift; "$@"; }\n'
        f'tmux() {{ touch {shlex.quote(str(tmux_marker))}; return 98; }}\n'
        'log() { :; }\n'
        f'state_mark_result() {{ echo "$*" > {shlex.quote(str(failure))}; }}\n'
        f'release_delivery_lease() {{ echo "$*" > {shlex.quote(str(released))}; }}\n'
        f'deliver_worker {shlex.quote(str(signal))} {NAME} 777 Sender subject high 1 body 0 owner\n')
    result = subprocess.run(['/bin/bash', '-c', script], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert not tmux_marker.exists()
    assert released.exists()
    if headless_rc:
        assert 'headless_reply_failed' in failure.read_text()
        assert signal.read_text() == '{"message":{"id":777}}'
    else:
        assert 'persistent-headless-replied' in failure.read_text()
        assert not signal.exists()


def test_running_deck_and_network_keep_unavailable_reason_after_dashboard_restart(monkeypatch):
    monkeypatch.setattr(server, '_tmux_session_env', lambda _name, key: '1' if key == 'AGENTSTACK_MAIL_DISABLED' else 'credential_absent')
    fields = server._claude_row_mail_fields({'name':NAME, 'program':'claude-code', 'running':True}, category='agent')
    assert fields['mail_reason'] == 'credential_absent' and fields['mail_status'] == 'unavailable'
    assert server._claude_row_mail_fields({'name':NAME,'program':'codex','running':True},category='agent') == {}


def test_real_isolated_tmux_reports_conversation_mode_without_per_agent_probes(monkeypatch, tmp_path):
    import hashlib
    import shutil
    tmux = shutil.which('tmux')
    if not tmux: pytest.skip('tmux unavailable')
    socket = 'conversation-mode-' + hashlib.sha256(os.fsencode(tmp_path)).hexdigest()[:12]
    def command(*args):
        return subprocess.run([tmux, '-L', socket, *args], capture_output=True, text=True, timeout=8)
    try:
        created = command('new-session', '-d', '-s', NAME, '-e', 'AGENTSTACK_MAIL_DISABLED=1',
                          '-e', 'AGENTSTACK_MAIL_DISABLED_REASON=credential_absent', '/bin/sleep 30')
        assert created.returncode == 0, created.stderr
        monkeypatch.setattr(server, '_tmux', lambda args: command(*args).stdout)
        monkeypatch.setattr(server, '_tmux_session_env', lambda *_a: pytest.fail('Per-agent tmux probe'))
        state = server.tmux_state()[NAME]
        assert state['mail_disabled'] == '1' and state['mail_disabled_reason'] == 'credential_absent'
        row = {'name':NAME,'program':'claude-code','running':True}
        assert server._claude_row_mail_fields(row, category='agent', session_state=state)['mail_status'] == 'unavailable'
    finally:
        command('kill-server')


def test_jump_missing_credential_resumes_in_one_request_and_reports_mail_status(resume, monkeypatch):
    runtime, _, launches, calls = resume
    (runtime / f'agent_token_{NAME}').unlink()
    monkeypatch.setattr(server, '_has_session', lambda _name: False)
    result = server.do_jump(NAME, open_terminal=False)
    assert result['ok'] and result['action'] == 'resumed'
    assert result['mail_reason'] == 'credential_absent' and launches and not calls


def test_conversation_startup_failure_keeps_material_and_mail_untouched(legacy, monkeypatch):
    runtime, _, launches, calls, *_ = legacy
    before = snapshot(runtime)
    monkeypatch.setattr(server, '_mcp_tool_parameters', lambda _tool: {'name'})
    prepared = []
    def launch(*args, **kwargs):
        prepared.append(kwargs['replace_husk'])
        return {'ok':False,'error':'fixture failure','rollback_errors':['fixture husk is intact']}
    monkeypatch.setattr(server, '_launch_claude_resume_tmux', launch)
    result = server.do_resume(NAME, open_terminal=False, replace_husk=True)
    assert not result['ok'] and prepared == [True] and not calls
    assert result['mail_status'] == 'unavailable' and result['rollback_errors']
    same_material(before)
