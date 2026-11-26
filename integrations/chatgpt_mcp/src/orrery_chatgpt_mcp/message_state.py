"""Private durable claims: uncertainty never permits an automatic resend."""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import sqlite3
import stat
from typing import Iterator

from .model import ToolError


def check_private(path: Path, *, directory: bool = False) -> None:
    info = path.lstat()
    kind = stat.S_ISDIR if directory else stat.S_ISREG
    if (not kind(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077
            or (not directory and info.st_nlink != 1)):
        raise ValueError('message state must be owner-private, without links')


def read_private_json(path: Path) -> dict:
    if not path.is_absolute() or path.resolve() != path:
        raise ValueError('message policy must use an absolute canonical path')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or info.st_nlink != 1):
            raise ValueError('message policy must be an owner-private regular file')
        raw = stream.read(65537)
    if len(raw) > 65536:
        raise ValueError('message policy is too large')

    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError('duplicate policy field')
            value[key] = item
        return value

    value = json.loads(raw, object_pairs_hook=unique)
    if not isinstance(value, dict):
        raise ValueError('message policy must be an object')
    return value


class MessageState:
    def __init__(self, state_dir: Path):
        if not state_dir.is_absolute() or state_dir.resolve() != state_dir:
            raise ValueError('message state must use an absolute canonical path')
        state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        check_private(state_dir, directory=True)
        self.root = state_dir / 'fresh-message'
        self.root.mkdir(mode=0o700, exist_ok=True)
        check_private(self.root, directory=True)
        self.path = self.root / 'ledger.sqlite3'
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        except FileExistsError:
            check_private(self.path)
        else:
            os.close(fd)
        with self.transaction() as db:
            db.execute('CREATE TABLE IF NOT EXISTS intents (key TEXT PRIMARY KEY, record TEXT NOT NULL)')

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        if self.root.resolve() != self.root or self.path.resolve() != self.path:
            raise ValueError('message state path changed')
        check_private(self.root, directory=True)
        check_private(self.path)
        db = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        try:
            db.execute('PRAGMA synchronous=FULL')
            db.execute('BEGIN IMMEDIATE')
            yield db
            db.execute('COMMIT')
        except BaseException:
            if db.in_transaction:
                db.execute('ROLLBACK')
            raise
        finally:
            db.close()

    @staticmethod
    def get(db: sqlite3.Connection, key: str) -> dict | None:
        row = db.execute('SELECT record FROM intents WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else None

    @staticmethod
    def put(db: sqlite3.Connection, key: str, record: dict) -> None:
        db.execute('INSERT INTO intents VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET record=excluded.record',
                   (key, json.dumps(record, ensure_ascii=False, sort_keys=True)))

    def read(self, key: str) -> dict:
        with self.transaction() as db:
            record = self.get(db, key)
        if record is None:
            raise ToolError('not_found', 'no local message request matches this key')
        return record
