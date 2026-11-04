"""Exact legacy Hermes history binding; never read message bodies or run Hermes.

The callback's native ID selects one SQLite session in a child-owned HOME.
There is intentionally no directory scan, MRU, cwd, or timestamp fallback.
"""
from contextlib import closing
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat


REASONS = {
    'registration_missing': 'Hermes registration is unavailable.',
    'invalid_binding': 'Hermes launch identity or HOME could not be verified.',
    'launch_transition': 'Hermes launch or resume is still being prepared.',
    'native_session_missing': 'Hermes has not recorded an exact native session.',
    'history_unavailable': 'The recorded Hermes SQLite session is unavailable.',
    'receipt_predates_session': 'The Hermes session receipt predates this terminal launch.',
    'fresh_history_provider_unavailable': 'Fresh Hermes history is unavailable through this legacy binding.',
}


def unbound(code):
    return dict(history_binding='unbound', history_binding_reason_code=code,
                history_binding_reason=REASONS[code], transcript_path=None,
                history_session_id=None, history_receipt_ts=None)


def _open_regular(path):
    """Reject symlinks in every component, including an ancestor directory."""
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts:
        raise ValueError('invalid path')
    parent = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for component in path.parts[1:-1]:
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            os.close(parent)
            parent = child
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            raise ValueError('not regular')
        return fd
    finally:
        os.close(parent)


def _identity(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_mode, info.st_uid


def _json(path, snapshots):
    fd = _open_regular(path)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_size > 262144:
            raise ValueError('not private metadata')
        value = json.loads(stream.read(262145))
        if not isinstance(value, dict) or _identity(info) != _identity(os.fstat(stream.fileno())):
            raise ValueError('metadata changed')
        snapshots[path] = _identity(info)
        return value


def _unchanged(snapshots):
    for path, expected in snapshots.items():
        fd = _open_regular(path)
        try:
            if _identity(os.fstat(fd)) != expected:
                return False
        finally:
            os.close(fd)
    return True


def history_binding(runtime_dir, install_home, name, registration, *, session_created=None):
    if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]*', name):
        return unbound('invalid_binding')
    if not isinstance(registration, dict):
        return unbound('registration_missing')
    snapshots = {}
    try:
        runtime = Path(runtime_dir).resolve()
        children = runtime / 'child-agents'
        state = _json(children / (name + '.json'), snapshots)
        identity = dict(agent_name=name, program='hermes', project_key=registration.get('project_key'),
                        agent_id=registration.get('agent_id'))
        if (not isinstance(identity['project_key'], str) or not identity['project_key']
                or type(identity['agent_id']) is not int or identity['agent_id'] <= 0
                or any(registration.get(k) != v or state.get(k) != v for k, v in identity.items())
                or state.get('harness') != 'hermes'):
            return unbound('invalid_binding')
        if (state.get('resume_in_progress_at') or state.get('tools_change_generation')
                or os.path.lexists(children / ('.' + name + '.registration-pending.json'))
                or os.path.lexists(children / ('.' + name + '.legacy-migration.json'))):
            return unbound('launch_transition')
        owned = children / (name + '.harness')
        directory = Path(state['harness_state_dir'])
        if directory != owned and (directory.parent != owned or not re.fullmatch(r'[a-f0-9]{32}', directory.name)):
            return unbound('invalid_binding')
        source = _json(directory / 'input.json', snapshots)
        plan = _json(directory / 'launch.json', snapshots)
        if (any(source.get(k) != identity[k] for k in ('agent_name', 'program', 'project_key'))
                or source.get('runtime_dir') != str(runtime) or source.get('state_dir') != str(directory)
                or source.get('backend_id') != state.get('harness_backend')
                or Path(source['product_root']).resolve() != Path(install_home).resolve()
                or source.get('fresh_client_dir') is not None
                or plan.get('program') != 'hermes' or plan.get('cwd') != source.get('cwd')):
            return unbound('invalid_binding')
        # Installed generation/profiles may redirect to the persistent root;
        # the child namespace and everything below it may not redirect.
        profiles = (Path(install_home) / 'harnesses/hermes/profiles').resolve()
        home = profiles / 'children' / hashlib.sha256(identity['project_key'].encode()).hexdigest() / name
        if (source.get('native_profile_env') != {'HERMES_HOME': str(home)}
                or plan.get('profile_home') != str(home)
                or plan.get('env', {}).get('HERMES_HOME') != str(home)
                or plan.get('env', {}).get('AGENTSTACK_NATIVE_SESSION_FILE') != str(directory / 'native-session.json')):
            return unbound('invalid_binding')
        try:
            event = _json(directory / 'native-session.json', snapshots)
        except (OSError, ValueError):
            return unbound('native_session_missing')
        sid = event.get('native_session_id')
        if (event.get('harness') != 'hermes' or event.get('agent_name', name) != name
                or not isinstance(sid, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,255}', sid)
                or source.get('resume_session_id', sid) != sid):
            return unbound('invalid_binding')
        # This timestamp validates callback shape, never selects a session.
        receipt_time = datetime.fromisoformat(event['created_at'].replace('Z', '+00:00'))
        if receipt_time.tzinfo is None:
            return unbound('invalid_binding')
        database = home / 'state.db'
        fd = _open_regular(database)
        try:
            pinned = os.fstat(fd)
            # mode=ro sees committed WAL rows; immutable=1 would miss them.
            # No provider import: opening Hermes SessionDB can run migrations.
            with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True, timeout=0.1)) as con:
                con.execute('PRAGMA query_only=ON')
                con.set_progress_handler(lambda: 1, 10000)
                row = con.execute('SELECT id FROM sessions WHERE id=? AND source=?', (sid, 'cli')).fetchone()
                # Fail closed for unsupported schema without fetching bodies.
                con.execute('SELECT id, session_id, role, content, timestamp, tool_calls, tool_call_id, tool_name '
                            'FROM messages LIMIT 0')
            current = _open_regular(database)
            try:
                now = os.fstat(current)
                same_database = (pinned.st_dev, pinned.st_ino) == (now.st_dev, now.st_ino)
            finally:
                os.close(current)
            if row is None or not same_database or not _unchanged(snapshots):
                return unbound('history_unavailable')
        finally:
            os.close(fd)
        evidence = dict(history_session_id=sid, history_receipt_ts=event['created_at'])
        # An exact, explicit resume may legitimately reuse an earlier receipt.
        # A new terminal launch without that intent must not inherit old proof.
        if (session_created and not source.get('resume_session_id')
                and receipt_time.timestamp() < float(session_created)):
            return dict(unbound('receipt_predates_session'), history_binding='stale', **evidence)
        return dict(history_binding='bound', history_binding_reason='', history_binding_reason_code='bound',
                    transcript_path=str(database), **evidence)
    except sqlite3.Error:
        return unbound('history_unavailable')
    except (OSError, ValueError, KeyError, TypeError, AttributeError, OverflowError):
        return unbound('invalid_binding')
