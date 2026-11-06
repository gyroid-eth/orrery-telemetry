"""Read non-secret, provider-issued legacy dsh history receipts only.

The provider supplies its own location and descriptor identity after flushing.
This module never scans sessions, reads credentials or decompresses messages.
"""
import json
import os
from pathlib import Path
import re
import stat
from datetime import datetime


def unbound(reason):
    return {'history_binding': 'unbound', 'history_binding_reason': reason.replace('_', ' '),
            'history_binding_reason_code': reason, 'transcript_path': None,
            'history_session_id': None, 'history_receipt_ts': None}


def _path(value):
    if not isinstance(value, str) or not value.startswith('/') or '\0' in value:
        raise ValueError('invalid_path')
    path = Path(value)
    if '..' in path.parts or str(path) != value:
        raise ValueError('invalid_path')
    return path


def _read(path, *, metadata=True):
    """No-follow component walk, bounded metadata read, pinned final descriptor."""
    path = _path(str(path))
    directory = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    descriptor = None
    try:
        for part in path.parts[1:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        descriptor = os.open(path.name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=directory)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise ValueError('invalid_file')
        if not metadata:
            return info
        if info.st_size > 65536 or info.st_mode & 0o022:
            raise ValueError('invalid_receipt')
        content = os.read(descriptor, 65537)
        if len(content) > 65536:
            raise ValueError('invalid_receipt')
        value = json.loads(content)
        if not isinstance(value, dict):
            raise ValueError('invalid_receipt')
        return value
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(directory)


def binding(runtime_dir, name, registration, *, session_created=None):
    if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9_-]+', name):
        return unbound('identity_mismatch')
    if not registration or registration.get('program') != 'dsh':
        return unbound('registration_missing')
    try:
        runtime = _path(str(runtime_dir))
        index = runtime / 'dsh-history' / (name + '.json')
        launch = _read(index)
        identity = dict(schema_version=1, provider='dsh', program='dsh', agent_name=name,
                        agent_id=registration['agent_id'], project_key=registration['project_key'])
        if any(launch.get(k) != v for k, v in identity.items()):
            return unbound('identity_mismatch')
        if type(launch.get('agent_id')) is not int or type(launch.get('schema_version')) is not int:
            return unbound('identity_mismatch')
        if not re.fullmatch(r'[a-f0-9]{32}', launch.get('launch_id', '')):
            return unbound('launch_mismatch')
        state = _path(launch['state_dir'])
        owned = runtime / 'child-agents' / (name + '.harness')
        if state.parent != owned or not re.fullmatch(r'[a-f0-9]{32}', state.name):
            return unbound('identity_mismatch')
        home = runtime / 'native/dsh' / name
        if launch.get('native_home') != str(home):
            return unbound('identity_mismatch')
        receipt = state / 'dsh-history.json'
        for suffix in ('lock', 'conflict'):
            try:
                (state / ('dsh-history-' + launch['launch_id'] + '.' + suffix)).lstat()
            except FileNotFoundError:
                continue
            return unbound('session_conflict')
        claim = _read(receipt)
        if any(claim.get(k) != v for k, v in launch.items()):
            return unbound('launch_mismatch')
        if claim.get('conflicted') is not False:
            return unbound('session_conflict')
        timestamp = claim.get('created_at')
        receipt_time = datetime.fromisoformat(timestamp.replace('Z', '+00:00')) if isinstance(timestamp, str) else None
        if receipt_time is None or receipt_time.tzinfo is None:
            return unbound('invalid_receipt')
        session_id = claim.get('session_id')
        if not isinstance(session_id, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,255}', session_id):
            return unbound('session_mismatch')
        path = _path(claim['transcript_path'])
        # The location comes from the provider, never our workspace-key guess.
        # Constrain it to this child's home and exactly this native ID.
        if (path.parents[2] != home / 'sessions' or path.parent.name != session_id
                or not re.fullmatch(r'session(?:\.v[1-9][0-9]*)?\.jsonl(?:\.zstd)?', path.name)):
            return unbound('session_mismatch')
        info = _read(path, metadata=False)
        if (type(claim.get('device')) is not int or type(claim.get('inode')) is not int
                or (info.st_dev, info.st_ino) != (claim['device'], claim['inode']) or not info.st_size):
            return unbound('artifact_changed')
        if _read(index) != launch or _read(receipt) != claim:
            return unbound('launch_mismatch')
        evidence = dict(history_session_id=session_id, history_receipt_ts=timestamp)
        if session_created and receipt_time.timestamp() < float(session_created):
            return dict(unbound('receipt_predates_session'), history_binding='stale', **evidence)
        return {'history_binding': 'bound', 'history_binding_reason': '',
                'history_binding_reason_code': 'bound', 'transcript_path': str(path),
                **evidence}
    except (OSError, ValueError, KeyError, TypeError, IndexError):
        return unbound('receipt_unavailable')
