"""Read the bounded text tail of one receipt-selected Hermes TUI session."""
from contextlib import closing
from datetime import datetime, timezone
import json
import sqlite3
import stat
import time

from .storage_origin_contract import B1Error


MAX_ROWS = 600
MAX_CELL = 256 * 1024
MAX_READ = 2 * 1024 * 1024


def _database_identity(path):
    if path.resolve(strict=True) != path:
        raise ValueError('redirected Hermes database')
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise ValueError('invalid Hermes database file')
    for suffix in ('-wal', '-shm', '-journal'):
        sidecar = path.with_name(path.name + suffix)
        try:
            mode = sidecar.lstat().st_mode
        except FileNotFoundError:
            continue
        if not stat.S_ISREG(mode):
            raise ValueError('invalid Hermes database sidecar')
    return info.st_dev, info.st_ino


def _json(raw):
    try:
        return json.loads(raw)
    except (ValueError, RecursionError):
        return None


def _text(raw):
    text = raw.decode('utf-8', errors='replace')
    # Hermes uses this sentinel for structured content such as attachments.
    if text.startswith('\x00json:'):
        value = _json(text[6:])
        if isinstance(value, list):
            return '\n'.join(part['text'] for part in value if isinstance(part, dict)
                             and part.get('type') == 'text' and isinstance(part.get('text'), str))
        return value if isinstance(value, str) else ''
    return text


def _reply_calls(raw):
    calls = _json(raw)
    if not isinstance(calls, list):
        return
    for call in calls:
        if not isinstance(call, dict) or not isinstance(call.get('id'), str):
            continue
        function = call.get('function')
        if not isinstance(function, dict) or function.get('name') != 'telegram_reply':
            continue
        args = function.get('arguments')
        if isinstance(args, str):
            args = _json(args)
        if isinstance(args, dict) and isinstance(args.get('text'), str):
            yield call['id'], args['text']


def _project(rows):
    events, turn, calls = [], [], {}

    def finish_turn():
        assistant_text = {text for role, text, _, reply in turn if role == 'assistant' and not reply}
        for role, text, timestamp, reply in turn:
            if not text.strip() or (reply and text in assistant_text):
                continue
            ts = datetime.fromtimestamp(timestamp, timezone.utc).isoformat()
            events.append(dict(role=role, kind='text', text=text.encode('utf-8', errors='replace').decode(), ts=ts))
        turn.clear()
        calls.clear()

    for role, content, call_id, tool_calls, timestamp in reversed(rows):
        if role == 'user':
            finish_turn()
        if role in {'user', 'assistant'}:
            turn.append((role, _text(content), timestamp, False))
            if tool_calls:
                calls.update(_reply_calls(tool_calls))
        elif call_id in calls:
            text = calls.pop(call_id)
            result = _json(content)
            if isinstance(result, dict) and result.get('ok') is True:
                turn.append(('assistant', text, timestamp, True))
    finish_turn()
    return events


def read_history(path, session_id, limit):
    try:
        database = _database_identity(path)
        # immutable=1 would omit committed messages that still live in the WAL.
        with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=0.1)) as con:
            con.execute('PRAGMA query_only=ON')
            con.execute('PRAGMA trusted_schema=OFF')
            deadline = time.monotonic() + 0.5
            con.set_progress_handler(lambda: time.monotonic() >= deadline, 1000)
            con.execute('BEGIN')
            if con.execute('SELECT 1 FROM sessions WHERE id=? AND source=?', (session_id, 'tui')).fetchone() is None:
                raise B1Error('FRESH_NATIVE_SESSION_MISMATCH')
            # Hermes orders messages by insertion ID even when clocks regress.
            cursor = con.execute('''
                SELECT role, CASE WHEN typeof(content) = 'text' THEN substr(CAST(content AS BLOB), 1, ?) END,
                       substr(tool_call_id, 1, 512),
                       CASE WHEN role = 'assistant' AND instr(tool_calls, '"telegram_reply"') > 0
                            THEN substr(CAST(tool_calls AS BLOB), 1, ?) END, timestamp
                FROM messages WHERE session_id = ? AND
                    (role IN ('user', 'assistant') OR (role = 'tool' AND tool_name = 'telegram_reply'))
                ORDER BY id DESC LIMIT ?
            ''', (MAX_CELL + 1, MAX_CELL + 1, session_id, MAX_ROWS + 1))
            rows, remaining, more = [], MAX_READ, False
            for role, content, call_id, tool_calls, ts in cursor:
                content, tool_calls = content or b'', tool_calls or b''
                remaining -= len(content) + len(tool_calls)
                if len(rows) == MAX_ROWS or remaining < 0:
                    more = True
                    break
                more |= max(len(content), len(tool_calls)) > MAX_CELL
                rows.append((role, content, call_id, tool_calls, ts))
        if _database_identity(path) != database:
            raise B1Error('FRESH_NATIVE_SESSION_MISMATCH')
        events = _project(rows)
        return events[-limit:], more or len(events) > limit
    except (sqlite3.Error, OSError, ValueError, TypeError, OverflowError):
        raise B1Error('FRESH_NATIVE_SESSION_MISMATCH') from None
