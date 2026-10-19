"""Small writers for the common JSONL boundary. Never serialize request bodies."""
import json
import os
from pathlib import Path
import threading
import traceback

from mail_lifecycle import read, stamp

_lock = threading.Lock()


def callers(state):
    """Source locations only: never stack source text, locals or arguments."""
    source = Path(read(Path(state) / 'run-config.json')['hermes_source']).resolve()
    adapter = Path(__file__).resolve().parent
    rows = []
    for frame in traceback.extract_stack()[:-1]:
        path = Path(frame.filename)
        for root, prefix in ((source, 'hermes/'), (adapter, 'adapter/')):
            if path.is_relative_to(root):
                rows.append({'file': prefix + str(path.relative_to(root)),
                             'line': frame.lineno, 'function': frame.name})
                break
    return rows[-16:]


def append(state, name, row):
    with _lock:
        fd = os.open(Path(state) / name, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            data = (json.dumps(row) + '\n').encode()
            if os.write(fd, data) != len(data):
                raise OSError('Incomplete evidence write')
        finally:
            os.close(fd)


def event(state, phase, **fields):
    attempt, task = read(Path(state) / 'attempt.json'), read(Path(state) / 'task.json')
    append(state, 'events.jsonl', {'attempt_id': attempt['attempt_id'], 'task_id': task['task_id'],
                                 'phase': phase, 'at': stamp(), **fields})


def audit(state, phase, **fields):
    attempt = read(Path(state) / 'attempt.json')
    append(state, 'audit.jsonl', {'attempt_id': attempt['attempt_id'], 'phase': phase, 'at': stamp(), **fields})
