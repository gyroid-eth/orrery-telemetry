"""Synthetic provider-issued receipts; never inspect installed conversations."""
import json
from pathlib import Path
import sqlite3
import shutil
import subprocess
from datetime import datetime

import pytest

from dashboard import server


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))
    path.chmod(0o600)


@pytest.fixture
def binding(tmp_path, monkeypatch):
    runtime = tmp_path / 'runtime'
    name = 'OwnDsh'
    state = runtime / 'child-agents' / (name + '.harness') / ('a' * 32)
    state.mkdir(parents=True)
    home = runtime / 'native/dsh' / name
    transcript = home / 'sessions/project/session-own/session.v4.jsonl.zstd'
    transcript.parent.mkdir(parents=True)
    transcript.write_bytes(b'synthetic compressed provider artifact')
    stat = transcript.stat()
    launch = dict(schema_version=1, provider='dsh', program='dsh', agent_name=name,
                  agent_id=7, project_key='/project', launch_id='a' * 32,
                  state_dir=str(state), native_home=str(home))
    claim = dict(launch, session_id='session-own', transcript_path=str(transcript),
                 device=stat.st_dev, inode=stat.st_ino, conflicted=False, created_at='2026-11-04T06:00:00.000Z')
    index = runtime / 'dsh-history' / (name + '.json')
    receipt = state / 'dsh-history.json'
    write(index, launch)
    write(receipt, claim)
    db = tmp_path / 'mail.db'
    with sqlite3.connect(db) as con:
        con.executescript('CREATE TABLE projects(id INTEGER, human_key TEXT);'
                         'CREATE TABLE agents(id INTEGER, project_id INTEGER, name TEXT, '
                         'program TEXT, retired_at TEXT, model TEXT, task_description TEXT, last_active_ts TEXT);')
        con.execute('INSERT INTO projects VALUES(1, ?)', ('/project',))
        con.execute("INSERT INTO agents VALUES(7, 1, ?, ?, NULL, '', '', '2026-11-04')", (name, 'dsh'))
    monkeypatch.setattr(server, 'RUNTIME_DIR', str(runtime))
    monkeypatch.setattr(server, 'DB_PATH', str(db))
    monkeypatch.setattr(server, '_project_key', lambda: '/project')
    return dict(name=name, index=index, receipt=receipt, launch=launch, claim=claim,
                transcript=transcript, db=db, runtime=runtime)


def resolve(f, **kwargs):
    return server._dsh_history_binding(f['name'], **kwargs)


def unbound(f, reason=None):
    result = resolve(f)
    assert result['history_binding'] == 'unbound'
    assert result.get('transcript_path') is None
    assert result['history_binding_reason']
    if reason:
        assert result['history_binding_reason_code'] == reason


def test_bound_exact_provider_path(binding, monkeypatch):
    monkeypatch.setattr(server.os, 'walk', lambda *_: pytest.fail('must not scan cwd/history'))
    result = resolve(binding)
    assert result['history_binding'] == 'bound'
    assert result['transcript_path'] == str(binding['transcript'])
    assert result['history_session_id'] == 'session-own'
    assert result['history_receipt_ts'] == '2026-11-04T06:00:00.000Z'


@pytest.mark.parametrize('defect', ['missing_index', 'missing_claim', 'launch', 'agent_id',
    'project_key', 'program', 'conflict', 'other_session', 'outside_home', 'replaced',
    'symlink', 'symlink_parent', 'corrupt', 'oversized', 'state_escape'])
def test_unproven_receipts_do_not_return_a_path(binding, defect):
    f = binding
    if defect == 'missing_index': f['index'].unlink()
    elif defect == 'missing_claim': f['receipt'].unlink()
    elif defect == 'launch':
        write(f['index'], dict(f['launch'], launch_id='b' * 32))
    elif defect in ('agent_id', 'project_key', 'program'):
        value = {'agent_id': 8, 'project_key': '/another-project', 'program': 'hermes'}[defect]
        write(f['index'], dict(f['launch'], **{defect: value}))
    elif defect == 'conflict': write(f['receipt'], dict(f['claim'], conflicted=True))
    elif defect == 'other_session': write(f['receipt'], dict(f['claim'], session_id='session-other'))
    elif defect == 'outside_home':
        other = f['runtime'] / 'outside/session-own/session.v4.jsonl.zstd'
        other.parent.mkdir(parents=True); other.write_bytes(b'other')
        st = other.stat()
        write(f['receipt'], dict(f['claim'], transcript_path=str(other), device=st.st_dev, inode=st.st_ino))
    elif defect == 'replaced':
        other = f['transcript'].with_name('replacement'); other.write_bytes(b'other-session')
        other.replace(f['transcript'])
    elif defect in ('symlink', 'symlink_parent'):
        old = f['transcript'] if defect == 'symlink' else f['transcript'].parent
        moved = old.with_name(old.name + '-moved'); old.rename(moved)
        old.symlink_to(moved, target_is_directory=defect == 'symlink_parent')
    elif defect == 'corrupt': f['receipt'].write_text('{')
    elif defect == 'oversized': f['receipt'].write_text(' ' * 70000)
    elif defect == 'state_escape':
        write(f['index'], dict(f['launch'], state_dir=str(f['transcript'].parent)))
    unbound(f)


def test_same_cwd_another_session_is_never_selected(binding):
    other = binding['transcript'].parent.parent / 'session-other/session.v4.jsonl.zstd'
    other.parent.mkdir(); other.write_bytes(b'newer unrelated session')
    assert resolve(binding)['transcript_path'] == str(binding['transcript'])
    binding['receipt'].unlink()
    unbound(binding)


def test_resume_invalidates_previous_receipt_then_accepts_new_self_claim(binding):
    f = binding
    next_launch = dict(f['launch'], launch_id='c' * 32)
    write(f['index'], next_launch)
    unbound(f)
    other = f['transcript'].parent.parent / 'session-resumed/session.v4.jsonl.zstd'
    other.parent.mkdir(); other.write_bytes(b'resumed provider artifact')
    st = other.stat()
    write(f['receipt'], dict(next_launch, session_id='session-resumed', transcript_path=str(other),
                            device=st.st_dev, inode=st.st_ino, conflicted=False, created_at=f['claim']['created_at']))
    assert resolve(f)['transcript_path'] == str(other)


@pytest.mark.parametrize('change', ['reregister', 'project', 'provider', 'remove'])
def test_current_mail_identity_is_required(binding, change):
    with sqlite3.connect(binding['db']) as con:
        query = {'retire': "UPDATE agents SET retired_at='2026-11-04'",
                 'reregister': 'UPDATE agents SET id=8',
                 'project': "UPDATE projects SET human_key='/other'",
                 'provider': "UPDATE agents SET program='hermes'", 'remove': 'DELETE FROM agents'}[change]
        con.execute(query)
    unbound(binding)


def test_retired_archive_keeps_exact_binding_until_receipt_cleanup(binding):
    with sqlite3.connect(binding['db']) as con:
        con.execute("UPDATE agents SET retired_at='2026-11-04'")
    assert resolve(binding)['history_binding'] == 'bound'
    binding['receipt'].unlink()
    unbound(binding)


def test_fresh_does_not_read_legacy_receipts(binding, monkeypatch):
    monkeypatch.setattr(server, 'FRESH_MODE', True)
    unbound(binding, 'protected_history_required')


def test_native_callback_receipts():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node required for native dsh callback')
    result = subprocess.run([node, '--test', str(Path(__file__).with_suffix('.mjs'))],
                            text=True, capture_output=True, timeout=45)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize('suffix', ['lock', 'conflict'])
def test_incomplete_or_competing_callback_is_unbound(binding, suffix):
    (binding['receipt'].parent / ('dsh-history-' + binding['launch']['launch_id'] + '.' + suffix)).touch()
    unbound(binding, 'session_conflict')


def test_launch_receipt_issuance_is_nonsecret_and_rotates_on_resume(tmp_path, monkeypatch):
    from hooks import harness_child
    monkeypatch.delenv('AGENTSTACK_RUNTIME_MODE', raising=False)
    monkeypatch.delenv('AGENTSTACK_FRESH_CLIENT_CONTEXT', raising=False)
    document = dict(program='dsh', agent_name='OwnDsh', project_key='/project',
                    runtime_dir=str(tmp_path), state_dir=str(tmp_path / 'state'))
    harness_child.prepare_dsh_history(document, {'agent_id':7, 'registration_token':'synthetic-secret'})
    first = document['history_binding'].copy()
    assert 'synthetic-secret' not in json.dumps(first)
    harness_child.prepare_dsh_history(document, {'agent_id':7})
    assert first['launch_id'] != document['history_binding']['launch_id']
    index = tmp_path / 'dsh-history/OwnDsh.json'
    assert json.loads(index.read_text()) == document['history_binding']
    retained = index.read_bytes()
    for change in ({'fresh_client_dir':str(tmp_path)}, {'program':'hermes'}):
        altered = dict(document, **change)
        harness_child.prepare_dsh_history(altered, {'agent_id':7})
        assert 'history_binding' not in altered
        assert index.read_bytes() == retained


@pytest.mark.parametrize('created,expected', [(100, 'bound'),
    (datetime.fromisoformat('2026-11-05T00:00:00+00:00').timestamp(), 'stale')])
def test_agents_roster_exports_the_verified_dsh_binding(binding, monkeypatch, created, expected):
    name = binding['name']
    session = dict(name=name, created=created, session_id='$fixture', activity=200,
                   attached=False, client_tty=None, cmd='node', pane_pid=10, title='fixture')
    registration = dict(model='fixture', model_raw='fixture', program='dsh',
                        task='fixture', last_active=150)
    monkeypatch.setattr(server, 'tmux_state', lambda: {name:session})
    monkeypatch.setattr(server, 'agentmail_state', lambda: ({name:registration}, {}))
    monkeypatch.setattr(server, '_process_tree_snapshot', lambda: ({10:'node'}, {}))
    monkeypatch.setattr(server, '_agent_process_alive', lambda *args: True)
    for function in ('_codex_app_runtimes', '_deliverables_index', '_name_substitutions',
                     '_annotations', '_persistent_profiles'):
        monkeypatch.setattr(server, function, lambda: {})
    monkeypatch.setattr(server, '_retired_names', lambda *args:set())
    monkeypatch.setattr(server, '_resume_capability_for_row', lambda *args, **kwargs:'not_required')
    monkeypatch.setattr(server, '_provider_stall_view', lambda *args:None)
    monkeypatch.setattr(server, '_native_context_runtime', lambda *args: {'ctx_used':None, 'ctx_window':None})
    row = server.build_agents()[0]
    assert row['program'] == 'dsh'
    assert row['history_binding'] == expected
    assert row['transcript_path'] == (str(binding['transcript']) if expected == 'bound' else None)
    assert row['history_session_id'] == 'session-own'
    binding['receipt'].unlink()
    assert server.build_agents()[0]['history_binding'] == 'unbound'


def test_new_terminal_stale_archive_and_formal_resume_nonce_rotation(binding):
    created=datetime.fromisoformat('2026-11-05T00:00:00+00:00').timestamp()
    stale=resolve(binding, session_created=created)
    assert stale['history_binding'] == 'stale'
    assert stale['history_binding_reason_code'] == 'receipt_predates_session'
    assert stale['transcript_path'] is None
    assert stale['history_session_id'] == 'session-own'
    assert stale['history_receipt_ts'] == binding['claim']['created_at']
    # A terminal-free retired archive still has exact usable proof.
    assert resolve(binding)['history_binding'] == 'bound'
    next_launch=dict(binding['launch'], launch_id='d'*32)
    write(binding['index'], next_launch)
    assert resolve(binding, session_created=created)['history_binding'] == 'unbound'
    # Only the new launch's own callback can bind this formally resumed run.
    claim=dict(binding['claim'], **next_launch, created_at='2026-11-05T00:00:01.000Z')
    write(binding['receipt'], claim)
    assert resolve(binding, session_created=created)['history_binding'] == 'bound'
