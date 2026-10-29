"""Bounded read-only queries against an explicitly named, finalized Mail copy.

Create the consistent backup after stopping old writers, before using this
command. No live database discovery, registration, signals, or attachment reads.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import stat
import time
from contextlib import contextmanager

MAX_RESULTS = 100
MAX_BODY = 8192
MAX_ATTACHMENT_JSON = 262144
MAX_OUTPUT = 512 * 1024


class ArchiveError(ValueError):
    pass


@contextmanager
def archive_connection(path):
    path = Path(path)
    try:
        if not path.is_absolute() or not stat.S_ISREG(path.lstat().st_mode):
            raise ArchiveError('ARCHIVE_COPY_REQUIRED')
        path = path.resolve(strict=True)
        # Immutable mode is valid only for a finalized copy, never pending WAL.
        for suffix in ('-wal', '-journal'):
            sidecar = Path(str(path) + suffix)
            if sidecar.exists() and sidecar.stat().st_size:
                raise ArchiveError('ARCHIVE_COPY_NOT_FINALIZED')
        database = sqlite3.connect(path.as_uri() + '?mode=ro&immutable=1', uri=True)
    except OSError:
        raise ArchiveError('ARCHIVE_COPY_UNAVAILABLE') from None
    try:
        database.row_factory = sqlite3.Row
        database.execute('PRAGMA query_only=ON')
        database.execute('PRAGMA trusted_schema=OFF')
        deadline = time.monotonic() + 5
        database.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
        yield database
    except sqlite3.Error:
        raise ArchiveError('ARCHIVE_QUERY_FAILED') from None
    finally:
        database.close()


def _attachment_paths(raw):
    if len(raw) > MAX_ATTACHMENT_JSON:
        return [], True
    try:
        values = json.loads(raw)
        if not isinstance(values, list):
            raise ValueError
    except (TypeError, ValueError):
        raise ArchiveError('ARCHIVE_ATTACHMENT_INVALID') from None
    result = []
    truncated = len(values) > 16
    for item in values[:16]:
        if not isinstance(item, dict):
            raise ArchiveError('ARCHIVE_ATTACHMENT_INVALID')
        # Never emit inline data_base64, arbitrary metadata, or open these paths.
        view = {}
        for key in ('type', 'media_type', 'path', 'original_path'):
            if key in item and isinstance(item[key], str):
                maximum = 4096 if key.endswith('path') else 128
                view[key] = item[key][:maximum]
                truncated = truncated or len(item[key]) > maximum
        result.append(view)
    return result, truncated


FIELDS = f'''m.id, m.project_id, m.sender_id, substr(a.name,1,128) AS sender_name,
substr(m.thread_id,1,128) AS thread_id, substr(m.subject,1,512) AS subject,
substr(m.body_md,1,{MAX_BODY + 1}) AS body_md,
substr(m.created_ts,1,64) AS created_ts, substr(m.importance,1,16) AS importance,
m.ack_required, substr(m.attachments,1,{MAX_ATTACHMENT_JSON + 1}) AS attachments'''


def _message(database, row):
    attachments, attachment_truncated = _attachment_paths(row['attachments'])
    recipients = database.execute('''SELECT r.agent_id, substr(a.name,1,128) AS name,
        substr(r.kind,1,8) AS kind, substr(r.read_ts,1,64) AS read_ts,
        substr(r.ack_ts,1,64) AS ack_ts FROM message_recipients r
        JOIN agents a ON a.id=r.agent_id WHERE r.message_id=? ORDER BY r.agent_id LIMIT 101''',
        (row['id'],)).fetchall()
    return dict(id=row['id'], storage_origin_id=row['project_id'], sender_id=row['sender_id'],
                sender_name=row['sender_name'], thread_id=row['thread_id'], subject=row['subject'],
                body_md=row['body_md'][:MAX_BODY], body_truncated=len(row['body_md']) > MAX_BODY,
                created_ts=row['created_ts'], importance=row['importance'], ack_required=bool(row['ack_required']),
                recipients=[dict(r) for r in recipients[:100]], recipients_truncated=len(recipients) > 100,
                attachments=attachments, attachments_truncated=attachment_truncated)


def query_archive(path, operation, *, query=None, message_id=None, limit=20, offset=0):
    if type(limit) is not int or not 1 <= limit <= MAX_RESULTS or type(offset) is not int or offset < 0:
        raise ArchiveError('ARCHIVE_LIMIT_INVALID')
    if operation not in {'search', 'thread', 'attachments'}:
        raise ArchiveError('ARCHIVE_OPERATION_INVALID')
    if operation == 'search':
        if not isinstance(query, str) or not 1 <= len(query) <= 512:
            raise ArchiveError('ARCHIVE_QUERY_INVALID')
    elif type(message_id) is not int or message_id <= 0:
        raise ArchiveError('ARCHIVE_MESSAGE_ID_INVALID')
    with archive_connection(path) as database:
        params = []
        if operation == 'search':
            # Plain substring search, deliberately not the old FTS query language.
            where = '(instr(lower(m.subject),lower(?))>0 OR instr(lower(m.body_md),lower(?))>0)'
            params = [query, query]
        else:
            anchor = database.execute('SELECT id,project_id,thread_id FROM messages WHERE id=?',
                                      (message_id,)).fetchone()
            if anchor is None:
                raise ArchiveError('ARCHIVE_MESSAGE_NOT_FOUND')
            if operation == 'thread':
                # Old projects may reuse thread IDs; anchor to the message's origin.
                where = 'm.project_id=? AND (m.thread_id=? OR m.id=?)'
                params = [anchor['project_id'], anchor['thread_id'] or str(anchor['id']), anchor['id']]
            else:
                where, params = 'm.id=?', [message_id]
        rows = database.execute('SELECT ' + FIELDS + ' FROM messages m JOIN agents a ON a.id=m.sender_id WHERE '
                                + where + ' ORDER BY m.created_ts,m.id LIMIT ? OFFSET ?',
                                [*params, limit + 1, offset]).fetchall()
        result = dict(archive_read_only=True, operation=operation, items=[], returned=0,
                      has_more=len(rows) > limit, next_offset=None)
        used = 1024
        for row in rows[:limit]:
            if operation == 'attachments':
                paths, truncated = _attachment_paths(row['attachments'])
                item = dict(id=row['id'], attachments=paths, attachments_truncated=truncated)
            else:
                item = _message(database, row)
            size = len(json.dumps(item, ensure_ascii=False).encode())
            if used + size > MAX_OUTPUT:
                result['has_more'] = True
                break
            result['items'].append(item)
            used += size + 2
        result['returned'] = len(result['items'])
        if result['has_more']:
            result['next_offset'] = offset + result['returned']
        return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive-copy', type=Path, required=True,
                        help='absolute path to a consistent backup copy, never the live database')
    parser.add_argument('--limit', type=int, default=20)
    parser.add_argument('--offset', type=int, default=0)
    commands = parser.add_subparsers(dest='operation', required=True)
    commands.add_parser('search').add_argument('query')
    for operation in ('thread', 'attachments'):
        commands.add_parser(operation).add_argument('--message-id', type=int, required=True)
    args = parser.parse_args(argv)
    try:
        result = query_archive(args.archive_copy, args.operation, query=getattr(args, 'query', None),
                               message_id=getattr(args, 'message_id', None), limit=args.limit, offset=args.offset)
        print(json.dumps(result, ensure_ascii=False))
    except (ArchiveError, sqlite3.Error) as error:
        raise SystemExit(str(error) if isinstance(error, ArchiveError) else 'ARCHIVE_QUERY_FAILED') from None


if __name__ == '__main__':
    main()
