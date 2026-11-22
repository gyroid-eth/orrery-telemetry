"""Explicit state snapshots for the global-namespace candidate, never auto-discovered."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Iterator

from .namespace_plan import fingerprint


class StateMigrationError(ValueError):
    """Fixed diagnostics; never include rows, credentials or message contents."""


@contextmanager
def connection(path: Path, *, write: bool = False) -> Iterator[sqlite3.Connection]:
    database = sqlite3.connect(
        path if write else path.resolve().as_uri() + "?mode=ro", uri=not write
    )
    database.row_factory = sqlite3.Row
    database.execute("PRAGMA foreign_keys=ON")
    database.execute("PRAGMA busy_timeout=5000")
    try:
        yield database
        if write:
            database.commit()
    except BaseException:
        database.rollback()
        raise
    finally:
        database.close()


def rows(database: sqlite3.Connection, table: str) -> list[dict]:
    # Identifiers come from sqlite_master; quote them rather than interpolate untrusted SQL.
    name = '"' + table.replace('"', '""') + '"'
    values = [dict(row) for row in database.execute("SELECT * FROM " + name)]
    return sorted(
        values,
        key=lambda row: json.dumps(
            row, sort_keys=True, default=lambda value: {"bytes": value.hex()}
        ),
    )


def tables(database: sqlite3.Connection) -> list[str]:
    return [
        row[0]
        for row in database.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' AND name NOT LIKE 'fts_messages%' ORDER BY name"
        )
    ]


def database_manifest(path: Path) -> dict:
    with connection(path) as database:
        content = {name: rows(database, name) for name in tables(database)}
        schema = [
            tuple(row)
            for row in database.execute(
                "SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
            )
        ]
    # JSON columns stay byte-for-byte strings, BLOBs use tagged hex for the digest.
    with connection(path) as database:
        if database.execute(
            "SELECT 1 FROM sqlite_master WHERE name='fts_messages'"
        ).fetchone():
            content["fts_messages"] = rows(database, "fts_messages")
    encoded = json.loads(
        json.dumps(content, default=lambda value: {"bytes": value.hex()})
    )
    return {
        "tables": {
            name: {"count": len(value), "digest": fingerprint(value)}
            for name, value in encoded.items()
        },
        "schema": fingerprint(schema),
    }


def tree_manifest(root: Path) -> dict[str, dict]:
    if not root.is_dir() or root.is_symlink():
        raise StateMigrationError("STATE_TREE_REQUIRED")
    result = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise StateMigrationError("STATE_TREE_SYMLINK")
        if path.is_file():
            result[path.relative_to(root).as_posix()] = {
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "size": path.stat().st_size,
            }
        elif not path.is_dir():
            raise StateMigrationError("STATE_TREE_SPECIAL_FILE")
    return result


def atomic_json(path: Path, document: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(path.name + ".new")
    # The enclosing candidate workspace is private and exclusively locked.
    descriptor = os.open(
        temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600
    )
    try:
        with os.fdopen(descriptor, "w") as stream:
            json.dump(
                document, stream, sort_keys=True, separators=(",", ":"), allow_nan=False
            )
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        temporary.unlink(missing_ok=True)


def backup(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    target.unlink(missing_ok=True)
    with connection(source) as original, connection(target, write=True) as candidate:
        original.backup(candidate)
    target.chmod(0o600)


@dataclass(frozen=True)
class SourceBundle:
    mail: Path
    delivery: Path
    archive: Path
    signals: Path
    history: Path
    bindings: Path
    config: Path

    def manifest(self) -> dict:
        databases = {
            "mail": database_manifest(self.mail),
            "delivery": database_manifest(self.delivery),
        }
        trees = {
            key: tree_manifest(getattr(self, key))
            for key in ("archive", "signals", "history")
        }
        files = {
            key: hashlib.sha256(getattr(self, key).read_bytes()).hexdigest()
            for key in ("bindings", "config")
        }
        return {"databases": databases, "trees": trees, "files": files}

    def snapshot(self, target: Path) -> SourceBundle:
        destination = target.resolve()
        for source in asdict(self).values():
            original = Path(source).resolve()
            if (
                destination == original
                or destination in original.parents
                or original in destination.parents
            ):
                raise StateMigrationError("SOURCE_SNAPSHOT_OVERLAP")
        before = self.manifest()
        ownership = fingerprint(
            {key: str(value.resolve()) for key, value in asdict(self).items()}
        )
        if target.exists():
            marker = target / ".namespace-snapshot-owned"
            if (
                not marker.is_file()
                or marker.is_symlink()
                or marker.read_text() != ownership
            ):
                raise StateMigrationError("SNAPSHOT_TARGET_NOT_OWNED")
            shutil.rmtree(target)
        target.mkdir(parents=True, mode=0o700)
        marker = target / ".namespace-snapshot-owned"
        marker.write_text(ownership)
        marker.chmod(0o600)
        for name in ("mail", "delivery"):
            backup(getattr(self, name), target / (name + ".sqlite3"))
        for name in ("archive", "signals", "history"):
            shutil.copytree(getattr(self, name), target / name)
        for name in ("bindings", "config"):
            shutil.copyfile(getattr(self, name), target / (name + ".json"))
            (target / (name + ".json")).chmod(0o600)
        result = SourceBundle(
            target / "mail.sqlite3",
            target / "delivery.sqlite3",
            target / "archive",
            target / "signals",
            target / "history",
            target / "bindings.json",
            target / "config.json",
        )
        if self.manifest() != before or result.manifest() != before:
            raise StateMigrationError("SOURCE_CHANGED_DURING_SNAPSHOT")
        return result
