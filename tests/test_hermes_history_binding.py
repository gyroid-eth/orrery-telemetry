"""Hermes history uses a callback ID and an owned HOME, never cwd or MRU."""
import hashlib
import json
import sqlite3

import pytest

from dashboard import server


NAME = 'HistoryHermes'
SID = '20261104_060800_aabbcc'


def write(path, value):
    path.write_text(json.dumps(value))
    path.chmod(0o600)


@pytest.fixture
def binding(tmp_path, monkeypatch):
    root = tmp_path / 'product'
    runtime = root / 'runtime'
    project = str(tmp_path / 'project')
    directory = runtime / 'child-agents' / (NAME + '.harness') / ('a' * 32)
    directory.mkdir(parents=True)
    home = root / 'harnesses/hermes/profiles/children' / hashlib.sha256(project.encode()).hexdigest() / NAME
    home.mkdir(parents=True)
    registration = dict(agent_id=42, agent_name=NAME, program='hermes', project_key=project)
    state = dict(registration, harness='hermes', harness_backend='strata',
                 harness_state_dir=str(directory))
    source = dict(agent_name=NAME, program='hermes', project_key=project,
                  backend_id='strata', state_dir=str(directory), runtime_dir=str(runtime),
                  product_root=str(root), cwd=project, native_profile_env={'HERMES_HOME': str(home)})
    plan = dict(program='hermes', cwd=project, profile_home=str(home), env={
        'HERMES_HOME': str(home), 'AGENTSTACK_NATIVE_SESSION_FILE': str(directory / 'native-session.json')})
    event = dict(harness='hermes', native_session_id=SID, created_at='2026-11-04T06:08:00+00:00')
    state_file = runtime / 'child-agents' / (NAME + '.json')
    for path, value in [(state_file, state), (directory / 'input.json', source),
                        (directory / 'launch.json', plan), (directory / 'native-session.json', event)]:
        write(path, value)
    with sqlite3.connect(home / 'state.db') as con:
        con.executescript('CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT);'
                          'CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, '
                          'content TEXT, timestamp REAL, tool_calls TEXT, tool_call_id TEXT, tool_name TEXT);')
        con.executemany('INSERT INTO sessions VALUES (?, ?)', [(SID, 'cli'), ('other-same-cwd', 'cli')])
    monkeypatch.setattr(server, 'RUNTIME_DIR', str(runtime))
    monkeypatch.setattr(server, 'HOOKS_DIR', str(root / 'hooks'))
    monkeypatch.setattr(server, 'FRESH_MODE', False)
    monkeypatch.setattr(server, '_resume_registration', lambda name, programs: registration)
    return dict(root=root, runtime=runtime, home=home, directory=directory, state=state,
                state_file=state_file, source=source, plan=plan, event=event, registration=registration)


def resolve():
    return server._hermes_history_binding(NAME)


def assert_unbound(result, code=None):
    assert result['history_binding'] == 'unbound'
    assert result['history_binding_reason']
    if code:
        assert result['history_binding_reason_code'] == code
    assert result['transcript_path'] is None
    assert result['history_session_id'] is None
    assert result['history_receipt_ts'] is None


def test_exact_callback_session_is_bound_even_with_another_session(binding):
    result = resolve()
    assert result['history_binding'] == 'bound'
    assert result['transcript_path'] == str(binding['home'] / 'state.db')
    assert result['history_session_id'] == SID
    assert result['history_receipt_ts'] == binding['event']['created_at']
    assert 'transcript_format' not in result


@pytest.mark.parametrize('missing', ['native-session.json', 'input.json', 'launch.json'])
def test_missing_proof_never_chooses_another_session(binding, missing):
    (binding['directory'] / missing).unlink()
    assert_unbound(resolve())


@pytest.mark.parametrize('target', ['registration', 'state', 'source', 'event', 'plan'])
def test_foreign_identity_is_rejected(binding, target):
    record = binding[target]
    if target == 'registration':
        record['agent_id'] = 99
    elif target in ('state', 'source'):
        record['project_key'] = '/foreign/project'
    elif target == 'event':
        record['native_session_id'] = 'not-in-owned-db'
    else:
        record['env']['HERMES_HOME'] = str(binding['root'] / 'foreign')
    path = {'state': binding['state_file'], 'source': binding['directory'] / 'input.json',
            'event': binding['directory'] / 'native-session.json',
            'plan': binding['directory'] / 'launch.json'}.get(target)
    if path:
        write(path, record)
    assert_unbound(resolve())


def test_resume_checks_exact_id_and_blocks_incomplete_transition(binding):
    binding['source']['resume_session_id'] = SID
    write(binding['directory'] / 'input.json', binding['source'])
    assert resolve()['history_binding'] == 'bound'
    binding['source']['resume_session_id'] = 'other-same-cwd'
    write(binding['directory'] / 'input.json', binding['source'])
    assert_unbound(resolve())
    binding['state']['resume_in_progress_at'] = '2026-11-04T06:10:00+00:00'
    write(binding['state_file'], binding['state'])
    assert_unbound(resolve(), 'launch_transition')


def test_retirement_preserves_history_but_removing_state_unbinds(binding):
    binding['state'].update(retired_at='2026-11-04T06:20:00+00:00', status='retired')
    write(binding['state_file'], binding['state'])
    assert resolve()['history_binding'] == 'bound'
    binding['state_file'].unlink()
    assert_unbound(resolve())


@pytest.mark.parametrize('kind', ['db', 'directory', 'metadata'])
def test_symlink_does_not_grant_history(binding, kind):
    path = {'db': binding['home'] / 'state.db', 'directory': binding['directory'],
            'metadata': binding['directory'] / 'native-session.json'}[kind]
    moved = path.with_name(path.name + '-moved')
    path.rename(moved)
    path.symlink_to(moved, target_is_directory=kind == 'directory')
    assert_unbound(resolve())


def test_missing_session_table_and_non_cli_session_are_unbound(binding):
    db = binding['home'] / 'state.db'
    with sqlite3.connect(db) as con:
        con.execute('UPDATE sessions SET source=? WHERE id=?', ('delegate', SID))
    assert_unbound(resolve())
    with sqlite3.connect(db) as con:
        con.execute('DROP TABLE sessions')
    assert_unbound(resolve())


def test_wal_commits_are_visible_and_reads_do_not_change_database(binding):
    db = binding['home'] / 'state.db'
    with sqlite3.connect(db) as con:
        con.execute('PRAGMA journal_mode=WAL')
        con.execute('UPDATE sessions SET id=? WHERE id=?', ('rotated-session', SID))
        con.commit()
        binding['event']['native_session_id'] = 'rotated-session'
        write(binding['directory'] / 'native-session.json', binding['event'])
        before = db.read_bytes()
        assert resolve()['history_session_id'] == 'rotated-session'
        assert db.read_bytes() == before


def test_fresh_never_resolves_legacy_metadata(binding, monkeypatch):
    monkeypatch.setattr(server, 'FRESH_MODE', True)
    monkeypatch.setattr(server, '_resume_registration', lambda *args: pytest.fail('legacy lookup'))
    assert_unbound(resolve(), 'fresh_history_provider_unavailable')


def test_agents_row_exposes_hermes_binding(binding, monkeypatch):
    name = NAME
    monkeypatch.setattr(server, 'tmux_state', lambda: {name: dict(name=name, created=100,
        session_id='$1', activity=200, attached=False, client_tty=None, cmd='hermes', pane_pid=10, title='')})
    mail = dict(model='fixture', model_raw='fixture', program='hermes', task='', last_active=150)
    monkeypatch.setattr(server, 'agentmail_state', lambda: ({name: mail}, {}))
    monkeypatch.setattr(server, '_process_tree_snapshot', lambda: ({}, {}))
    monkeypatch.setattr(server, '_tmux_row_state', lambda *args: ('hermes', 'agent', True, None))
    for function in ('_codex_app_runtimes', '_deliverables_index', '_name_substitutions',
                     '_annotations', '_persistent_profiles', '_native_context_runtime'):
        monkeypatch.setattr(server, function, lambda *a: {})
    monkeypatch.setattr(server, '_retired_names', lambda *a: set())
    monkeypatch.setattr(server, '_project_key', lambda: '')
    monkeypatch.setattr(server, '_resume_capability_for_row', lambda *a, **k: 'not_required')
    monkeypatch.setattr(server, '_provider_stall_view', lambda *a: None)
    row = server.build_agents()[0]
    assert row['history_binding'] == 'bound'
    assert row['history_session_id'] == SID


def test_old_receipt_is_stale_for_new_launch_but_valid_for_exact_resume(binding):
    result = server._hermes_history_binding(NAME, session_created=9999999999)
    assert result['history_binding'] == 'stale'
    assert result['history_binding_reason_code'] == 'receipt_predates_session'
    assert result['history_session_id'] == SID
    assert result['transcript_path'] is None
    binding['source']['resume_session_id'] = SID
    write(binding['directory'] / 'input.json', binding['source'])
    assert server._hermes_history_binding(NAME, session_created=9999999999)['history_binding'] == 'bound'


@pytest.mark.parametrize('marker', ['registration-pending.json', 'legacy-migration.json'])
def test_pending_registration_or_migration_never_reads_old_receipt(binding, marker):
    write(binding['runtime'] / 'child-agents' / ('.' + NAME + '.' + marker), {})
    assert_unbound(resolve(), 'launch_transition')


def test_registration_absent_and_foreign_generation_are_unbound(binding, monkeypatch):
    directory = binding['directory'].parent.parent / 'OtherChild.harness' / ('a' * 32)
    binding['state']['harness_state_dir'] = str(directory)
    write(binding['state_file'], binding['state'])
    assert_unbound(resolve())
    monkeypatch.setattr(server, '_resume_registration', lambda *a: None)
    assert_unbound(resolve(), 'registration_missing')


def test_metadata_changed_during_db_check_is_not_published(binding, monkeypatch):
    from dashboard import hermes_history
    original = hermes_history.sqlite3.connect
    def connect(*args, **kwargs):
        binding['event']['native_session_id'] = 'other-same-cwd'
        write(binding['directory'] / 'native-session.json', binding['event'])
        return original(*args, **kwargs)
    monkeypatch.setattr(hermes_history.sqlite3, 'connect', connect)
    assert_unbound(resolve(), 'history_unavailable')


def test_database_replaced_during_check_is_not_published(binding, monkeypatch):
    from dashboard import hermes_history
    original = hermes_history.sqlite3.connect
    def connect(*args, **kwargs):
        database = binding['home'] / 'state.db'
        contents = database.read_bytes()
        database.rename(database.with_name('old.db'))
        database.write_bytes(contents)
        return original(*args, **kwargs)
    monkeypatch.setattr(hermes_history.sqlite3, 'connect', connect)
    assert_unbound(resolve(), 'history_unavailable')


@pytest.mark.parametrize('corruption', ['permissions', 'malformed', 'traversal_id', 'shared_home', 'foreign_name'])
def test_invalid_metadata_is_unbound(binding, corruption):
    path = binding['directory'] / 'native-session.json'
    if corruption == 'permissions':
        path.chmod(0o644)
    elif corruption == 'malformed':
        path.write_text('[]')
    elif corruption == 'shared_home':
        binding['source']['native_profile_env']['HERMES_HOME'] = str(binding['root'] / 'shared')
        write(binding['directory'] / 'input.json', binding['source'])
    else:
        binding['event'].update({'native_session_id': '../other'} if corruption == 'traversal_id'
                                else {'agent_name': 'OtherChild'})
        write(path, binding['event'])
    assert_unbound(resolve())
