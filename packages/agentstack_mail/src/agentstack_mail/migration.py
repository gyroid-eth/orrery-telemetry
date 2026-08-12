"""Atomic, copy-only migration and fail-closed rollback assessment.

The migration command deliberately accepts every path explicitly. It never
loads the running service configuration and never starts, stops, or contacts a
service. It does not change canonical source records, though opening and closing
a WAL database read-only can create SQLite-owned ``-wal``/``-shm`` runtime
sidecars. The copy-only ``mode=rw`` writer guard can additionally checkpoint or
remove them on close and can therefore change main-file bytes. Database,
archive, and signal state are staged below one sibling directory and become
visible together through one directory rename.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import uuid
from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final
from urllib.parse import quote

from . import __version__ as AGENTSTACK_MAIL_VERSION


MANIFEST_NAME: Final[str] = "migration-manifest.json"
STAGING_MARKER: Final[str] = ".agentstack-mail-migration-staging.json"
ARCHIVE_EXCLUDED_ROOT_NAMES: Final[frozenset[str]] = frozenset({".git", "server.pid"})
ARCHIVE_POLICY: Final[dict[str, Any]] = {
    "copied": "working_tree",
    "excluded_root_names": [".git", "server.pid"],
    "legacy_git_history": "not_copied",
    "new_git_history": "single_root_baseline_commit",
}
DATABASE_POLICY: Final[dict[str, str]] = {
    "copied": "sqlite_logical_backup_including_committed_wal",
    "compared": "main_database_schema_rows_relations_and_pragmas",
    "sqlite_runtime_sidecars": (
        "excluded_ro_may_create_rw_guard_may_checkpoint_or_remove"
    ),
}
BASELINE_BRANCH: Final[str] = "main"
BASELINE_AUTHOR_NAME: Final[str] = "AgentStack Mail Migration"
BASELINE_AUTHOR_EMAIL: Final[str] = "agentstack-mail-migration@localhost"
BASELINE_COMMIT_SUBJECT: Final[str] = "AgentStack Mail migration baseline"
GIT_TIMEOUT_SECONDS: Final[int] = 120
OWNERSHIP_JSON_MAX_BYTES: Final[int] = 16 * 1024 * 1024
COLD_BACKUP_RECEIPT_NAME: Final[str] = "cold-backup-receipt.json"
COLD_RESTORE_MARKER_NAME: Final[str] = ".agentstack-mail-cold-restore.json"
COLD_REHEARSAL_RECEIPT_NAME: Final[str] = "cold-restore-rehearsal-receipt.json"
COLD_REHEARSAL_VERIFICATION_NAME: Final[str] = (
    "cold-restore-rehearsal-verification.json"
)
COLD_REHEARSAL_MARKER_NAME: Final[str] = ".agentstack-mail-cold-rehearsal.json"
COLD_REHEARSAL_DAMAGE_PLAN: Final[str] = "truncate-main-and-create-absent-sidecars-v1"
REHEARSAL_GENERATOR_RECEIPT_NAME: Final[str] = "generator-receipt.json"
REHEARSAL_SCALE_MINIMUMS: Final[dict[str, int]] = {
    "database_family_bytes": 50 * 1024 * 1024,
    "agents": 700,
    "messages": 8_000,
    "message_recipients": 8_000,
}
COLD_BACKUP_FILE_NAMES: Final[dict[str, str]] = {
    "main": "storage.sqlite3",
    "wal": "storage.sqlite3-wal",
    "shm": "storage.sqlite3-shm",
}
REQUIRED_TABLES: Final[frozenset[str]] = frozenset(
    {
        "projects",
        "agents",
        "messages",
        "message_recipients",
        "file_reservations",
    }
)
CUTOVER_STAGES: Final[tuple[str, ...]] = (
    "C0_LEGACY_AUTHORITY_PREPARED",
    "C1_NEW_INSTALLED",
    "C2_LEGACY_QUIESCED",
    "C3_MIGRATION_VERIFIED",
    "C4_NEW_SERVICE_READY",
    "C5_CLIENT_SWITCHING",
    "C6_NEW_AUTHORITY_VERIFIED",
)
ASSESSABLE_STAGES: Final[tuple[str, ...]] = CUTOVER_STAGES[3:]


class MigrationError(RuntimeError):
    """A migration safety check failed."""


class VerificationError(MigrationError):
    """Source, staging, or destination content failed verification."""


FaultHook = Callable[[str], None]
# Exhaustive by construction: _call_fault rejects every call-site seam absent here,
# and the test suite injects one interruption at every listed seam.
PRE_PUBLICATION_FAULT_PHASES: Final[tuple[str, ...]] = (
    "before_staging",
    "before_database_backup",
    "after_database_backup",
    "archive_copy:before_file",
    "archive_copy:copy_chunk",
    "after_archive_copy",
    "signals_copy:before_file",
    "signals_copy:copy_chunk",
    "after_signals_copy",
    "before_baseline_git",
    "after_baseline_git_init",
    "after_baseline_git_add",
    "after_baseline_git_commit",
    "before_verification",
    "before_fsync",
    "after_fsync",
    "before_publish",
)
POST_PUBLICATION_FAULT_PHASES: Final[tuple[str, ...]] = ("after_publish",)
MIGRATION_FAULT_PHASES: Final[tuple[str, ...]] = (
    PRE_PUBLICATION_FAULT_PHASES + POST_PUBLICATION_FAULT_PHASES
)


@dataclass(frozen=True, slots=True)
class StatePaths:
    """The three state surfaces owned by one mail authority."""

    database: Path
    archive: Path
    signals: Path

    @classmethod
    def from_root(cls, root: Path) -> StatePaths:
        return cls(
            database=root / "storage.sqlite3",
            archive=root / "archive",
            signals=root / "signals",
        )

    def resolved(self) -> StatePaths:
        # absolute() deliberately does not follow symlinks.  State roots are
        # validated separately so a symlink cannot disappear from the safety
        # checks merely because resolve() canonicalised it first.
        return StatePaths(
            database=self.database.expanduser().absolute(),
            archive=self.archive.expanduser().absolute(),
            signals=self.signals.expanduser().absolute(),
        )


@dataclass(frozen=True, slots=True)
class MigrationResult:
    status: str
    destination_root: str
    operation_id: str | None
    state_sha256: str


def _utc_now() -> str:
    # Git commit timestamps have one-second resolution. Use the same precision
    # in the manifest so the recorded migration instant and baseline commit are
    # exactly comparable instead of merely close.
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _typed_value(value: Any) -> list[Any]:
    if value is None:
        return ["null", None]
    if isinstance(value, bytes):
        return ["blob", hashlib.sha256(value).hexdigest(), len(value)]
    if isinstance(value, int):
        return ["integer", value]
    if isinstance(value, float):
        return ["real", value.hex()]
    if isinstance(value, str):
        return ["text", value]
    raise VerificationError(f"unsupported SQLite value type: {type(value)!r}")


def _quote_identifier(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _rows_digest(connection: sqlite3.Connection, query: str) -> dict[str, Any]:
    rows = [
        [_typed_value(value) for value in row]
        for row in connection.execute(query).fetchall()
    ]
    rows.sort(key=_canonical_json)
    return {"count": len(rows), "sha256": _sha256(rows)}


def _relation_snapshot(connection: sqlite3.Connection) -> dict[str, Any]:
    """Capture edges whose preservation cannot be demonstrated by counts."""

    queries = {
        "agent_project": "SELECT id, project_id FROM agents",
        "message_sender_thread": (
            "SELECT id, project_id, sender_id, thread_id, created_ts FROM messages"
        ),
        "message_recipient_receipt": (
            "SELECT message_id, agent_id, kind, read_ts, ack_ts "
            "FROM message_recipients"
        ),
        "reservation_owner": (
            "SELECT id, project_id, agent_id, path_pattern, exclusive, "
            "created_ts, expires_ts, released_ts FROM file_reservations"
        ),
        "thread_membership": (
            "SELECT m.project_id, m.thread_id, m.id, m.sender_id, "
            "mr.agent_id, mr.kind, mr.read_ts, mr.ack_ts "
            "FROM messages AS m LEFT JOIN message_recipients AS mr "
            "ON mr.message_id = m.id WHERE m.thread_id IS NOT NULL"
        ),
    }
    return {name: _rows_digest(connection, query) for name, query in queries.items()}


def _orphan_diagnostics(connection: sqlite3.Connection) -> dict[str, int]:
    queries = {
        "agents_without_project": (
            "SELECT COUNT(*) FROM agents AS a LEFT JOIN projects AS p "
            "ON p.id=a.project_id WHERE p.id IS NULL"
        ),
        "messages_without_project": (
            "SELECT COUNT(*) FROM messages AS m LEFT JOIN projects AS p "
            "ON p.id=m.project_id WHERE p.id IS NULL"
        ),
        "messages_without_sender": (
            "SELECT COUNT(*) FROM messages AS m LEFT JOIN agents AS a "
            "ON a.id=m.sender_id WHERE a.id IS NULL"
        ),
        "message_sender_project_mismatch": (
            "SELECT COUNT(*) FROM messages AS m JOIN agents AS a "
            "ON a.id=m.sender_id WHERE a.project_id != m.project_id"
        ),
        "recipients_without_message": (
            "SELECT COUNT(*) FROM message_recipients AS mr LEFT JOIN messages AS m "
            "ON m.id=mr.message_id WHERE m.id IS NULL"
        ),
        "recipients_without_agent": (
            "SELECT COUNT(*) FROM message_recipients AS mr LEFT JOIN agents AS a "
            "ON a.id=mr.agent_id WHERE a.id IS NULL"
        ),
        "recipient_project_mismatch": (
            "SELECT COUNT(*) FROM message_recipients AS mr "
            "JOIN messages AS m ON m.id=mr.message_id "
            "JOIN agents AS a ON a.id=mr.agent_id "
            "WHERE a.project_id != m.project_id"
        ),
        "reservations_without_project": (
            "SELECT COUNT(*) FROM file_reservations AS r LEFT JOIN projects AS p "
            "ON p.id=r.project_id WHERE p.id IS NULL"
        ),
        "reservations_without_agent": (
            "SELECT COUNT(*) FROM file_reservations AS r LEFT JOIN agents AS a "
            "ON a.id=r.agent_id WHERE a.id IS NULL"
        ),
        "reservation_owner_project_mismatch": (
            "SELECT COUNT(*) FROM file_reservations AS r JOIN agents AS a "
            "ON a.id=r.agent_id WHERE a.project_id != r.project_id"
        ),
    }
    return {
        name: int(connection.execute(query).fetchone()[0])
        for name, query in queries.items()
    }


def snapshot_database(path: Path) -> dict[str, Any]:
    """Return a logical snapshot; SQLite WAL sidecars are explicitly excluded."""

    path = _absolute_without_symlinks(path)
    before = _database_file_identity(path)
    container_before = _database_container_identity(path)
    try:
        connection = sqlite3.connect(_database_uri(path, "ro"), uri=True)
    except sqlite3.Error as exc:
        raise VerificationError(f"cannot open database read-only: {path}: {exc}") from exc
    try:
        try:
            connection.execute("PRAGMA query_only=ON")
            connection.execute("BEGIN")
        except sqlite3.Error as exc:
            raise VerificationError(
                f"cannot start a consistent database snapshot: {path}: {exc}"
            ) from exc
        opened = _database_file_identity(path)
        if opened[:4] != before[:4]:
            raise VerificationError(f"database changed identity while it was opened: {path}")
        if _database_container_identity(path) != container_before:
            raise VerificationError(
                f"database parent changed while it was opened: {path.parent}"
            )
        result = _snapshot_database_connection(path, connection)
    finally:
        if connection.in_transaction:
            connection.rollback()
        connection.close()
    after = _database_file_identity(path)
    if after[:4] != before[:4]:
        raise VerificationError(f"database changed identity while it was read: {path}")
    if _database_container_identity(path) != container_before:
        raise VerificationError(
            f"database parent changed while it was read: {path.parent}"
        )
    return result


def _snapshot_database_connection(
    path: Path, connection: sqlite3.Connection
) -> dict[str, Any]:
    if not connection.in_transaction:
        raise VerificationError(
            f"database snapshot requires one active read transaction: {path}"
        )
    try:
        integrity = [str(row[0]) for row in connection.execute("PRAGMA integrity_check")]
        if integrity != ["ok"]:
            raise VerificationError(f"SQLite integrity_check failed: {integrity!r}")
        foreign_keys = [
            [_typed_value(value) for value in row]
            for row in connection.execute("PRAGMA foreign_key_check").fetchall()
        ]
        if foreign_keys:
            raise VerificationError(
                f"SQLite foreign_key_check found {len(foreign_keys)} violation(s)"
            )
        schema = [
            [_typed_value(value) for value in row]
            for row in connection.execute(
                "SELECT type, name, tbl_name, sql FROM sqlite_schema "
                "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
            ).fetchall()
        ]
        tables = [
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
        ]
        missing = sorted(REQUIRED_TABLES.difference(tables))
        if missing:
            raise VerificationError(
                "database is missing required table(s): " + ", ".join(missing)
            )
        table_state = {
            table: _rows_digest(connection, f"SELECT * FROM {_quote_identifier(table)}")
            for table in tables
        }
        orphans = _orphan_diagnostics(connection)
        if any(orphans.values()):
            raise VerificationError(f"database contains unresolved relationships: {orphans}")
        relations = _relation_snapshot(connection)
        pragmas = {
            "application_id": int(connection.execute("PRAGMA application_id").fetchone()[0]),
            "auto_vacuum": int(connection.execute("PRAGMA auto_vacuum").fetchone()[0]),
            "encoding": str(connection.execute("PRAGMA encoding").fetchone()[0]),
            "journal_mode": str(connection.execute("PRAGMA journal_mode").fetchone()[0]),
            "page_size": int(connection.execute("PRAGMA page_size").fetchone()[0]),
            "schema_version": int(connection.execute("PRAGMA schema_version").fetchone()[0]),
            "user_version": int(connection.execute("PRAGMA user_version").fetchone()[0]),
        }
        logical = {
            "schema_sha256": _sha256(schema),
            "pragmas": pragmas,
            "tables": table_state,
            "relations": relations,
        }
        return {**logical, "logical_sha256": _sha256(logical)}
    except sqlite3.Error as exc:
        raise VerificationError(f"cannot verify SQLite database {path}: {exc}") from exc


def _absolute_without_symlinks(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path.expanduser())))


def _canonical_absolute_path(value: Any, *, label: str) -> Path:
    if not isinstance(value, str) or "\x00" in value:
        raise MigrationError(f"{label} must be a canonical absolute path")
    path = Path(value)
    if (
        not path.is_absolute()
        or value.startswith("//")
        or os.path.normpath(value) != value
        or str(path) != value
    ):
        raise MigrationError(
            f"{label} must be a canonical absolute path without '.', '..', or '~': "
            f"{value!r}"
        )
    return path


def _assert_no_symlink_components(path: Path) -> None:
    path = _absolute_without_symlinks(path)
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        try:
            info = current.lstat()
        except FileNotFoundError:
            return
        if stat.S_ISLNK(info.st_mode):
            raise VerificationError(f"symbolic path components are not accepted: {current}")


_DATABASE_IDENTITY_FIELDS: Final[tuple[str, ...]] = (
    "st_dev",
    "st_ino",
    "st_mode",
    "st_nlink",
    "st_size",
    "st_mtime_ns",
    "st_ctime_ns",
)

_DATABASE_CONTAINER_IDENTITY_FIELDS: Final[tuple[str, ...]] = (
    "st_dev",
    "st_ino",
    "st_mode",
    "st_uid",
)


def _database_file_identity(path: Path) -> tuple[int, ...]:
    path = _absolute_without_symlinks(path)
    _assert_no_symlink_components(path)
    try:
        info = path.lstat()
    except FileNotFoundError as exc:
        raise VerificationError(f"database is missing or not a file: {path}") from exc
    if not stat.S_ISREG(info.st_mode):
        raise VerificationError(f"database is not a regular file: {path}")
    if info.st_nlink != 1:
        raise VerificationError(f"hard-linked databases are not accepted: {path}")
    return tuple(int(getattr(info, field)) for field in _DATABASE_IDENTITY_FIELDS)


def _database_container_identity(path: Path) -> tuple[int, ...]:
    """Fingerprint the stable identity of the database root container.

    The database lives directly in this directory.  Device/inode/type/owner
    identity detects replacement of that parent.  The seal intentionally stops
    here and excludes directory entry metadata: SQLite may legitimately create
    or retire WAL/SHM siblings, while migration may publish an unrelated sibling
    in the container above during a large snapshot.
    """

    path = _absolute_without_symlinks(path)
    container = path.parent
    _assert_no_symlink_components(container)
    try:
        info = container.lstat()
    except FileNotFoundError as exc:
        raise VerificationError(f"database container is missing: {container}") from exc
    if not stat.S_ISDIR(info.st_mode):
        raise VerificationError(f"database container is not a directory: {container}")
    return tuple(
        int(getattr(info, field)) for field in _DATABASE_CONTAINER_IDENTITY_FIELDS
    )


def _database_uri(path: Path, mode: str) -> str:
    if mode not in {"ro", "rw"}:
        raise AssertionError(f"unsupported SQLite URI mode: {mode}")
    return f"file:{quote(path.as_posix(), safe='/')}?mode={mode}"


@contextmanager
def _database_writer_guard(path: Path) -> Iterator[sqlite3.Connection]:
    """Hold SQLite's writer slot while a supposedly quiesced DB is inspected."""

    path = _absolute_without_symlinks(path)
    before = _database_file_identity(path)
    container_before = _database_container_identity(path)
    # SQLite's read-only VFS path accepts BEGIN IMMEDIATE in WAL mode without
    # taking the writer slot. Open mode=rw solely to acquire that slot, then
    # enable query_only before exposing the connection to snapshot/backup code.
    uri = _database_uri(path, "rw")
    try:
        connection = sqlite3.connect(
            uri,
            uri=True,
            timeout=0,
            isolation_level=None,
        )
    except sqlite3.Error as exc:
        raise VerificationError(
            f"cannot open database for guarded read: {path}: {exc}"
        ) from exc
    try:
        try:
            connection.execute("BEGIN IMMEDIATE")
        except sqlite3.Error as exc:
            raise VerificationError(
                f"database has an active writer or cannot be quiesced: {path}: {exc}"
            ) from exc
        connection.execute("PRAGMA query_only=ON")
        opened = _database_file_identity(path)
        if opened != before:
            raise VerificationError(f"database changed while it was opened: {path}")
        if _database_container_identity(path) != container_before:
            raise VerificationError(
                f"database parent changed while it was opened: {path.parent}"
            )
        yield connection
        after = _database_file_identity(path)
        if after != before:
            raise VerificationError(f"database changed while migration held it: {path}")
    finally:
        if connection.in_transaction:
            connection.rollback()
        connection.close()


def _is_lock_artifact(name: str) -> bool:
    return name.endswith(".lock") or name.endswith(".lock.owner.json")


def _tree_entries(
    root: Path,
    *,
    excluded_root_names: frozenset[str] = frozenset(),
) -> Iterable[tuple[Path, Path]]:
    root = _absolute_without_symlinks(root)
    directory_fields = (
        "st_dev",
        "st_ino",
        "st_mode",
        "st_nlink",
        "st_size",
        "st_mtime_ns",
        "st_ctime_ns",
    )

    def walk(directory: Path, relative_directory: Path) -> Iterable[tuple[Path, Path]]:
        try:
            entries = sorted(os.scandir(directory), key=lambda entry: entry.name)
        except OSError as exc:
            raise VerificationError(f"cannot scan state directory {directory}: {exc}") from exc
        for entry in entries:
            relative = relative_directory / entry.name
            path = directory / entry.name
            if _is_lock_artifact(entry.name):
                raise VerificationError(
                    f"active or stale writer lock must be resolved before migration: {path}"
                )
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError as exc:
                raise VerificationError(f"cannot inspect state entry {path}: {exc}") from exc
            if relative_directory == Path(".") and entry.name in excluded_root_names:
                if entry.name != ".git" and (
                    not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                ):
                    raise VerificationError(
                        "excluded runtime files must be regular and singly linked: "
                        f"{path}"
                    )
                continue
            if stat.S_ISLNK(info.st_mode):
                raise VerificationError(f"symbolic links are not accepted: {path}")
            if stat.S_ISDIR(info.st_mode):
                if entry.name == ".git":
                    raise VerificationError(f"nested Git repositories are not accepted: {path}")
                yield path, relative
                yield from walk(path, relative)
                after = path.lstat()
                if any(
                    getattr(info, field) != getattr(after, field)
                    for field in directory_fields
                ):
                    raise VerificationError(
                        f"state directory changed while it was scanned: {path}"
                    )
                continue
            if stat.S_ISREG(info.st_mode):
                if info.st_nlink != 1:
                    raise VerificationError(f"hard-linked files are not accepted: {path}")
                yield path, relative
                continue
            raise VerificationError(f"special filesystem entry is not accepted: {path}")

    root_before = root.lstat()
    if not stat.S_ISDIR(root_before.st_mode):
        raise VerificationError(f"state tree is not a real directory: {root}")
    yield from walk(root, Path("."))
    root_after = root.lstat()
    if any(
        getattr(root_before, field) != getattr(root_after, field)
        for field in directory_fields
    ):
        raise VerificationError(f"state tree changed while it was scanned: {root}")


def snapshot_tree(
    root: Path,
    *,
    required: bool,
    excluded_root_names: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    root = _absolute_without_symlinks(root)
    _assert_no_symlink_components(root)
    if not root.exists():
        if required:
            raise VerificationError(f"required directory is missing: {root}")
        return {"exists": False, "entries": 0, "sha256": _sha256([])}
    if not root.is_dir() or root.is_symlink():
        raise VerificationError(f"state tree is not a real directory: {root}")
    entries: list[list[Any]] = [
        ["directory", ".", stat.S_IMODE(root.lstat().st_mode)]
    ]
    for path, relative in _tree_entries(root, excluded_root_names=excluded_root_names):
        before = path.lstat()
        mode = stat.S_IMODE(before.st_mode)
        if stat.S_ISDIR(before.st_mode):
            entries.append(["directory", relative.as_posix(), mode])
            continue
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise VerificationError(f"state file changed type before snapshot: {path}")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags)
        except OSError as exc:
            raise VerificationError(
                f"cannot open state file without following links: {path}: {exc}"
            ) from exc
        try:
            opened = os.fstat(descriptor)
            comparable = (
                "st_dev",
                "st_ino",
                "st_mode",
                "st_nlink",
                "st_size",
                "st_mtime_ns",
                "st_ctime_ns",
            )
            if any(
                getattr(before, field) != getattr(opened, field)
                for field in comparable
            ):
                raise VerificationError(
                    f"state file changed while it was opened: {path}"
                )
            digest = hashlib.sha256()
            with os.fdopen(descriptor, "rb", closefd=False) as handle:
                while chunk := handle.read(1024 * 1024):
                    digest.update(chunk)
        finally:
            os.close(descriptor)
        after = path.lstat()
        if any(
            getattr(before, field) != getattr(after, field) for field in comparable
        ):
            raise VerificationError(f"state file changed while it was read: {path}")
        entries.append(
            ["file", relative.as_posix(), mode, before.st_size, digest.hexdigest()]
        )
    return {"exists": True, "entries": len(entries), "sha256": _sha256(entries)}


def _git_environment(extra: dict[str, str] | None = None) -> dict[str, str]:
    environment = os.environ.copy()
    for name in (
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_COMMON_DIR",
        "GIT_NAMESPACE",
        "GIT_CONFIG_PARAMETERS",
        "GIT_CONFIG_SYSTEM",
    ):
        environment.pop(name, None)
    environment.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_CONFIG_COUNT": "3",
            "GIT_CONFIG_KEY_0": "gc.auto",
            "GIT_CONFIG_VALUE_0": "0",
            "GIT_CONFIG_KEY_1": "gc.autoDetach",
            "GIT_CONFIG_VALUE_1": "false",
            "GIT_CONFIG_KEY_2": "maintenance.auto",
            "GIT_CONFIG_VALUE_2": "false",
            "LC_ALL": "C",
        }
    )
    if extra:
        environment.update(extra)
    return environment


def _git_run(
    root: Path,
    *arguments: str,
    extra_environment: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            [
                "git",
                "-c",
                f"core.hooksPath={os.devnull}",
                "-c",
                "commit.gpgsign=false",
                "-c",
                "core.autocrlf=false",
                "-C",
                str(root),
                *arguments,
            ],
            check=False,
            capture_output=True,
            text=True,
            env=_git_environment(extra_environment),
            timeout=GIT_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise VerificationError(
            f"Git command timed out after {GIT_TIMEOUT_SECONDS}s: {' '.join(arguments)}"
        ) from exc
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "no diagnostic"
        raise VerificationError(
            f"Git command failed ({' '.join(arguments)}): {detail}"
        )
    return result


def _assert_git_directory(root: Path) -> Path:
    git_directory = root / ".git"
    try:
        info = git_directory.lstat()
    except FileNotFoundError as exc:
        raise VerificationError(f"archive is not a normal Git worktree: {root}")
    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise VerificationError(f"archive .git is not a real directory: {git_directory}")
    for path in git_directory.rglob("*"):
        if _is_lock_artifact(path.name):
            raise VerificationError(
                f"active or stale Git writer lock must be resolved before migration: {path}"
            )
    return git_directory


def _git_blob_oid(path: Path) -> str:
    size = path.stat().st_size
    digest = hashlib.sha1()
    digest.update(f"blob {size}\0".encode("ascii"))
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _baseline_tree_snapshot(root: Path) -> dict[str, Any]:
    object_format = _git_run(root, "rev-parse", "--show-object-format").stdout.strip()
    if object_format != "sha1":
        raise VerificationError(f"unsupported Git object format: {object_format!r}")
    records = _git_run(root, "ls-files", "-s", "-z").stdout.split("\0")
    index: dict[str, tuple[str, str]] = {}
    for record in records:
        if not record:
            continue
        try:
            metadata, relative = record.split("\t", 1)
            mode, oid, stage = metadata.split(" ")
        except ValueError as exc:
            raise VerificationError("baseline Git index output is malformed") from exc
        if stage != "0" or relative in index:
            raise VerificationError(f"baseline Git index has a non-stage-0 entry: {relative}")
        index[relative] = (mode, oid)

    files: dict[str, tuple[str, str]] = {}
    for path, relative in _tree_entries(
        root, excluded_root_names=ARCHIVE_EXCLUDED_ROOT_NAMES
    ):
        if path.is_dir():
            continue
        info = path.lstat()
        mode = "100755" if info.st_mode & stat.S_IXUSR else "100644"
        files[relative.as_posix()] = (mode, _git_blob_oid(path))
    if index != files:
        missing = sorted(set(files).difference(index))[:5]
        extra = sorted(set(index).difference(files))[:5]
        changed = sorted(
            path for path in set(files).intersection(index) if files[path] != index[path]
        )[:5]
        raise VerificationError(
            "baseline Git tree is not byte-exact with the copied working tree: "
            f"missing={missing}, extra={extra}, changed={changed}"
        )
    return {"files": len(files), "sha256": _sha256(files)}


def _git_object_inventory(root: Path) -> dict[str, Any]:
    records: dict[str, tuple[str, int]] = {}
    output = _git_run(
        root,
        "cat-file",
        "--batch-check=%(objectname) %(objecttype) %(objectsize)",
        "--batch-all-objects",
    ).stdout
    for line in output.splitlines():
        try:
            oid, object_type, size_text = line.split(" ")
            size = int(size_text)
        except ValueError as exc:
            raise VerificationError("baseline Git object inventory is malformed") from exc
        if oid in records or object_type not in {"blob", "commit", "tag", "tree"}:
            raise VerificationError(
                f"baseline Git object inventory has an invalid record: {line!r}"
            )
        records[oid] = (object_type, size)
    reachable = {
        line
        for line in _git_run(
            root,
            "rev-list",
            "--objects",
            "--all",
            "--no-object-names",
        ).stdout.splitlines()
        if line
    }
    all_objects = set(records)
    if all_objects != reachable:
        unreachable = sorted(all_objects.difference(reachable))[:5]
        missing = sorted(reachable.difference(all_objects))[:5]
        raise VerificationError(
            "baseline Git object database is not exactly its reachable set: "
            f"unreachable={unreachable}, missing={missing}"
        )
    inventory = [[oid, *records[oid]] for oid in sorted(records)]
    return {"count": len(inventory), "sha256": _sha256(inventory)}


def _git_snapshot(root: Path, *, require_baseline: bool = False) -> dict[str, Any]:
    root = _absolute_without_symlinks(root)
    _assert_no_symlink_components(root)
    _assert_git_directory(root)

    head = _git_run(root, "rev-parse", "--verify", "HEAD").stdout.strip()
    refs_output = _git_run(
        root, "for-each-ref", "--format=%(refname) %(objectname)"
    ).stdout
    status_output = _git_run(
        root, "status", "--porcelain=v1", "--untracked-files=all"
    ).stdout
    state = {
        "exists": True,
        "head": head,
        "refs_sha256": hashlib.sha256(refs_output.encode()).hexdigest(),
        "status_sha256": hashlib.sha256(status_output.encode()).hexdigest(),
    }
    if require_baseline:
        _git_run(root, "fsck", "--full", "--strict")
        refs = [line for line in refs_output.splitlines() if line]
        if refs != [f"refs/heads/{BASELINE_BRANCH} {head}"]:
            raise VerificationError(f"baseline Git has unexpected refs: {refs!r}")
        branch = _git_run(root, "symbolic-ref", "--short", "HEAD").stdout.strip()
        commit_count = int(_git_run(root, "rev-list", "--all", "--count").stdout)
        roots = int(
            _git_run(root, "rev-list", "--all", "--max-parents=0", "--count").stdout
        )
        remotes = _git_run(root, "remote").stdout.splitlines()
        alternates = root / ".git" / "objects" / "info" / "alternates"
        if branch != BASELINE_BRANCH or commit_count != 1 or roots != 1:
            raise VerificationError(
                "baseline Git must contain exactly one root commit on "
                f"{BASELINE_BRANCH}: branch={branch!r}, commits={commit_count}, roots={roots}"
            )
        if remotes or alternates.exists():
            raise VerificationError(
                f"baseline Git must have no remotes or alternates: remotes={remotes!r}"
            )
        if status_output:
            raise VerificationError("baseline Git working tree is not clean")
        metadata = _git_run(
            root,
            "show",
            "-s",
            "--format=%an%x00%ae%x00%aI%x00%cn%x00%ce%x00%cI%x00%s",
            "HEAD",
        ).stdout.rstrip("\n").split("\0")
        if len(metadata) != 7:
            raise VerificationError("baseline Git commit metadata is malformed")
        message = _git_run(root, "show", "-s", "--format=%B", "HEAD").stdout.rstrip("\n")
        state["baseline"] = {
            "branch": branch,
            "commit_count": commit_count,
            "root_count": roots,
            "author_name": metadata[0],
            "author_email": metadata[1],
            "author_date": metadata[2],
            "committer_name": metadata[3],
            "committer_email": metadata[4],
            "committer_date": metadata[5],
            "subject": metadata[6],
            "message": message,
            "tree": _baseline_tree_snapshot(root),
            "objects": _git_object_inventory(root),
        }
    return {**state, "sha256": _sha256(state)}


def snapshot_state(
    paths: StatePaths,
    *,
    require_baseline_git: bool = False,
    _database_connection: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    paths = paths.resolved()
    database = (
        snapshot_database(paths.database)
        if _database_connection is None
        else _snapshot_database_connection(paths.database, _database_connection)
    )
    archive = snapshot_tree(
        paths.archive,
        required=True,
        excluded_root_names=ARCHIVE_EXCLUDED_ROOT_NAMES,
    )
    signals = snapshot_tree(paths.signals, required=False)
    git = _git_snapshot(paths.archive, require_baseline=require_baseline_git)
    state = {
        "database": database,
        "archive": archive,
        "signals": signals,
        "git": git,
    }
    state_sha256 = _state_snapshot_digest(state)
    snapshot_sha256 = _snapshot_digest({**state, "state_sha256": state_sha256})
    return {**state, "state_sha256": state_sha256, "snapshot_sha256": snapshot_sha256}


def _state_snapshot_digest(state: dict[str, Any]) -> str:
    try:
        database = state["database"]
        # WAL/SHM files are SQLite runtime coordination artifacts, not a fourth
        # authority surface. DATABASE_POLICY records their explicit exclusion;
        # logical schema, rows, relationships, and PRAGMAs remain authoritative.
        logical_database = {
            "schema_sha256": database["schema_sha256"],
            "pragmas": database["pragmas"],
            "tables": database["tables"],
            "relations": database["relations"],
        }
        logical_sha256 = _sha256(logical_database)
        if logical_sha256 != database["logical_sha256"]:
            raise VerificationError("database snapshot digest is internally inconsistent")
        comparable = {
            "database": logical_sha256,
            "archive": state["archive"],
            "signals": state["signals"],
        }
    except (KeyError, TypeError) as exc:
        raise VerificationError("state snapshot is malformed") from exc
    return _sha256(comparable)


def _snapshot_digest(state: dict[str, Any]) -> str:
    try:
        state_sha256 = state["state_sha256"]
        if state_sha256 != _state_snapshot_digest(state):
            raise VerificationError("state snapshot digest is internally inconsistent")
        git = state["git"]
        git_logical = {key: value for key, value in git.items() if key != "sha256"}
        if _sha256(git_logical) != git.get("sha256"):
            raise VerificationError("Git snapshot digest is internally inconsistent")
    except (KeyError, TypeError) as exc:
        raise VerificationError("state snapshot is malformed") from exc
    return _sha256({"state_sha256": state_sha256, "git_sha256": git["sha256"]})


def _create_baseline_git(
    archive: Path,
    *,
    authority_state_sha256: str,
    timestamp: str,
    hook: FaultHook | None,
) -> dict[str, Any]:
    _call_fault(hook, "before_baseline_git")
    if (archive / ".git").exists() or (archive / ".git").is_symlink():
        raise MigrationError("staged archive unexpectedly contains legacy Git metadata")
    _git_run(archive, "init", "-q", "-b", BASELINE_BRANCH)
    _call_fault(hook, "after_baseline_git_init")
    _git_run(archive, "add", "-f", "--all")
    _call_fault(hook, "after_baseline_git_add")
    message = (
        f"{BASELINE_COMMIT_SUBJECT}\n\n"
        f"Authority-Data-SHA256: {authority_state_sha256}"
    )
    identity = {
        "GIT_AUTHOR_NAME": BASELINE_AUTHOR_NAME,
        "GIT_AUTHOR_EMAIL": BASELINE_AUTHOR_EMAIL,
        "GIT_AUTHOR_DATE": timestamp,
        "GIT_COMMITTER_NAME": BASELINE_AUTHOR_NAME,
        "GIT_COMMITTER_EMAIL": BASELINE_AUTHOR_EMAIL,
        "GIT_COMMITTER_DATE": timestamp,
    }
    _git_run(
        archive,
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        message,
        extra_environment=identity,
    )
    _call_fault(hook, "after_baseline_git_commit")
    return _git_snapshot(archive, require_baseline=True)


def _call_fault(hook: FaultHook | None, phase: str) -> None:
    if phase not in MIGRATION_FAULT_PHASES:
        raise AssertionError(f"unenumerated migration fault seam: {phase}")
    if hook is not None:
        hook(phase)


def _copy_database(
    source: Path,
    destination: Path,
    hook: FaultHook | None,
) -> None:
    _call_fault(hook, "before_database_backup")
    source_container_before = _database_container_identity(source)
    descriptor = os.open(
        destination,
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    os.close(descriptor)
    destination_before = _database_file_identity(destination)
    source_connection: sqlite3.Connection | None = None
    destination_connection: sqlite3.Connection | None = None
    try:
        try:
            source_connection = sqlite3.connect(
                _database_uri(source, "ro"), uri=True, timeout=0
            )
            destination_connection = sqlite3.connect(
                _database_uri(destination, "rw"), uri=True
            )
        except sqlite3.Error as exc:
            raise MigrationError(
                f"cannot open SQLite source or staged destination: {exc}"
            ) from exc
        if _database_container_identity(source) != source_container_before:
            raise VerificationError(
                f"database parent changed while backup opened it: {source.parent}"
            )
        source_journal_mode = str(
            source_connection.execute("PRAGMA journal_mode").fetchone()[0]
        )
        source_schema_version = int(
            source_connection.execute("PRAGMA schema_version").fetchone()[0]
        )
        source_connection.backup(destination_connection)
        selected_mode = str(
            destination_connection.execute(
                f"PRAGMA journal_mode={source_journal_mode}"
            ).fetchone()[0]
        )
        if selected_mode.lower() != source_journal_mode.lower():
            raise MigrationError(
                "SQLite backup could not preserve journal_mode: "
                f"source={source_journal_mode!r}, destination={selected_mode!r}"
            )
        destination_connection.execute(
            f"PRAGMA schema_version={source_schema_version}"
        )
        destination_connection.commit()
    except sqlite3.Error as exc:
        raise MigrationError(f"SQLite backup failed: {exc}") from exc
    finally:
        if destination_connection is not None:
            destination_connection.close()
        if source_connection is not None:
            source_connection.close()
    if _database_container_identity(source) != source_container_before:
        raise VerificationError(
            f"database parent changed while it was backed up: {source.parent}"
        )
    destination_after = _database_file_identity(destination)
    if destination_before[:4] != destination_after[:4]:
        raise VerificationError(
            f"staged database changed identity while it was copied: {destination}"
        )
    _call_fault(hook, "after_database_backup")


def _copy_tree(
    source: Path,
    destination: Path,
    *,
    required: bool,
    hook: FaultHook | None,
    phase: str,
    excluded_root_names: frozenset[str] = frozenset(),
) -> None:
    if not source.exists():
        if required:
            raise MigrationError(f"required source directory is missing: {source}")
        return
    destination.mkdir(mode=0o700)
    directories: list[tuple[Path, Path]] = [(source, destination)]
    for path, relative in _tree_entries(
        source, excluded_root_names=excluded_root_names
    ):
        target = destination / relative
        if path.is_dir():
            target.mkdir(mode=0o700)
            directories.append((path, target))
            continue
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        _call_fault(hook, f"{phase}:before_file")
        before = path.lstat()
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise VerificationError(f"source file changed type before copy: {path}")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        opened = os.fstat(descriptor)
        comparable = (
            "st_dev",
            "st_ino",
            "st_mode",
            "st_nlink",
            "st_size",
            "st_mtime_ns",
            "st_ctime_ns",
        )
        if any(getattr(before, name) != getattr(opened, name) for name in comparable):
            os.close(descriptor)
            raise VerificationError(f"source file changed while it was opened: {path}")
        with os.fdopen(descriptor, "rb") as reader, target.open("xb") as writer:
            while chunk := reader.read(1024 * 1024):
                _call_fault(hook, f"{phase}:copy_chunk")
                writer.write(chunk)
            writer.flush()
            os.fsync(writer.fileno())
        after = path.lstat()
        if any(getattr(before, name) != getattr(after, name) for name in comparable):
            raise VerificationError(f"source file changed while it was copied: {path}")
        shutil.copystat(path, target, follow_symlinks=False)
    for source_directory, target_directory in reversed(directories):
        target_directory.chmod(stat.S_IMODE(source_directory.stat().st_mode))
    _call_fault(hook, f"after_{phase}")


def _fsync_tree(root: Path, hook: FaultHook | None) -> None:
    _call_fault(hook, "before_fsync")
    directories = [root]
    for path in root.rglob("*"):
        if path.is_dir() and not path.is_symlink():
            directories.append(path)
        elif path.is_file() and not path.is_symlink():
            with path.open("rb") as handle:
                os.fsync(handle.fileno())
    for directory in sorted(directories, key=lambda path: len(path.parts), reverse=True):
        descriptor = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    _call_fault(hook, "after_fsync")


def _write_manifest(staging: Path, payload: dict[str, Any]) -> None:
    path = staging / MANIFEST_NAME
    with path.open("xb") as handle:
        handle.write(_canonical_json(payload) + b"\n")
        handle.flush()
        os.fsync(handle.fileno())
    path.chmod(0o600)


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise MigrationError(f"ownership JSON contains duplicate key {key!r}")
        result[key] = value
    return result


def _read_owned_json(path: Path, *, label: str) -> Any:
    """Read one bounded, singly linked regular JSON file without following links."""

    _assert_no_symlink_components(path)
    flags = (
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise MigrationError(f"cannot open {label} {path}: {exc}") from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise MigrationError(f"{label} must be a singly linked regular file: {path}")
        if before.st_size > OWNERSHIP_JSON_MAX_BYTES:
            raise MigrationError(
                f"{label} exceeds {OWNERSHIP_JSON_MAX_BYTES} bytes: {path}"
            )
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            payload = handle.read(OWNERSHIP_JSON_MAX_BYTES + 1)
        if len(payload) > OWNERSHIP_JSON_MAX_BYTES:
            raise MigrationError(
                f"{label} exceeds {OWNERSHIP_JSON_MAX_BYTES} bytes: {path}"
            )
        after = os.fstat(descriptor)
        comparable = (
            "st_dev",
            "st_ino",
            "st_mode",
            "st_nlink",
            "st_size",
            "st_mtime_ns",
            "st_ctime_ns",
        )
        if any(getattr(before, field) != getattr(after, field) for field in comparable):
            raise MigrationError(f"{label} changed while it was read: {path}")
    finally:
        os.close(descriptor)
    try:
        return json.loads(
            payload.decode("utf-8"), object_pairs_hook=_unique_json_object
        )
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise MigrationError(f"cannot parse {label} {path}: {exc}") from exc


_REGULAR_FILE_IDENTITY_FIELDS: Final[tuple[str, ...]] = (
    "st_dev",
    "st_ino",
    "st_mode",
    "st_nlink",
    "st_size",
    "st_mtime_ns",
    "st_ctime_ns",
)


def _optional_regular_file_identity(path: Path, *, required: bool) -> tuple[int, ...] | None:
    """Return a no-follow identity, or ``None`` only for an optional absent file."""

    path = _absolute_without_symlinks(path)
    _assert_no_symlink_components(path)
    try:
        info = path.lstat()
    except FileNotFoundError:
        if required:
            raise VerificationError(f"required regular file is missing: {path}")
        return None
    if not stat.S_ISREG(info.st_mode):
        raise VerificationError(f"state file is not regular: {path}")
    if info.st_nlink != 1:
        raise VerificationError(f"hard-linked state files are not accepted: {path}")
    return tuple(int(getattr(info, field)) for field in _REGULAR_FILE_IDENTITY_FIELDS)


def _fingerprint_regular_file(path: Path, *, required: bool) -> dict[str, Any] | None:
    """Hash one stable regular file without following a link or trusting its name."""

    before = _optional_regular_file_identity(path, required=required)
    if before is None:
        return None
    descriptor = os.open(
        path,
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0),
    )
    try:
        opened = os.fstat(descriptor)
        opened_identity = tuple(
            int(getattr(opened, field)) for field in _REGULAR_FILE_IDENTITY_FIELDS
        )
        if opened_identity != before:
            raise VerificationError(f"state file changed while it was opened: {path}")
        digest = hashlib.sha256()
        while chunk := os.read(descriptor, 1024 * 1024):
            digest.update(chunk)
        after_open = os.fstat(descriptor)
        after_open_identity = tuple(
            int(getattr(after_open, field)) for field in _REGULAR_FILE_IDENTITY_FIELDS
        )
        if after_open_identity != before:
            raise VerificationError(f"state file changed while it was read: {path}")
    finally:
        os.close(descriptor)
    if _optional_regular_file_identity(path, required=True) != before:
        raise VerificationError(f"state file changed after it was read: {path}")
    return {
        "state": "PRESENT",
        "size": before[4],
        "mode": stat.S_IMODE(before[2]),
        "sha256": digest.hexdigest(),
    }


def _copy_regular_file_exact(source: Path, destination: Path) -> dict[str, Any]:
    """Create and fsync an exact regular-file copy, returning its fingerprint."""

    before = _optional_regular_file_identity(source, required=True)
    assert before is not None
    _assert_no_symlink_components(destination.parent)
    parent_info = destination.parent.lstat()
    if not stat.S_ISDIR(parent_info.st_mode) or stat.S_ISLNK(parent_info.st_mode):
        raise VerificationError(
            f"copy destination parent is not a real directory: {destination.parent}"
        )
    source_descriptor = os.open(
        source,
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0),
    )
    destination_descriptor: int | None = None
    created = False
    digest = hashlib.sha256()
    try:
        opened = os.fstat(source_descriptor)
        opened_identity = tuple(
            int(getattr(opened, field)) for field in _REGULAR_FILE_IDENTITY_FIELDS
        )
        if opened_identity != before:
            raise VerificationError(f"source file changed while it was opened: {source}")
        destination_descriptor = os.open(
            destination,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        created = True
        while chunk := os.read(source_descriptor, 1024 * 1024):
            digest.update(chunk)
            view = memoryview(chunk)
            while view:
                written = os.write(destination_descriptor, view)
                if written <= 0:
                    raise OSError("short write while copying cold database file")
                view = view[written:]
        os.fchmod(destination_descriptor, stat.S_IMODE(before[2]))
        os.fsync(destination_descriptor)
        after_open = os.fstat(source_descriptor)
        after_open_identity = tuple(
            int(getattr(after_open, field)) for field in _REGULAR_FILE_IDENTITY_FIELDS
        )
        if after_open_identity != before:
            raise VerificationError(f"source file changed while it was copied: {source}")
    except Exception:
        if destination_descriptor is not None:
            os.close(destination_descriptor)
            destination_descriptor = None
        if created:
            try:
                destination.unlink()
            except FileNotFoundError:
                pass
        raise
    finally:
        if destination_descriptor is not None:
            os.close(destination_descriptor)
        os.close(source_descriptor)
    if _optional_regular_file_identity(source, required=True) != before:
        destination.unlink(missing_ok=True)
        raise VerificationError(f"source file changed after it was copied: {source}")
    copied = _fingerprint_regular_file(destination, required=True)
    assert copied is not None
    expected = {
        "state": "PRESENT",
        "size": before[4],
        "mode": stat.S_IMODE(before[2]),
        "sha256": digest.hexdigest(),
    }
    if copied != expected:
        destination.unlink(missing_ok=True)
        raise VerificationError(f"cold file copy does not match its source: {source}")
    return copied


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_json_exclusive(path: Path, payload: dict[str, Any]) -> None:
    """Create one canonical receipt and fsync both it and its parent directory."""

    _assert_no_symlink_components(path)
    if not path.parent.is_dir() or path.parent.is_symlink():
        raise MigrationError(f"receipt parent must be a real existing directory: {path.parent}")
    descriptor: int | None = None
    created = False
    try:
        descriptor = os.open(
            path,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        created = True
        content = _canonical_json(payload) + b"\n"
        view = memoryview(content)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short write while writing migration receipt")
            view = view[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        _fsync_directory(path.parent)
    except Exception:
        if descriptor is not None:
            os.close(descriptor)
        if created:
            try:
                path.unlink()
                _fsync_directory(path.parent)
            except OSError:
                pass
        raise


def _replace_json_fsynced(path: Path, payload: dict[str, Any]) -> None:
    """Atomically replace an owned JSON marker and fsync the containing directory."""

    temporary = path.parent / f".{path.name}.{uuid.uuid4()}.next"
    try:
        _write_json_exclusive(temporary, payload)
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    except Exception:
        if temporary.exists() and not temporary.is_symlink():
            temporary.unlink()
            try:
                _fsync_directory(temporary.parent)
            except OSError:
                pass
        raise


def _database_family_paths(database: Path) -> dict[str, Path]:
    return {
        "main": database,
        "wal": Path(f"{database}-wal"),
        "shm": Path(f"{database}-shm"),
    }


def _database_family_identities(paths: dict[str, Path]) -> dict[str, tuple[int, ...] | None]:
    return {
        role: _optional_regular_file_identity(path, required=role == "main")
        for role, path in paths.items()
    }


def _database_family_fingerprints(paths: dict[str, Path]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for role, path in paths.items():
        fingerprint = _fingerprint_regular_file(path, required=False)
        result[role] = fingerprint if fingerprint is not None else {"state": "ABSENT"}
    return result


def _generation_digest(records: dict[str, dict[str, Any]]) -> str:
    comparable = {
        role: {
            key: record[key]
            for key in ("state", "size", "mode", "sha256")
            if key in record
        }
        for role, record in sorted(records.items())
    }
    return _sha256(comparable)


def _path_within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _record_matches_fingerprint(record: dict[str, Any], actual: dict[str, Any]) -> bool:
    return all(record.get(key) == actual.get(key) for key in ("state", "size", "mode", "sha256"))


def _logical_snapshot_from_raw_family(
    paths: dict[str, Path],
    records: dict[str, dict[str, Any]],
    *,
    scratch_parent: Path,
) -> dict[str, Any]:
    """Validate raw bytes through a disposable SQLite family, never the sealed files."""

    scratch = scratch_parent / f".cold-logical-validation-{uuid.uuid4()}"
    scratch.mkdir(mode=0o700)
    scratch_paths = _database_family_paths(scratch / "storage.sqlite3")
    try:
        for role, source in paths.items():
            record = records[role]
            if record["state"] == "ABSENT":
                continue
            copied = _copy_regular_file_exact(source, scratch_paths[role])
            if not _record_matches_fingerprint(record, copied):
                raise VerificationError(
                    f"logical validation input differs from sealed {role} backup"
                )
        return snapshot_database(scratch_paths["main"])
    finally:
        shutil.rmtree(scratch)
        _fsync_directory(scratch_parent)


def cold_backup_database(
    source_database: Path,
    backup_directory: Path,
    *,
    services_stopped: bool,
) -> dict[str, Any]:
    """Seal a raw SQLite family only while both mail authorities are stopped."""

    if not services_stopped:
        raise MigrationError("cold-backup requires caller assertion --services-stopped")
    source_database = _absolute_without_symlinks(source_database)
    backup_directory = _absolute_without_symlinks(backup_directory)
    _assert_no_symlink_components(source_database)
    _assert_no_symlink_components(backup_directory)
    if backup_directory.exists() or backup_directory.is_symlink():
        raise MigrationError(f"cold backup destination must be absent: {backup_directory}")
    if not backup_directory.parent.is_dir() or backup_directory.parent.is_symlink():
        raise MigrationError(
            f"cold backup parent must be a real existing directory: {backup_directory.parent}"
        )
    if _path_within(backup_directory, source_database.parent):
        raise MigrationError("cold backup must not stage inside the source database directory")
    source_paths = _database_family_paths(source_database)
    before = _database_family_identities(source_paths)
    operation_id = str(uuid.uuid4())
    staging = backup_directory.parent / f".{backup_directory.name}.cold-backup-{operation_id}"
    unconfirmed = (
        backup_directory.parent
        / f".{backup_directory.name}.cold-backup-{operation_id}.unconfirmed"
    )
    published = False
    staging.mkdir(mode=0o700)
    try:
        records: dict[str, dict[str, Any]] = {}
        staged_paths: dict[str, Path] = {}
        for role, source in source_paths.items():
            backup_name = COLD_BACKUP_FILE_NAMES[role]
            target = staging / backup_name
            staged_paths[role] = target
            if before[role] is None:
                records[role] = {"state": "ABSENT", "backup_name": backup_name}
                continue
            copied = _copy_regular_file_exact(source, target)
            records[role] = {**copied, "backup_name": backup_name}
        if _database_family_identities(source_paths) != before:
            raise VerificationError(
                "SQLite main/WAL/SHM family changed while the cold backup was assembled"
            )
        logical_snapshot = _logical_snapshot_from_raw_family(
            staged_paths,
            records,
            scratch_parent=staging,
        )
        receipt = {
            "schema_version": 1,
            "tool": "agentstack-mail-migrate",
            "kind": "cold-backup",
            "operation_id": operation_id,
            "created_at": _utc_now(),
            "source_database": str(source_database),
            "services_stopped": {
                "asserted": True,
                "provenance": "caller_asserted_unverified",
            },
            "files": records,
            "logical_snapshot": logical_snapshot,
            "logical_sha256": logical_snapshot["logical_sha256"],
        }
        _write_json_exclusive(staging / COLD_BACKUP_RECEIPT_NAME, receipt)
        _fsync_directory(staging)
        if backup_directory.exists() or backup_directory.is_symlink():
            raise MigrationError(
                f"cold backup destination appeared before publish: {backup_directory}"
            )
        os.replace(staging, backup_directory)
        published = True
        _fsync_directory(backup_directory.parent)
        receipt_fingerprint = _fingerprint_regular_file(
            backup_directory / COLD_BACKUP_RECEIPT_NAME, required=True
        )
        assert receipt_fingerprint is not None
        return {
            "status": "backed_up",
            "operation_id": operation_id,
            "backup_directory": str(backup_directory),
            "receipt": str(backup_directory / COLD_BACKUP_RECEIPT_NAME),
            "receipt_sha256": receipt_fingerprint["sha256"],
            "logical_sha256": logical_snapshot["logical_sha256"],
        }
    except Exception:
        if staging.exists():
            shutil.rmtree(staging)
        if published and backup_directory.exists():
            os.replace(backup_directory, unconfirmed)
            try:
                _fsync_directory(backup_directory.parent)
            except OSError:
                pass
        raise


def _load_cold_backup_receipt(backup_directory: Path) -> dict[str, Any]:
    receipt_path = backup_directory / COLD_BACKUP_RECEIPT_NAME
    payload = _read_owned_json(receipt_path, label="cold backup receipt")
    expected_keys = {
        "schema_version",
        "tool",
        "kind",
        "operation_id",
        "created_at",
        "source_database",
        "services_stopped",
        "files",
        "logical_snapshot",
        "logical_sha256",
    }
    if not isinstance(payload, dict) or set(payload) != expected_keys:
        raise MigrationError("cold backup receipt has an unexpected shape")
    if type(payload["schema_version"]) is not int or payload["schema_version"] != 1:
        raise MigrationError("cold backup receipt has an unsupported schema version")
    if payload["tool"] != "agentstack-mail-migrate" or payload["kind"] != "cold-backup":
        raise MigrationError("cold backup receipt has an unexpected producer or kind")
    _canonical_operation_id(payload["operation_id"], label="cold backup receipt")
    if not isinstance(payload["created_at"], str):
        raise MigrationError("cold backup receipt created_at is malformed")
    _canonical_absolute_path(payload["source_database"], label="cold backup source_database")
    if payload["services_stopped"] != {
        "asserted": True,
        "provenance": "caller_asserted_unverified",
    }:
        raise MigrationError("cold backup receipt lacks the services-stopped assertion")
    records = payload["files"]
    if not isinstance(records, dict) or set(records) != set(COLD_BACKUP_FILE_NAMES):
        raise MigrationError("cold backup receipt file inventory is malformed")
    for role, backup_name in COLD_BACKUP_FILE_NAMES.items():
        record = records[role]
        if not isinstance(record, dict) or record.get("backup_name") != backup_name:
            raise MigrationError(f"cold backup receipt {role} record is malformed")
        if record.get("state") == "ABSENT":
            if set(record) != {"state", "backup_name"} or role == "main":
                raise MigrationError(f"cold backup receipt {role} absence is invalid")
            if (backup_directory / backup_name).exists() or (
                backup_directory / backup_name
            ).is_symlink():
                raise MigrationError(f"cold backup {role} is present but receipt says ABSENT")
            continue
        if set(record) != {"state", "backup_name", "size", "mode", "sha256"}:
            raise MigrationError(f"cold backup receipt {role} record is malformed")
        if (
            record.get("state") != "PRESENT"
            or type(record.get("size")) is not int
            or record["size"] < 0
            or type(record.get("mode")) is not int
            or not isinstance(record.get("sha256"), str)
            or len(record["sha256"]) != 64
        ):
            raise MigrationError(f"cold backup receipt {role} fingerprint is malformed")
        actual = _fingerprint_regular_file(backup_directory / backup_name, required=True)
        assert actual is not None
        if not _record_matches_fingerprint(record, actual):
            raise VerificationError(f"cold backup {role} bytes do not match its receipt")
    expected_entries = {COLD_BACKUP_RECEIPT_NAME} | {
        record["backup_name"]
        for record in records.values()
        if record["state"] == "PRESENT"
    }
    actual_entries = {entry.name for entry in os.scandir(backup_directory)}
    if actual_entries != expected_entries:
        raise MigrationError(
            "cold backup directory does not match its exact file allowlist: "
            f"expected={sorted(expected_entries)}, actual={sorted(actual_entries)}"
        )
    logical = payload["logical_snapshot"]
    if not isinstance(logical, dict) or set(logical) != {
        "schema_sha256",
        "pragmas",
        "tables",
        "relations",
        "logical_sha256",
    }:
        raise MigrationError("cold backup logical snapshot is malformed")
    logical_core = {key: value for key, value in logical.items() if key != "logical_sha256"}
    if (
        _sha256(logical_core) != logical.get("logical_sha256")
        or payload["logical_sha256"] != logical.get("logical_sha256")
    ):
        raise MigrationError("cold backup logical snapshot digest is inconsistent")
    return payload


def cold_restore_database(
    backup_directory: Path,
    destination_database: Path,
    restore_receipt: Path,
    migration_manifest: Path,
    *,
    services_stopped: bool,
    target_kind: str,
    fault_injection: str,
) -> dict[str, Any]:
    """Restore a sealed raw SQLite family, then accept it by logical equality."""

    if not services_stopped:
        raise MigrationError("cold-restore requires caller assertion --services-stopped")
    if target_kind not in {"rehearsal-copy", "production-source"}:
        raise MigrationError("cold-restore target kind is invalid")
    if target_kind == "rehearsal-copy" and fault_injection in {"", "none"}:
        raise MigrationError("rehearsal-copy restore requires an asserted fault injection")
    backup_directory = _absolute_without_symlinks(backup_directory)
    destination_database = _absolute_without_symlinks(destination_database)
    restore_receipt = _absolute_without_symlinks(restore_receipt)
    migration_manifest = _absolute_without_symlinks(migration_manifest)
    _assert_no_symlink_components(backup_directory)
    _assert_no_symlink_components(destination_database)
    _assert_no_symlink_components(restore_receipt)
    _assert_no_symlink_components(migration_manifest)
    if not backup_directory.is_dir() or backup_directory.is_symlink():
        raise MigrationError(f"cold backup is not a real directory: {backup_directory}")
    if (
        not destination_database.parent.is_dir()
        or destination_database.parent.is_symlink()
    ):
        raise MigrationError(
            "restore destination parent must be a real existing directory: "
            f"{destination_database.parent}"
        )
    if restore_receipt.exists() or restore_receipt.is_symlink():
        raise MigrationError(f"restore receipt destination must be absent: {restore_receipt}")
    if not restore_receipt.parent.is_dir() or restore_receipt.parent.is_symlink():
        raise MigrationError(
            f"restore receipt parent must be a real existing directory: {restore_receipt.parent}"
        )
    backup_receipt_path = backup_directory / COLD_BACKUP_RECEIPT_NAME
    backup_receipt_fingerprint = _fingerprint_regular_file(
        backup_receipt_path, required=True
    )
    assert backup_receipt_fingerprint is not None
    backup = _load_cold_backup_receipt(backup_directory)
    if _fingerprint_regular_file(backup_receipt_path, required=True) != backup_receipt_fingerprint:
        raise MigrationError("cold backup receipt changed while restore validated it")
    manifest_fingerprint = _fingerprint_regular_file(
        migration_manifest, required=True
    )
    assert manifest_fingerprint is not None
    manifest = _load_manifest(migration_manifest)
    if _fingerprint_regular_file(migration_manifest, required=True) != manifest_fingerprint:
        raise MigrationError("migration manifest changed while restore validated it")
    recorded_source = Path(backup["source_database"])
    is_recorded_source = destination_database == recorded_source
    if target_kind == "rehearsal-copy" and is_recorded_source:
        raise MigrationError("rehearsal restore cannot target the recorded production source")
    if target_kind == "production-source" and not is_recorded_source:
        raise MigrationError("production-source restore must target the recorded source path")
    if manifest["source"]["database"] != backup["source_database"]:
        raise MigrationError("migration manifest and cold backup name different source databases")
    baseline = manifest["baseline"]
    if baseline.get("state_sha256") != _state_snapshot_digest(baseline):
        raise MigrationError("migration manifest baseline state digest is inconsistent")
    if baseline.get("snapshot_sha256") != _snapshot_digest(baseline):
        raise MigrationError("migration manifest baseline snapshot digest is inconsistent")
    if manifest["baseline"]["database"] != backup["logical_snapshot"]:
        raise VerificationError(
            "migration manifest database baseline does not match the cold backup"
        )
    records: dict[str, dict[str, Any]] = backup["files"]
    destination_paths = _database_family_paths(destination_database)
    manifest_source = manifest["source"]
    protected_authority_roots = {
        Path(manifest_source["database"]).parent,
        Path(manifest_source["archive"]),
        Path(manifest_source["signals"]),
        Path(manifest["destination_root"]),
        backup_directory,
    }
    if migration_manifest in destination_paths.values():
        raise MigrationError("restore target must not replace its migration manifest")
    if target_kind == "rehearsal-copy":
        for candidate in (*destination_paths.values(), restore_receipt):
            if candidate == migration_manifest or any(
                _path_within(candidate, root) for root in protected_authority_roots
            ):
                raise MigrationError(
                    "rehearsal target and receipt must be outside every recorded authority surface"
                )
    elif any(
        _path_within(restore_receipt, root) for root in protected_authority_roots
    ):
        raise MigrationError(
            "production restore receipt must be outside every recorded authority surface"
        )
    protected_paths = {
        *destination_paths.values(),
        *(backup_directory / name for name in COLD_BACKUP_FILE_NAMES.values()),
        backup_directory / COLD_BACKUP_RECEIPT_NAME,
    }
    if restore_receipt in protected_paths:
        raise MigrationError("restore receipt must not replace a database family file")
    if _path_within(destination_database, backup_directory) or _path_within(
        restore_receipt, backup_directory
    ):
        raise MigrationError("restore target and receipt must be outside the cold backup")

    backup_paths = {
        role: backup_directory / record["backup_name"]
        for role, record in records.items()
    }
    sealed_logical = _logical_snapshot_from_raw_family(
        backup_paths,
        records,
        scratch_parent=backup_directory.parent,
    )
    if sealed_logical != backup["logical_snapshot"]:
        raise VerificationError("sealed cold backup no longer matches its logical receipt")
    pre_restore_fingerprints = _database_family_fingerprints(destination_paths)
    backup_generation_sha256 = _generation_digest(records)
    pre_restore_generation_sha256 = _generation_digest(pre_restore_fingerprints)
    observed_divergence = pre_restore_generation_sha256 != backup_generation_sha256
    if target_kind == "rehearsal-copy" and not observed_divergence:
        raise MigrationError(
            "rehearsal restore requires observed target divergence from the cold backup"
        )

    operation_id = str(uuid.uuid4())
    staging = (
        destination_database.parent
        / f".{destination_database.name}.cold-restore-{operation_id}"
    )
    prepared_receipt = (
        restore_receipt.parent
        / f".{restore_receipt.name}.cold-restore-{operation_id}.prepared"
    )
    if prepared_receipt.exists() or prepared_receipt.is_symlink():
        raise MigrationError(f"prepared restore receipt already exists: {prepared_receipt}")
    outcomes: dict[str, dict[str, str]] = {}
    target_mutation_started = False
    terminal_receipt_published = False
    try:
        staging.mkdir(mode=0o700)
        marker_payload = {
            "schema_version": 1,
            "tool": "agentstack-mail-migrate",
            "kind": "cold-restore-ownership",
            "operation_id": operation_id,
            "phase": "STAGED_BEFORE_TARGET_RENAME",
            "target_database": str(destination_database),
            "target_kind": target_kind,
            "backup_receipt": str(backup_receipt_path),
            "backup_receipt_sha256": backup_receipt_fingerprint["sha256"],
            "backup_generation_sha256": backup_generation_sha256,
            "migration_manifest": str(migration_manifest),
            "migration_manifest_sha256": manifest_fingerprint["sha256"],
            "terminal_receipt": str(restore_receipt),
        }
        _write_json_exclusive(
            staging / COLD_RESTORE_MARKER_NAME,
            marker_payload,
        )
        staged_paths: dict[str, Path] = {}
        for role, backup_name in COLD_BACKUP_FILE_NAMES.items():
            record = records[role]
            if record["state"] == "ABSENT":
                continue
            target = staging / backup_name
            staged_paths[role] = target
            copied = _copy_regular_file_exact(backup_directory / backup_name, target)
            if not _record_matches_fingerprint(record, copied):
                raise VerificationError(f"staged restore {role} does not match backup receipt")

        if _database_family_fingerprints(destination_paths) != pre_restore_fingerprints:
            raise VerificationError("restore target changed during preflight staging")

        existing = {
            role: _optional_regular_file_identity(path, required=False)
            for role, path in destination_paths.items()
        }
        marker_payload = {
            **marker_payload,
            "phase": "TARGET_MUTATION_MAY_HAVE_STARTED",
        }
        _replace_json_fsynced(
            staging / COLD_RESTORE_MARKER_NAME, marker_payload
        )
        for role in ("wal", "shm"):
            record = records[role]
            target = destination_paths[role]
            if record["state"] == "PRESENT":
                target_mutation_started = True
                os.replace(staged_paths[role], target)
                _fsync_directory(destination_database.parent)
                outcomes[role] = {
                    "expected_state": "PRESENT",
                    "result": "atomic_replace",
                }
            elif existing[role] is not None:
                quarantine = staging / f"quarantine-{role}"
                target_mutation_started = True
                os.replace(target, quarantine)
                _fsync_directory(destination_database.parent)
                outcomes[role] = {
                    "expected_state": "ABSENT",
                    "result": "atomic_quarantine_remove",
                }
            else:
                outcomes[role] = {
                    "expected_state": "ABSENT",
                    "result": "already_absent",
                }
        target_mutation_started = True
        os.replace(staged_paths["main"], destination_paths["main"])
        _fsync_directory(destination_database.parent)
        outcomes["main"] = {
            "expected_state": "PRESENT",
            "result": "atomic_replace",
        }
        marker_payload = {
            **marker_payload,
            "phase": "TARGET_FAMILY_REPLACED_AWAITING_VALIDATION",
        }
        _replace_json_fsynced(
            staging / COLD_RESTORE_MARKER_NAME, marker_payload
        )
        post_restore_fingerprints = _database_family_fingerprints(destination_paths)
        for role, path in destination_paths.items():
            record = records[role]
            actual = post_restore_fingerprints[role]
            if record["state"] == "ABSENT":
                if actual["state"] != "ABSENT":
                    raise VerificationError(f"restored {role} exists but backup records ABSENT")
            else:
                if not _record_matches_fingerprint(record, actual):
                    raise VerificationError(f"restored {role} bytes do not match backup receipt")
        logical = _logical_snapshot_from_raw_family(
            destination_paths,
            records,
            scratch_parent=destination_database.parent,
        )
        if logical != backup["logical_snapshot"]:
            raise VerificationError(
                "restored database failed logical schema/rows/relations/PRAGMA equality"
            )
        post_logical_fingerprints = _database_family_fingerprints(destination_paths)
        if post_logical_fingerprints != post_restore_fingerprints:
            raise VerificationError(
                "restored physical database family changed during logical validation"
            )
        marker_payload = {
            **marker_payload,
            "phase": "TARGET_VALIDATED_AWAITING_RECEIPT_PUBLICATION",
        }
        _replace_json_fsynced(
            staging / COLD_RESTORE_MARKER_NAME, marker_payload
        )
        receipt = {
            "schema_version": 1,
            "tool": "agentstack-mail-migrate",
            "kind": "cold-restore",
            "operation_id": operation_id,
            "created_at": _utc_now(),
            "target": {
                "kind": target_kind,
                "database": str(destination_database),
                "production_source": is_recorded_source,
            },
            "services_stopped": {
                "asserted": True,
                "provenance": "caller_asserted_unverified",
            },
            "backup_identity": {
                "operation_id": backup["operation_id"],
                "receipt": str(backup_receipt_path),
                "receipt_sha256": backup_receipt_fingerprint["sha256"],
                "logical_sha256": backup["logical_sha256"],
            },
            "migration_identity": {
                "manifest": str(migration_manifest),
                "operation_id": manifest["operation_id"],
                "manifest_sha256": manifest_fingerprint["sha256"],
                "baseline_state_sha256": manifest["baseline"]["state_sha256"],
                "baseline_snapshot_sha256": manifest["baseline"]["snapshot_sha256"],
            },
            "fault_injection": {
                "description": fault_injection,
                "observed": observed_divergence,
                "pre_restore_generation_sha256": pre_restore_generation_sha256,
                "backup_generation_sha256": backup_generation_sha256,
                "provenance": "observed_file_divergence",
            },
            "physical_validator": {
                "collection_order": (
                    "post_restore_before_disposable_scratch_logical_validation"
                ),
                "backup_expected": records,
                "pre_restore": pre_restore_fingerprints,
                "post_restore_before_logical": post_restore_fingerprints,
                "post_logical": post_logical_fingerprints,
                "status": "matched",
            },
            "restore_result": {"status": "restored", "files": outcomes},
            "logical_validator": {
                "status": "matched",
                "comparison": "schema_rows_relations_pragmas",
                "logical_sha256": logical["logical_sha256"],
            },
        }
        _write_json_exclusive(prepared_receipt, receipt)
        shutil.rmtree(staging)
        _fsync_directory(destination_database.parent)
        if restore_receipt.exists() or restore_receipt.is_symlink():
            raise MigrationError(
                f"restore receipt destination appeared before publication: {restore_receipt}"
            )
        os.replace(prepared_receipt, restore_receipt)
        terminal_receipt_published = True
        _fsync_directory(restore_receipt.parent)
        return {
            "status": "restored",
            "operation_id": operation_id,
            "restore_receipt": str(restore_receipt),
            "logical_sha256": logical["logical_sha256"],
        }
    except Exception:
        # Before the first target rename the staging directory is disposable.
        # Afterwards it may hold quarantined pre-restore sidecars, so retain it
        # as incident evidence; a terminal receipt is never written on failure.
        if staging.exists() and not target_mutation_started:
            shutil.rmtree(staging)
        if prepared_receipt.exists() and not target_mutation_started:
            prepared_receipt.unlink()
            _fsync_directory(prepared_receipt.parent)
        if terminal_receipt_published and restore_receipt.exists():
            unconfirmed = (
                restore_receipt.parent
                / f".{restore_receipt.name}.cold-restore-{operation_id}.unconfirmed"
            )
            os.replace(restore_receipt, unconfirmed)
            try:
                _fsync_directory(restore_receipt.parent)
            except OSError:
                pass
        raise


def _artifact_descriptor(paths: dict[str, Path]) -> dict[str, Any]:
    files: dict[str, dict[str, Any]] = {}
    core: dict[str, dict[str, Any]] = {}
    for role, path in paths.items():
        fingerprint = _fingerprint_regular_file(path, required=role == "main")
        record = fingerprint if fingerprint is not None else {"state": "ABSENT"}
        core[role] = record
        files[role] = {**record, "path": str(path)}
    return {
        "directory": str(paths["main"].parent),
        "files": files,
        "family_sha256": _generation_digest(core),
    }


def _artifact_core_records(descriptor: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        role: {
            key: record[key]
            for key in ("state", "size", "mode", "sha256")
            if key in record
        }
        for role, record in descriptor["files"].items()
    }


def _artifact_paths(descriptor: dict[str, Any]) -> dict[str, Path]:
    return {
        role: Path(record["path"])
        for role, record in descriptor["files"].items()
    }


def _copy_database_family_artifact(
    source_paths: dict[str, Path], destination_directory: Path
) -> dict[str, Any]:
    if destination_directory.exists() or destination_directory.is_symlink():
        raise MigrationError(
            f"raw artifact destination must be absent: {destination_directory}"
        )
    if (
        not destination_directory.parent.is_dir()
        or destination_directory.parent.is_symlink()
    ):
        raise MigrationError(
            "raw artifact parent must be a real existing directory: "
            f"{destination_directory.parent}"
        )
    source_before = _database_family_identities(source_paths)
    destination_directory.mkdir(mode=0o700)
    destination_paths = _database_family_paths(
        destination_directory / COLD_BACKUP_FILE_NAMES["main"]
    )
    try:
        for role, source in source_paths.items():
            fingerprint = _fingerprint_regular_file(source, required=role == "main")
            if fingerprint is None:
                continue
            copied = _copy_regular_file_exact(source, destination_paths[role])
            if not _record_matches_fingerprint(fingerprint, copied):
                raise VerificationError(
                    f"raw {role} artifact does not match its source"
                )
        if _database_family_identities(source_paths) != source_before:
            raise VerificationError(
                "SQLite main/WAL/SHM family changed while raw artifact was assembled"
            )
        _fsync_directory(destination_directory)
        _fsync_directory(destination_directory.parent)
        return _artifact_descriptor(destination_paths)
    except Exception:
        # This directory is owned by the current rehearsal and has not been
        # published as a completed artifact. Preserve it only when its caller
        # owns the surrounding failed-run directory and needs forensic state.
        raise


def _logical_artifact_result(
    descriptor: dict[str, Any], *, scratch_parent: Path
) -> dict[str, Any]:
    try:
        snapshot = _logical_snapshot_from_raw_family(
            _artifact_paths(descriptor),
            _artifact_core_records(descriptor),
            scratch_parent=scratch_parent,
        )
    except MigrationError as exc:
        return {
            "status": "error",
            "error_type": type(exc).__name__,
            "error": str(exc),
        }
    return {
        "status": "valid",
        "logical_sha256": snapshot["logical_sha256"],
        "snapshot": snapshot,
    }


def _damage_rehearsal_target(
    target_paths: dict[str, Path], backup_records: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    """Apply the one fixed rehearsal fault and return what this call changed."""

    observed_before = _database_family_fingerprints(target_paths)
    absent_sidecars = [
        role
        for role in ("wal", "shm")
        if backup_records[role]["state"] == "ABSENT"
    ]
    if not absent_sidecars:
        raise MigrationError(
            "rehearsal seed must have at least one ABSENT sidecar to exercise removal"
        )
    main_before = _optional_regular_file_identity(target_paths["main"], required=True)
    assert main_before is not None
    descriptor = os.open(
        target_paths["main"],
        os.O_WRONLY | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
            raise VerificationError("rehearsal main target is not a singly linked file")
        payload = (
            b"agentstack-mail deliberate cold-restore rehearsal corruption\n" * 64
        )
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short write while damaging rehearsal main database")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    _fsync_directory(target_paths["main"].parent)

    created: list[str] = []
    for role in absent_sidecars:
        path = target_paths[role]
        sidecar_descriptor = os.open(
            path,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            content = f"unexpected-{role}-for-rehearsal\n".encode("ascii")
            if os.write(sidecar_descriptor, content) != len(content):
                raise OSError("short write while creating rehearsal sidecar")
            os.fsync(sidecar_descriptor)
        finally:
            os.close(sidecar_descriptor)
        _fsync_directory(path.parent)
        created.append(role)
    observed_after = _database_family_fingerprints(target_paths)
    return {
        "plan": COLD_REHEARSAL_DAMAGE_PLAN,
        "main_action": "truncate_and_replace_content",
        "created_absent_sidecars": created,
        "observed_before_physical": observed_before,
        "observed_after_physical": observed_after,
    }


def _validate_candidate_commit(candidate_commit: str) -> str:
    if (
        re.fullmatch(r"[0-9a-f]{40}", candidate_commit) is None
        or candidate_commit == "0" * 40
    ):
        raise MigrationError("candidate commit must be one full lowercase SHA-1")
    return candidate_commit


def _parse_utc_timestamp(value: Any, *, label: str) -> datetime:
    if not isinstance(value, str):
        raise VerificationError(f"{label} must be an RFC3339 UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise VerificationError(f"{label} must be an RFC3339 UTC timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise VerificationError(f"{label} must be an RFC3339 UTC timestamp")
    return parsed


def _candidate_checkout_identity(
    repository: Path, candidate_commit: str
) -> dict[str, Any]:
    repository = _absolute_without_symlinks(repository)
    _assert_no_symlink_components(repository)
    if not repository.is_dir() or repository.is_symlink():
        raise MigrationError(f"candidate repository is not a real directory: {repository}")
    head = _git_run(repository, "rev-parse", "--verify", "HEAD^{commit}").stdout.strip()
    resolved = _git_run(
        repository, "rev-parse", "--verify", f"{candidate_commit}^{{commit}}"
    ).stdout.strip()
    if head != candidate_commit or resolved != candidate_commit:
        raise MigrationError("candidate commit must be the exact checkout HEAD")
    dirty = _git_run(
        repository, "status", "--porcelain", "--untracked-files=no"
    ).stdout
    if dirty:
        raise MigrationError("candidate repository must have no tracked working-tree changes")
    module_relative = Path(
        "packages/agentstack_mail/src/agentstack_mail/migration.py"
    )
    module_path = Path(__file__).resolve()
    candidate_module = subprocess.run(
        [
            "git",
            "-c",
            f"core.hooksPath={os.devnull}",
            "-C",
            str(repository),
            "show",
            f"{candidate_commit}:{module_relative.as_posix()}",
        ],
        check=False,
        capture_output=True,
        env=_git_environment(),
        timeout=GIT_TIMEOUT_SECONDS,
    )
    if candidate_module.returncode != 0 or candidate_module.stdout != module_path.read_bytes():
        raise MigrationError(
            "executing migration.py bytes must equal the candidate commit blob"
        )
    return {
        "repository": str(repository),
        "head": head,
        "tracked_worktree_clean": True,
        "migration_module": module_relative.as_posix(),
        "migration_module_sha256": hashlib.sha256(module_path.read_bytes()).hexdigest(),
    }


def _load_seed_provenance(
    provenance_path: Path,
    *,
    seed_database: Path,
    production_source_database: Path,
) -> tuple[dict[str, Any], dict[str, Any]]:
    fingerprint = _fingerprint_regular_file(provenance_path, required=True)
    assert fingerprint is not None
    payload = _read_owned_json(provenance_path, label="rehearsal seed provenance")
    if not isinstance(payload, dict) or set(payload) != {
        "schema_version",
        "kind",
        "created_at",
        "seed_database",
        "production_source_database",
        "acquisition_method",
        "source_reference",
    }:
        raise MigrationError("rehearsal seed provenance has an unexpected shape")
    if payload["schema_version"] != 1 or payload["kind"] != "production-shaped-synthetic":
        raise MigrationError("rehearsal seed provenance kind is invalid")
    if (
        payload["seed_database"] != str(seed_database)
        or payload["production_source_database"] != str(production_source_database)
        or not isinstance(payload["acquisition_method"], str)
        or not payload["acquisition_method"]
        or not isinstance(payload["source_reference"], str)
        or not payload["source_reference"]
    ):
        raise MigrationError("rehearsal seed provenance does not bind the supplied paths")
    _parse_utc_timestamp(payload["created_at"], label="seed provenance created_at")
    if _fingerprint_regular_file(provenance_path, required=True) != fingerprint:
        raise MigrationError("rehearsal seed provenance changed while it was read")
    return payload, fingerprint


def _load_rehearsal_generator_receipt(
    receipt_path: Path,
    *,
    expected_sha256: str,
    seed_database: Path,
    production_source_database: Path,
    seed_provenance: Path,
    provenance_fingerprint: dict[str, Any],
    candidate_repository: Path,
    candidate_commit: str,
    candidate_checkout: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    if re.fullmatch(r"[0-9a-f]{64}", expected_sha256) is None:
        raise MigrationError("expected generator receipt SHA-256 is malformed")
    expected_path = seed_database.parent.parent / REHEARSAL_GENERATOR_RECEIPT_NAME
    if receipt_path != expected_path:
        raise MigrationError("generator receipt must use the canonical seed-root path")
    fingerprint = _fingerprint_regular_file(receipt_path, required=True)
    assert fingerprint is not None
    if fingerprint["sha256"] != expected_sha256:
        raise VerificationError("generator receipt differs from its out-of-band SHA-256 pin")
    payload = _read_owned_json(receipt_path, label="rehearsal seed generator receipt")
    required = {
        "schema_version",
        "kind",
        "run_id",
        "started_at",
        "completed_at",
        "candidate_commit",
        "candidate_checkout",
        "output_root",
        "production_source_database",
        "production_source_opened",
        "seed_database",
        "seed_database_size",
        "seed_database_sha256",
        "seed_database_family",
        "seed_archive",
        "seed_signals",
        "major_table_rows",
        "seed_provenance",
        "seed_provenance_sha256",
        "scale_floor",
    }
    if not isinstance(payload, dict) or set(payload) != required:
        raise MigrationError("rehearsal seed generator receipt has an unexpected shape")
    if (
        payload["schema_version"] != 1
        or payload["kind"] != "production-shaped-synthetic-seed-generation"
    ):
        raise MigrationError("rehearsal seed generator receipt schema/kind is invalid")
    _canonical_operation_id(payload["run_id"], label="generator receipt")
    started = _parse_utc_timestamp(payload["started_at"], label="generator started_at")
    completed = _parse_utc_timestamp(
        payload["completed_at"], label="generator completed_at"
    )
    if completed < started:
        raise VerificationError("rehearsal seed generation completed before it started")
    if (
        payload["candidate_commit"] != candidate_commit
        or payload["output_root"] != str(seed_database.parent.parent)
        or payload["production_source_database"] != str(production_source_database)
        or payload["production_source_opened"] is not False
        or payload["seed_database"] != str(seed_database)
        or payload["seed_provenance"] != str(seed_provenance)
        or payload["seed_provenance_sha256"] != provenance_fingerprint["sha256"]
        or payload["scale_floor"] != REHEARSAL_SCALE_MINIMUMS
    ):
        raise VerificationError("rehearsal seed generator receipt bindings are inconsistent")

    checkout = payload["candidate_checkout"]
    script_relative = "packages/agentstack_mail/scripts/build_rehearsal_seed.py"
    migration_relative = "packages/agentstack_mail/src/agentstack_mail/migration.py"
    if (
        not isinstance(checkout, dict)
        or set(checkout)
        != {
            "repository",
            "head",
            "tracked_and_untracked_worktree_clean",
            "executing_file_sha256",
        }
        or checkout["repository"] != str(candidate_repository)
        or checkout["head"] != candidate_commit
        or checkout["tracked_and_untracked_worktree_clean"] is not True
        or not isinstance(checkout["executing_file_sha256"], dict)
        or set(checkout["executing_file_sha256"])
        != {script_relative, migration_relative}
        or checkout["executing_file_sha256"][migration_relative]
        != candidate_checkout["migration_module_sha256"]
    ):
        raise VerificationError("generator candidate checkout binding is malformed")
    script_blob = subprocess.run(
        [
            "git",
            "-c",
            f"core.hooksPath={os.devnull}",
            "-C",
            str(candidate_repository),
            "show",
            f"{candidate_commit}:{script_relative}",
        ],
        check=False,
        capture_output=True,
        env=_git_environment(),
        timeout=GIT_TIMEOUT_SECONDS,
    )
    if (
        script_blob.returncode != 0
        or hashlib.sha256(script_blob.stdout).hexdigest()
        != checkout["executing_file_sha256"][script_relative]
    ):
        raise VerificationError("generator script does not match its candidate blob")

    main_fingerprint = _fingerprint_regular_file(seed_database, required=True)
    assert main_fingerprint is not None
    expected_family = payload["seed_database_family"]
    if (
        not isinstance(expected_family, dict)
        or set(expected_family) != {"main", "wal", "shm"}
        or expected_family["main"]
        != {
            "state": "PRESENT",
            "size": main_fingerprint["size"],
            "sha256": main_fingerprint["sha256"],
        }
        or expected_family["wal"] != {"state": "ABSENT"}
        or expected_family["shm"] != {"state": "ABSENT"}
        or payload["seed_database_size"] != main_fingerprint["size"]
        or payload["seed_database_sha256"] != main_fingerprint["sha256"]
        or _fingerprint_regular_file(Path(f"{seed_database}-wal"), required=False)
        is not None
        or _fingerprint_regular_file(Path(f"{seed_database}-shm"), required=False)
        is not None
    ):
        raise VerificationError("rehearsal seed changed after generator publication")
    seed_root = seed_database.parent
    expected_archive = {
        "path": str(seed_root / "archive"),
        "snapshot": snapshot_tree(
            seed_root / "archive",
            required=True,
            excluded_root_names=ARCHIVE_EXCLUDED_ROOT_NAMES,
        ),
    }
    expected_signals = {
        "path": str(seed_root / "signals"),
        "snapshot": snapshot_tree(seed_root / "signals", required=False),
    }
    if (
        payload["seed_archive"] != expected_archive
        or payload["seed_signals"] != expected_signals
    ):
        raise VerificationError(
            "rehearsal seed archive or signals changed after generator publication"
        )
    if _fingerprint_regular_file(receipt_path, required=True) != fingerprint:
        raise VerificationError("generator receipt changed while it was read")
    return payload, fingerprint


def _seed_scale(
    source_artifact: dict[str, Any], logical_snapshot: dict[str, Any]
) -> dict[str, Any]:
    file_sizes = {
        role: (
            record["size"] if record["state"] == "PRESENT" else None
        )
        for role, record in source_artifact["files"].items()
    }
    major_table_rows = {
        table: logical_snapshot["tables"][table]["count"]
        for table in sorted(REQUIRED_TABLES)
    }
    return {
        "database_family_bytes": sum(
            size for size in file_sizes.values() if size is not None
        ),
        "file_sizes": file_sizes,
        "major_table_rows": major_table_rows,
    }


def _assert_production_shaped_scale(scale: dict[str, Any]) -> None:
    failures: list[str] = []
    if scale["database_family_bytes"] < REHEARSAL_SCALE_MINIMUMS[
        "database_family_bytes"
    ]:
        failures.append(
            "database_family_bytes=" + str(scale["database_family_bytes"])
        )
    for table in ("agents", "messages", "message_recipients"):
        count = scale["major_table_rows"][table]
        if count < REHEARSAL_SCALE_MINIMUMS[table]:
            failures.append(f"{table}={count}")
    if failures:
        raise VerificationError(
            "rehearsal seed is below the production-shaped scale floor: "
            + ", ".join(failures)
        )


def _verify_artifact_descriptor(
    descriptor: Any, *, label: str, run_directory: Path
) -> dict[str, Any]:
    if not isinstance(descriptor, dict) or set(descriptor) != {
        "directory",
        "files",
        "family_sha256",
    }:
        raise VerificationError(f"{label} artifact descriptor is malformed")
    directory = _canonical_absolute_path(
        descriptor["directory"], label=f"{label}.directory"
    )
    if not _path_within(directory, run_directory):
        raise VerificationError(f"{label} artifact is outside the rehearsal run")
    files = descriptor["files"]
    if not isinstance(files, dict) or set(files) != set(COLD_BACKUP_FILE_NAMES):
        raise VerificationError(f"{label} artifact file map is malformed")
    actual_core: dict[str, dict[str, Any]] = {}
    for role, expected_name in COLD_BACKUP_FILE_NAMES.items():
        record = files[role]
        if not isinstance(record, dict) or not isinstance(record.get("path"), str):
            raise VerificationError(f"{label} {role} artifact record is malformed")
        path = _canonical_absolute_path(record["path"], label=f"{label}.{role}.path")
        if path != directory / expected_name:
            raise VerificationError(f"{label} {role} artifact path is not canonical")
        fingerprint = _fingerprint_regular_file(path, required=role == "main")
        actual = fingerprint if fingerprint is not None else {"state": "ABSENT"}
        expected = {
            key: record[key]
            for key in ("state", "size", "mode", "sha256")
            if key in record
        }
        if actual != expected:
            raise VerificationError(f"{label} {role} raw artifact changed")
        actual_core[role] = actual
    expected_entries = {
        COLD_BACKUP_FILE_NAMES[role]
        for role, record in actual_core.items()
        if record["state"] == "PRESENT"
    }
    if label == "backup":
        expected_entries.add(COLD_BACKUP_RECEIPT_NAME)
    actual_entries = {entry.name for entry in os.scandir(directory)}
    if actual_entries != expected_entries:
        raise VerificationError(
            f"{label} raw artifact directory allowlist changed: "
            f"expected={sorted(expected_entries)}, actual={sorted(actual_entries)}"
        )
    digest = _generation_digest(actual_core)
    if descriptor["family_sha256"] != digest:
        raise VerificationError(f"{label} family digest is inconsistent")
    return {
        "descriptor": descriptor,
        "core": actual_core,
        "family_sha256": digest,
    }


def _verify_cold_rehearsal_payload(
    payload: Any,
    *,
    receipt_path: Path | None,
    allow_verification_receipt: bool = False,
) -> dict[str, Any]:
    required_keys = {
        "schema_version",
        "tool",
        "kind",
        "run_id",
        "started_at",
        "completed_at",
        "status",
        "mode",
        "candidate_commit",
        "candidate_checkout",
        "canonical_paths",
        "production_source",
        "seed",
        "identities",
        "artifacts",
        "damage",
        "restore",
        "independent_verification",
    }
    if not isinstance(payload, dict) or set(payload) != required_keys:
        raise VerificationError("cold restore rehearsal receipt has an unexpected shape")
    if payload["schema_version"] != 1 or payload["kind"] != "cold-restore-rehearsal":
        raise VerificationError("cold restore rehearsal receipt schema/kind is invalid")
    if payload["tool"] != {
        "name": "agentstack-mail-migrate",
        "version": AGENTSTACK_MAIL_VERSION,
    }:
        raise VerificationError("cold restore rehearsal tool identity is invalid")
    run_id = _canonical_operation_id(payload["run_id"], label="rehearsal")
    if payload["status"] != "completed" or payload["mode"] != "isolated_rehearsal":
        raise VerificationError("cold restore rehearsal did not complete in isolated mode")
    started_at = _parse_utc_timestamp(payload["started_at"], label="started_at")
    completed_at = _parse_utc_timestamp(payload["completed_at"], label="completed_at")
    if completed_at < started_at:
        raise VerificationError("cold restore rehearsal completed before it started")
    _validate_candidate_commit(payload["candidate_commit"])
    checkout = payload["candidate_checkout"]
    if not isinstance(checkout, dict) or set(checkout) != {
        "repository",
        "head",
        "tracked_worktree_clean",
        "migration_module",
        "migration_module_sha256",
    }:
        raise VerificationError("cold restore rehearsal candidate checkout is malformed")
    if checkout["head"] != payload["candidate_commit"] or checkout[
        "tracked_worktree_clean"
    ] is not True:
        raise VerificationError("cold restore rehearsal candidate checkout is inconsistent")
    if _candidate_checkout_identity(
        Path(checkout["repository"]), payload["candidate_commit"]
    ) != checkout:
        raise VerificationError("candidate checkout no longer matches the rehearsal")
    paths = payload["canonical_paths"]
    if not isinstance(paths, dict) or set(paths) != {
        "run_directory",
        "rehearsal_receipt",
        "working_target",
    }:
        raise VerificationError("cold restore rehearsal canonical paths are malformed")
    run_directory = _canonical_absolute_path(
        paths["run_directory"], label="rehearsal run_directory"
    )
    expected_receipt = run_directory / COLD_REHEARSAL_RECEIPT_NAME
    if Path(paths["rehearsal_receipt"]) != expected_receipt:
        raise VerificationError("cold restore rehearsal receipt path is not canonical")
    if receipt_path is not None and receipt_path != expected_receipt:
        raise VerificationError("rehearsal verifier was given the wrong receipt path")
    production = payload["production_source"]
    seed = payload["seed"]
    if (
        not isinstance(production, dict)
        or production.get("used") is not False
        or production.get("database") == seed.get("database")
    ):
        raise VerificationError("rehearsal did not prove separation from production source")
    if seed.get("kind") != "production-shaped-synthetic" or not all(
        isinstance(seed.get(key), str)
        for key in ("origin", "acquisition_method", "acquired_at")
    ):
        raise VerificationError("rehearsal seed provenance is malformed")

    artifacts = payload["artifacts"]
    if not isinstance(artifacts, dict) or set(artifacts) != {
        "source",
        "backup",
        "damaged",
        "restored",
    }:
        raise VerificationError("rehearsal four-state artifact map is malformed")
    verified = {
        name: _verify_artifact_descriptor(
            descriptor, label=name, run_directory=run_directory
        )
        for name, descriptor in artifacts.items()
    }
    source_generation = verified["source"]["family_sha256"]
    backup_generation = verified["backup"]["family_sha256"]
    damaged_generation = verified["damaged"]["family_sha256"]
    restored_generation = verified["restored"]["family_sha256"]
    if source_generation != backup_generation or restored_generation != backup_generation:
        raise VerificationError("source/backup/restored raw families are not identical")
    if damaged_generation == backup_generation:
        raise VerificationError("damage was a no-op at the physical family boundary")

    identities = payload["identities"]
    if not isinstance(identities, dict) or set(identities) != {
        "migration_manifest",
        "seed_provenance",
        "seed_generator_receipt",
        "cold_backup_receipt",
        "cold_restore_receipt",
    }:
        raise VerificationError("rehearsal identities are malformed")
    for label, identity in identities.items():
        if not isinstance(identity, dict) or set(identity) != {"path", "sha256"}:
            raise VerificationError(f"{label} identity is malformed")
        identity_path = _canonical_absolute_path(identity["path"], label=label)
        fingerprint = _fingerprint_regular_file(identity_path, required=True)
        assert fingerprint is not None
        if fingerprint["sha256"] != identity["sha256"]:
            raise VerificationError(f"{label} no longer matches its recorded SHA-256")

    sealed_manifest_path = Path(identities["migration_manifest"]["path"])
    sealed_provenance_path = Path(identities["seed_provenance"]["path"])
    expected_identity_paths = {
        "migration_manifest": run_directory / "identities" / MANIFEST_NAME,
        "seed_provenance": run_directory / "identities" / "seed-provenance.json",
        "seed_generator_receipt": (
            run_directory / "identities" / REHEARSAL_GENERATOR_RECEIPT_NAME
        ),
        "cold_backup_receipt": run_directory / "backup" / COLD_BACKUP_RECEIPT_NAME,
        "cold_restore_receipt": run_directory / "cold-restore-receipt.json",
    }
    for label, expected_path in expected_identity_paths.items():
        if Path(identities[label]["path"]) != expected_path:
            raise VerificationError(f"{label} is outside its canonical rehearsal path")
    raw_root = run_directory / "raw"
    if {entry.name for entry in os.scandir(raw_root)} != {
        "source",
        "damaged",
        "restored",
    }:
        raise VerificationError("rehearsal raw root exact allowlist changed")
    identities_root = run_directory / "identities"
    if {entry.name for entry in os.scandir(identities_root)} != {
        MANIFEST_NAME,
        "seed-provenance.json",
        REHEARSAL_GENERATOR_RECEIPT_NAME,
    }:
        raise VerificationError("rehearsal identities exact allowlist changed")
    expected_run_entries = {
        COLD_REHEARSAL_MARKER_NAME,
        "raw",
        "identities",
        "backup",
        "working-target",
        "cold-restore-receipt.json",
    }
    if receipt_path is not None:
        expected_run_entries.add(COLD_REHEARSAL_RECEIPT_NAME)
    if allow_verification_receipt:
        expected_run_entries.add(COLD_REHEARSAL_VERIFICATION_NAME)
    if {entry.name for entry in os.scandir(run_directory)} != expected_run_entries:
        raise VerificationError("rehearsal run root exact allowlist changed")
    expected_working_entries = {
        COLD_BACKUP_FILE_NAMES[role]
        for role, record in verified["restored"]["core"].items()
        if record["state"] == "PRESENT"
    }
    if {
        entry.name for entry in os.scandir(run_directory / "working-target")
    } != expected_working_entries:
        raise VerificationError("rehearsal working target exact allowlist changed")
    manifest = _load_manifest(sealed_manifest_path)
    if manifest["source"]["database"] != seed.get("database"):
        raise VerificationError("sealed manifest does not name the rehearsal seed")
    provenance, _ = _load_seed_provenance(
        sealed_provenance_path,
        seed_database=Path(seed["database"]),
        production_source_database=Path(production["database"]),
    )
    if (
        provenance["kind"] != seed["kind"]
        or provenance["source_reference"] != seed["origin"]
        or provenance["acquisition_method"] != seed["acquisition_method"]
        or provenance["created_at"] != seed["acquired_at"]
    ):
        raise VerificationError("sealed seed provenance differs from the rehearsal receipt")

    generator = _read_owned_json(
        Path(identities["seed_generator_receipt"]["path"]),
        label="sealed rehearsal seed generator receipt",
    )
    generator_checkout = generator.get("candidate_checkout", {})
    generator_hashes = generator_checkout.get("executing_file_sha256", {})
    generator_main = generator.get("seed_database_family", {}).get("main")
    source_main = verified["source"]["core"]["main"]
    generator_script_relative = (
        "packages/agentstack_mail/scripts/build_rehearsal_seed.py"
    )
    generator_script = subprocess.run(
        [
            "git",
            "-c",
            f"core.hooksPath={os.devnull}",
            "-C",
            checkout["repository"],
            "show",
            f"{payload['candidate_commit']}:{generator_script_relative}",
        ],
        check=False,
        capture_output=True,
        env=_git_environment(),
        timeout=GIT_TIMEOUT_SECONDS,
    )
    if (
        generator.get("schema_version") != 1
        or generator.get("kind")
        != "production-shaped-synthetic-seed-generation"
        or generator.get("candidate_commit") != payload["candidate_commit"]
        or generator.get("output_root") != str(Path(seed["database"]).parent.parent)
        or generator.get("production_source_database") != production["database"]
        or generator.get("production_source_opened") is not False
        or generator.get("seed_database") != seed["database"]
        or generator.get("seed_provenance")
        != str(Path(seed["database"]).parent.parent / "seed-provenance.json")
        or generator.get("seed_provenance_sha256")
        != identities["seed_provenance"]["sha256"]
        or generator.get("scale_floor") != REHEARSAL_SCALE_MINIMUMS
        or generator_checkout.get("repository") != checkout["repository"]
        or generator_checkout.get("head") != payload["candidate_commit"]
        or generator_checkout.get("tracked_and_untracked_worktree_clean") is not True
        or generator_hashes.get(
            "packages/agentstack_mail/src/agentstack_mail/migration.py"
        )
        != checkout["migration_module_sha256"]
        or generator_script.returncode != 0
        or generator_hashes.get(generator_script_relative)
        != hashlib.sha256(generator_script.stdout).hexdigest()
        or generator_main
        != {
            "state": "PRESENT",
            "size": source_main["size"],
            "sha256": source_main["sha256"],
        }
        or generator.get("seed_database_family", {}).get("wal")
        != {"state": "ABSENT"}
        or generator.get("seed_database_family", {}).get("shm")
        != {"state": "ABSENT"}
        or generator.get("seed_database_size") != source_main["size"]
        or generator.get("seed_database_sha256") != source_main["sha256"]
        or generator.get("seed_archive")
        != {
            "path": manifest["source"]["archive"],
            "snapshot": manifest["baseline"]["archive"],
        }
        or generator.get("seed_signals")
        != {
            "path": manifest["source"]["signals"],
            "snapshot": manifest["baseline"]["signals"],
        }
    ):
        raise VerificationError("sealed seed generator receipt is inconsistent")

    cold_backup_path = Path(identities["cold_backup_receipt"]["path"])
    backup_receipt = _load_cold_backup_receipt(cold_backup_path.parent)
    if backup_receipt["logical_sha256"] != seed.get("logical_sha256"):
        raise VerificationError("cold backup logical identity differs from the seed")
    if manifest["baseline"]["database"] != backup_receipt["logical_snapshot"]:
        raise VerificationError("sealed manifest baseline differs from the cold backup")
    source_logical = _logical_artifact_result(
        artifacts["source"], scratch_parent=run_directory.parent
    )
    damaged_logical = _logical_artifact_result(
        artifacts["damaged"], scratch_parent=run_directory.parent
    )
    restored_logical = _logical_artifact_result(
        artifacts["restored"], scratch_parent=run_directory.parent
    )
    if (
        source_logical.get("status") != "valid"
        or source_logical.get("logical_sha256") != seed.get("logical_sha256")
        or restored_logical.get("status") != "valid"
        or restored_logical.get("logical_sha256") != seed.get("logical_sha256")
    ):
        raise VerificationError("source or restored logical state differs from the seed")
    if (
        damaged_logical.get("status") == "valid"
        and damaged_logical.get("logical_sha256") == seed.get("logical_sha256")
    ):
        raise VerificationError("damage was a no-op at the logical boundary")

    expected_scale = _seed_scale(artifacts["source"], source_logical["snapshot"])
    if seed.get("scale") != expected_scale:
        raise VerificationError("rehearsal seed scale/provenance is inconsistent")
    if generator.get("major_table_rows") != expected_scale["major_table_rows"]:
        raise VerificationError("generator receipt row counts do not match raw seed")
    _assert_production_shaped_scale(expected_scale)
    damage = payload["damage"]
    derived_created_sidecars = [
        role
        for role in ("wal", "shm")
        if verified["backup"]["core"][role]["state"] == "ABSENT"
        and verified["damaged"]["core"][role]["state"] == "PRESENT"
        and verified["restored"]["core"][role]["state"] == "ABSENT"
    ]
    if (
        not isinstance(damage, dict)
        or damage.get("plan") != COLD_REHEARSAL_DAMAGE_PLAN
        or damage.get("damage_assertion_passed") is not True
        or damage.get("before_family_sha256") != backup_generation
        or damage.get("after_family_sha256") != damaged_generation
        or damage.get("observed_before_physical") != verified["backup"]["core"]
        or damage.get("observed_after_physical") != verified["damaged"]["core"]
        or not isinstance(damage.get("created_absent_sidecars"), list)
        or damage["created_absent_sidecars"] != derived_created_sidecars
        or not derived_created_sidecars
    ):
        raise VerificationError("rehearsal damage evidence is malformed")
    for role in damage["created_absent_sidecars"]:
        if role not in {"wal", "shm"}:
            raise VerificationError("rehearsal created an unknown sidecar role")
        if (
            verified["backup"]["core"][role]["state"] != "ABSENT"
            or verified["damaged"]["core"][role]["state"] != "PRESENT"
            or verified["restored"]["core"][role]["state"] != "ABSENT"
        ):
            raise VerificationError("ABSENT sidecar removal branch was not exercised")
    if verified["backup"]["core"]["main"] == verified["damaged"]["core"]["main"]:
        raise VerificationError("PRESENT main replace branch was not exercised")

    restore_receipt = _read_owned_json(
        Path(identities["cold_restore_receipt"]["path"]),
        label="cold restore receipt",
    )
    if (
        not isinstance(restore_receipt, dict)
        or restore_receipt.get("kind") != "cold-restore"
        or restore_receipt.get("restore_result", {}).get("status") != "restored"
        or restore_receipt.get("logical_validator", {}).get("status") != "matched"
    ):
        raise VerificationError("cold restore receipt did not record a completed restore")
    if restore_receipt.get("migration_identity", {}).get("manifest_sha256") != identities[
        "migration_manifest"
    ]["sha256"]:
        raise VerificationError("cold restore receipt names a different migration manifest")
    if restore_receipt.get("backup_identity", {}).get("receipt_sha256") != identities[
        "cold_backup_receipt"
    ]["sha256"]:
        raise VerificationError("cold restore receipt names a different cold backup")
    physical = restore_receipt.get("physical_validator")
    if not isinstance(physical, dict) or physical.get("status") != "matched":
        raise VerificationError("cold restore receipt lacks physical verification")
    if (
        _generation_digest(physical.get("pre_restore", {})) != damaged_generation
        or _generation_digest(physical.get("post_restore_before_logical", {}))
        != restored_generation
        or physical.get("post_restore_before_logical") != physical.get("post_logical")
    ):
        raise VerificationError("cold restore physical maps do not bind damaged/restored artifacts")
    outcomes = restore_receipt["restore_result"]["files"]
    if outcomes.get("main", {}).get("result") != "atomic_replace":
        raise VerificationError("PRESENT main atomic replace was not recorded")
    for role in damage["created_absent_sidecars"]:
        if outcomes.get(role, {}).get("result") != "atomic_quarantine_remove":
            raise VerificationError("ABSENT sidecar atomic removal was not recorded")

    if payload["independent_verification"] != {
        "status": "required_separate_receipt",
        "embedded_result_is_evidence": False,
    }:
        raise VerificationError("rehearsal receipt must require a separate verifier receipt")

    return {
        "status": "verified",
        "run_id": run_id,
        "candidate_commit": payload["candidate_commit"],
        "raw_artifact_count": 4,
        "physical_family_sha256": restored_generation,
        "logical_sha256": seed["logical_sha256"],
        "damage_control": "physical_and_logical_non_noop",
        "recomputed_at": _utc_now(),
    }


def rehearse_cold_restore(
    seed_database: Path,
    production_source_database: Path,
    run_directory: Path,
    migration_manifest: Path,
    candidate_repository: Path,
    seed_provenance: Path,
    *,
    generator_receipt: Path,
    candidate_commit: str,
    expected_generator_receipt_sha256: str,
) -> dict[str, Any]:
    """Run one isolated backup/damage/restore cycle and retain all raw evidence."""

    candidate_commit = _validate_candidate_commit(candidate_commit)
    seed_database = _absolute_without_symlinks(seed_database)
    production_source_database = _absolute_without_symlinks(production_source_database)
    run_directory = _absolute_without_symlinks(run_directory)
    migration_manifest = _absolute_without_symlinks(migration_manifest)
    candidate_repository = _absolute_without_symlinks(candidate_repository)
    seed_provenance = _absolute_without_symlinks(seed_provenance)
    generator_receipt = _absolute_without_symlinks(generator_receipt)
    for path in (
        seed_database,
        production_source_database,
        run_directory,
        migration_manifest,
        candidate_repository,
        seed_provenance,
        generator_receipt,
    ):
        _assert_no_symlink_components(path)
    if seed_database == production_source_database:
        raise MigrationError("isolated rehearsal must not use the production source database")
    if run_directory.exists() or run_directory.is_symlink():
        raise MigrationError(f"rehearsal run directory must be absent: {run_directory}")
    if not run_directory.parent.is_dir() or run_directory.parent.is_symlink():
        raise MigrationError(
            f"rehearsal parent must be a real existing directory: {run_directory.parent}"
        )
    if _path_within(run_directory, seed_database.parent) or _path_within(
        run_directory, production_source_database.parent
    ):
        raise MigrationError("rehearsal evidence must be outside seed and production roots")
    manifest_fingerprint = _fingerprint_regular_file(migration_manifest, required=True)
    assert manifest_fingerprint is not None
    manifest = _load_manifest(migration_manifest)
    if Path(manifest["source"]["database"]) != seed_database:
        raise MigrationError("rehearsal manifest must name the isolated seed database")
    candidate_checkout = _candidate_checkout_identity(
        candidate_repository, candidate_commit
    )
    provenance, provenance_fingerprint = _load_seed_provenance(
        seed_provenance,
        seed_database=seed_database,
        production_source_database=production_source_database,
    )
    generator, generator_fingerprint = _load_rehearsal_generator_receipt(
        generator_receipt,
        expected_sha256=expected_generator_receipt_sha256,
        seed_database=seed_database,
        production_source_database=production_source_database,
        seed_provenance=seed_provenance,
        provenance_fingerprint=provenance_fingerprint,
        candidate_repository=candidate_repository,
        candidate_commit=candidate_commit,
        candidate_checkout=candidate_checkout,
    )

    run_id = str(uuid.uuid4())
    started_at = _utc_now()
    marker = run_directory / COLD_REHEARSAL_MARKER_NAME
    receipt_path = run_directory / COLD_REHEARSAL_RECEIPT_NAME
    prepared_receipt = (
        run_directory / f".{COLD_REHEARSAL_RECEIPT_NAME}.{run_id}.prepared"
    )
    unconfirmed_receipt = (
        run_directory / f".{COLD_REHEARSAL_RECEIPT_NAME}.{run_id}.unconfirmed"
    )
    run_directory.mkdir(mode=0o700)
    try:
        _fsync_directory(run_directory.parent)
        _write_json_exclusive(
            marker,
            {
                "schema_version": 1,
                "kind": "cold-restore-rehearsal-in-progress",
                "run_id": run_id,
                "candidate_commit": candidate_commit,
            },
        )
        raw_root = run_directory / "raw"
        raw_root.mkdir(mode=0o700)
        _fsync_directory(run_directory)
        source_artifact = _copy_database_family_artifact(
            _database_family_paths(seed_database), raw_root / "source"
        )
        source_logical = _logical_artifact_result(
            source_artifact, scratch_parent=run_directory.parent
        )
        if source_logical["status"] != "valid":
            raise VerificationError("rehearsal seed raw artifact is not logically valid")
        if source_logical["snapshot"] != manifest["baseline"]["database"]:
            raise VerificationError("rehearsal seed differs from its migration baseline")
        scale = _seed_scale(source_artifact, source_logical["snapshot"])
        if generator["major_table_rows"] != scale["major_table_rows"]:
            raise VerificationError("rehearsal seed row counts differ from generator receipt")
        _assert_production_shaped_scale(scale)

        identities_directory = run_directory / "identities"
        identities_directory.mkdir(mode=0o700)
        sealed_manifest = identities_directory / MANIFEST_NAME
        sealed_provenance = identities_directory / "seed-provenance.json"
        sealed_generator = identities_directory / REHEARSAL_GENERATOR_RECEIPT_NAME
        sealed_manifest_fingerprint = _copy_regular_file_exact(
            migration_manifest, sealed_manifest
        )
        sealed_provenance_fingerprint = _copy_regular_file_exact(
            seed_provenance, sealed_provenance
        )
        sealed_generator_fingerprint = _copy_regular_file_exact(
            generator_receipt, sealed_generator
        )
        if sealed_manifest_fingerprint != manifest_fingerprint:
            raise VerificationError("sealed rehearsal migration manifest changed")
        if sealed_provenance_fingerprint != provenance_fingerprint:
            raise VerificationError("sealed rehearsal seed provenance changed")
        if sealed_generator_fingerprint != generator_fingerprint:
            raise VerificationError("sealed rehearsal generator receipt changed")
        _fsync_directory(identities_directory)
        _fsync_directory(run_directory)

        backup_directory = run_directory / "backup"
        backup_result = cold_backup_database(
            seed_database, backup_directory, services_stopped=True
        )
        backup_receipt = _load_cold_backup_receipt(backup_directory)
        backup_paths = {
            role: backup_directory / record["backup_name"]
            for role, record in backup_receipt["files"].items()
        }
        backup_artifact = _artifact_descriptor(backup_paths)
        working_directory = run_directory / "working-target"
        before_damage = _copy_database_family_artifact(
            backup_paths, working_directory
        )
        working_paths = _artifact_paths(before_damage)
        damage_action = _damage_rehearsal_target(
            working_paths, backup_receipt["files"]
        )
        damaged_artifact = _copy_database_family_artifact(
            working_paths, raw_root / "damaged"
        )
        if (
            damage_action.get("observed_before_physical")
            != _artifact_core_records(before_damage)
            or damage_action.get("observed_after_physical")
            != _artifact_core_records(damaged_artifact)
        ):
            raise VerificationError(
                "rehearsal damage function did not bind its own before/after state"
            )
        damage_logical = _logical_artifact_result(
            damaged_artifact, scratch_parent=run_directory.parent
        )
        physical_changed = (
            before_damage["family_sha256"] != damaged_artifact["family_sha256"]
        )
        logical_changed = (
            damage_logical["status"] != "valid"
            or damage_logical.get("logical_sha256")
            != source_logical["logical_sha256"]
        )
        if not physical_changed or not logical_changed:
            raise VerificationError(
                "rehearsal damage was a no-op; restore was not invoked"
            )

        restore_receipt_path = run_directory / "cold-restore-receipt.json"
        restore_result = cold_restore_database(
            backup_directory,
            working_paths["main"],
            restore_receipt_path,
            sealed_manifest,
            services_stopped=True,
            target_kind="rehearsal-copy",
            fault_injection=COLD_REHEARSAL_DAMAGE_PLAN,
        )
        restored_artifact = _copy_database_family_artifact(
            working_paths, raw_root / "restored"
        )
        restored_logical = _logical_artifact_result(
            restored_artifact, scratch_parent=run_directory.parent
        )
        if (
            restored_logical["status"] != "valid"
            or restored_logical["snapshot"] != source_logical["snapshot"]
        ):
            raise VerificationError("rehearsal restore did not recover the seed logically")
        if restored_artifact["family_sha256"] != backup_artifact["family_sha256"]:
            raise VerificationError("rehearsal restore did not recover the raw family")

        backup_receipt_path = backup_directory / COLD_BACKUP_RECEIPT_NAME
        backup_receipt_fingerprint = _fingerprint_regular_file(
            backup_receipt_path, required=True
        )
        restore_receipt_fingerprint = _fingerprint_regular_file(
            restore_receipt_path, required=True
        )
        assert backup_receipt_fingerprint is not None
        assert restore_receipt_fingerprint is not None
        payload: dict[str, Any] = {
            "schema_version": 1,
            "tool": {
                "name": "agentstack-mail-migrate",
                "version": AGENTSTACK_MAIL_VERSION,
            },
            "kind": "cold-restore-rehearsal",
            "run_id": run_id,
            "started_at": started_at,
            "completed_at": _utc_now(),
            "status": "completed",
            "mode": "isolated_rehearsal",
            "candidate_commit": candidate_commit,
            "candidate_checkout": candidate_checkout,
            "canonical_paths": {
                "run_directory": str(run_directory),
                "rehearsal_receipt": str(receipt_path),
                "working_target": str(working_paths["main"]),
            },
            "production_source": {
                "database": str(production_source_database),
                "used": False,
            },
            "seed": {
                "kind": provenance["kind"],
                "origin": provenance["source_reference"],
                "acquisition_method": provenance["acquisition_method"],
                "acquired_at": provenance["created_at"],
                "database": str(seed_database),
                "logical_sha256": source_logical["logical_sha256"],
                "scale": scale,
                "services": "not_applicable_isolated_copy",
            },
            "identities": {
                "migration_manifest": {
                    "path": str(sealed_manifest),
                    "sha256": sealed_manifest_fingerprint["sha256"],
                },
                "seed_provenance": {
                    "path": str(sealed_provenance),
                    "sha256": sealed_provenance_fingerprint["sha256"],
                },
                "seed_generator_receipt": {
                    "path": str(sealed_generator),
                    "sha256": sealed_generator_fingerprint["sha256"],
                },
                "cold_backup_receipt": {
                    "path": str(backup_receipt_path),
                    "sha256": backup_receipt_fingerprint["sha256"],
                },
                "cold_restore_receipt": {
                    "path": str(restore_receipt_path),
                    "sha256": restore_receipt_fingerprint["sha256"],
                },
            },
            "artifacts": {
                "source": source_artifact,
                "backup": backup_artifact,
                "damaged": damaged_artifact,
                "restored": restored_artifact,
            },
            "damage": {
                **damage_action,
                "before_family_sha256": before_damage["family_sha256"],
                "after_family_sha256": damaged_artifact["family_sha256"],
                "before_physical": before_damage["files"],
                "after_physical": damaged_artifact["files"],
                "logical_after": damage_logical,
                "damage_assertion_passed": True,
            },
            "restore": {
                "command_result": restore_result,
                "physical_before_logical": restored_artifact["files"],
                "logical_after": restored_logical,
            },
            "independent_verification": {
                "status": "required_separate_receipt",
                "embedded_result_is_evidence": False,
            },
        }
        _verify_cold_rehearsal_payload(payload, receipt_path=None)
        _replace_json_fsynced(
            marker,
            {
                "schema_version": 1,
                "kind": "cold-restore-rehearsal-ownership",
                "run_id": run_id,
                "candidate_commit": candidate_commit,
                "phase": "RAW_ARTIFACTS_VERIFIED_AWAITING_TERMINAL_RECEIPT",
            },
        )
        _write_json_exclusive(prepared_receipt, payload)
        _replace_json_fsynced(
            marker,
            {
                "schema_version": 1,
                "kind": "cold-restore-rehearsal-ownership",
                "run_id": run_id,
                "candidate_commit": candidate_commit,
                "phase": "TERMINAL_RECEIPT_PREPARED_OR_PUBLISHED",
            },
        )
        os.replace(prepared_receipt, receipt_path)
        _fsync_directory(run_directory)
        receipt_fingerprint = _fingerprint_regular_file(receipt_path, required=True)
        assert receipt_fingerprint is not None
        return {
            "status": "completed",
            "run_id": run_id,
            "candidate_commit": candidate_commit,
            "rehearsal_receipt": str(receipt_path),
            "rehearsal_receipt_sha256": receipt_fingerprint["sha256"],
            "logical_sha256": source_logical["logical_sha256"],
            "backup_result": backup_result["status"],
            "restore_result": restore_result["status"],
        }
    except Exception:
        # A failed run is retained with its in-progress marker and raw artifacts.
        # A canonical terminal receipt is the success boundary. If publication was
        # not durably confirmed to the caller, retain its bytes under an explicitly
        # non-canonical name instead of leaving an ambiguous success receipt.
        if receipt_path.exists() or receipt_path.is_symlink():
            try:
                os.replace(receipt_path, unconfirmed_receipt)
                _fsync_directory(run_directory)
            except OSError as quarantine_error:
                raise AssertionError(
                    "failed rehearsal could not quarantine its terminal receipt"
                ) from quarantine_error
        if not marker.exists() and not marker.is_symlink():
            try:
                run_directory.rmdir()
                _fsync_directory(run_directory.parent)
            except OSError:
                # Non-empty state was created before the marker became durable.
                # Never delete an ownership-ambiguous incident directory.
                pass
        if receipt_path.exists() or receipt_path.is_symlink():
            raise AssertionError("failed rehearsal unexpectedly retained a terminal receipt")
        raise


def verify_cold_restore_rehearsal(
    receipt_path: Path,
    verification_receipt: Path,
    *,
    expected_receipt_sha256: str,
    expected_run_id: str,
    expected_candidate_commit: str,
) -> dict[str, Any]:
    receipt_path = _absolute_without_symlinks(receipt_path)
    verification_receipt = _absolute_without_symlinks(verification_receipt)
    _assert_no_symlink_components(receipt_path)
    _assert_no_symlink_components(verification_receipt)
    if verification_receipt != receipt_path.parent / COLD_REHEARSAL_VERIFICATION_NAME:
        raise MigrationError("verification receipt must use the canonical rehearsal path")
    if verification_receipt.exists() or verification_receipt.is_symlink():
        raise MigrationError(
            f"verification receipt destination must be absent: {verification_receipt}"
        )
    if re.fullmatch(r"[0-9a-f]{64}", expected_receipt_sha256) is None:
        raise MigrationError("expected rehearsal receipt SHA-256 is malformed")
    _canonical_operation_id(expected_run_id, label="expected rehearsal")
    _validate_candidate_commit(expected_candidate_commit)
    fingerprint = _fingerprint_regular_file(receipt_path, required=True)
    assert fingerprint is not None
    if fingerprint["sha256"] != expected_receipt_sha256:
        raise VerificationError("rehearsal receipt differs from its out-of-band SHA-256 pin")
    payload = _read_owned_json(receipt_path, label="cold restore rehearsal receipt")
    if (
        not isinstance(payload, dict)
        or payload.get("run_id") != expected_run_id
        or payload.get("candidate_commit") != expected_candidate_commit
    ):
        raise VerificationError("rehearsal receipt differs from out-of-band run/candidate pins")
    result = _verify_cold_rehearsal_payload(payload, receipt_path=receipt_path)
    if _fingerprint_regular_file(receipt_path, required=True) != fingerprint:
        raise VerificationError("rehearsal receipt changed during independent verification")
    verification_payload = {
        "schema_version": 1,
        "tool": {
            "name": "agentstack-mail-migrate",
            "version": AGENTSTACK_MAIL_VERSION,
        },
        "kind": "cold-restore-rehearsal-verification",
        "created_at": _utc_now(),
        "rehearsal_receipt": str(receipt_path),
        "rehearsal_receipt_sha256": fingerprint["sha256"],
        "result": result,
    }
    unconfirmed = (
        verification_receipt.parent
        / (
            f".{verification_receipt.name}.cold-restore-rehearsal-verification-"
            f"{expected_run_id}.unconfirmed"
        )
    )
    published = False
    try:
        _write_json_exclusive(verification_receipt, verification_payload)
        published = True
        verification_fingerprint = _fingerprint_regular_file(
            verification_receipt, required=True
        )
        assert verification_fingerprint is not None
        return {
            **result,
            "rehearsal_receipt": str(receipt_path),
            "rehearsal_receipt_sha256": fingerprint["sha256"],
            "verification_receipt": str(verification_receipt),
            "verification_receipt_sha256": verification_fingerprint["sha256"],
        }
    except Exception:
        if published and verification_receipt.exists():
            os.replace(verification_receipt, unconfirmed)
            try:
                _fsync_directory(verification_receipt.parent)
            except OSError:
                pass
        raise


def check_cold_restore_rehearsal_verification(
    receipt_path: Path,
    verification_receipt: Path,
    *,
    expected_receipt_sha256: str,
    expected_verification_receipt_sha256: str,
    expected_run_id: str,
    expected_candidate_commit: str,
) -> dict[str, Any]:
    """Recompute retained evidence without replacing either terminal receipt."""

    receipt_path = _absolute_without_symlinks(receipt_path)
    verification_receipt = _absolute_without_symlinks(verification_receipt)
    _assert_no_symlink_components(receipt_path)
    _assert_no_symlink_components(verification_receipt)
    if verification_receipt != receipt_path.parent / COLD_REHEARSAL_VERIFICATION_NAME:
        raise MigrationError("verification receipt must use the canonical rehearsal path")
    for value, label in (
        (expected_receipt_sha256, "expected rehearsal receipt"),
        (expected_verification_receipt_sha256, "expected verification receipt"),
    ):
        if re.fullmatch(r"[0-9a-f]{64}", value) is None:
            raise MigrationError(f"{label} SHA-256 is malformed")
    _canonical_operation_id(expected_run_id, label="expected rehearsal")
    _validate_candidate_commit(expected_candidate_commit)

    receipt_fingerprint = _fingerprint_regular_file(receipt_path, required=True)
    verification_fingerprint = _fingerprint_regular_file(
        verification_receipt, required=True
    )
    assert receipt_fingerprint is not None
    assert verification_fingerprint is not None
    if receipt_fingerprint["sha256"] != expected_receipt_sha256:
        raise VerificationError("rehearsal receipt differs from its out-of-band SHA-256 pin")
    if (
        verification_fingerprint["sha256"]
        != expected_verification_receipt_sha256
    ):
        raise VerificationError(
            "verification receipt differs from its out-of-band SHA-256 pin"
        )

    payload = _read_owned_json(receipt_path, label="cold restore rehearsal receipt")
    if (
        not isinstance(payload, dict)
        or payload.get("run_id") != expected_run_id
        or payload.get("candidate_commit") != expected_candidate_commit
    ):
        raise VerificationError("rehearsal receipt differs from out-of-band run/candidate pins")
    verification_payload = _read_owned_json(
        verification_receipt, label="cold restore rehearsal verification receipt"
    )
    if not isinstance(verification_payload, dict) or set(verification_payload) != {
        "schema_version",
        "tool",
        "kind",
        "created_at",
        "rehearsal_receipt",
        "rehearsal_receipt_sha256",
        "result",
    }:
        raise VerificationError("cold restore rehearsal verification receipt is malformed")
    if (
        verification_payload["schema_version"] != 1
        or verification_payload["tool"]
        != {"name": "agentstack-mail-migrate", "version": AGENTSTACK_MAIL_VERSION}
        or verification_payload["kind"] != "cold-restore-rehearsal-verification"
        or verification_payload["rehearsal_receipt"] != str(receipt_path)
        or verification_payload["rehearsal_receipt_sha256"]
        != receipt_fingerprint["sha256"]
    ):
        raise VerificationError("cold restore rehearsal verification receipt is inconsistent")
    _parse_utc_timestamp(verification_payload["created_at"], label="verification created_at")

    result = _verify_cold_rehearsal_payload(
        payload,
        receipt_path=receipt_path,
        allow_verification_receipt=True,
    )
    recorded_result = verification_payload["result"]
    if not isinstance(recorded_result, dict) or set(recorded_result) != set(result):
        raise VerificationError("recorded independent verification result is malformed")
    _parse_utc_timestamp(
        recorded_result.get("recomputed_at"), label="recorded recomputed_at"
    )
    stable_keys = set(result) - {"recomputed_at"}
    if {key: recorded_result[key] for key in stable_keys} != {
        key: result[key] for key in stable_keys
    }:
        raise VerificationError("recorded independent verification result no longer recomputes")
    if _fingerprint_regular_file(receipt_path, required=True) != receipt_fingerprint:
        raise VerificationError("rehearsal receipt changed during check-only verification")
    if (
        _fingerprint_regular_file(verification_receipt, required=True)
        != verification_fingerprint
    ):
        raise VerificationError("verification receipt changed during check-only verification")
    return {
        **result,
        "status": "verified_check_only",
        "rehearsal_receipt": str(receipt_path),
        "rehearsal_receipt_sha256": receipt_fingerprint["sha256"],
        "verification_receipt": str(verification_receipt),
        "verification_receipt_sha256": verification_fingerprint["sha256"],
    }


def _canonical_operation_id(value: Any, *, label: str) -> str:
    if not isinstance(value, str):
        raise MigrationError(f"{label} operation_id is not a UUID string")
    try:
        parsed = uuid.UUID(value)
    except ValueError as exc:
        raise MigrationError(f"{label} operation_id is not a canonical UUID") from exc
    if parsed.version != 4 or str(parsed) != value:
        raise MigrationError(f"{label} operation_id is not a canonical UUID4")
    return value


def _validate_staging_marker(payload: Any) -> str:
    if not isinstance(payload, dict) or set(payload) != {
        "schema_version",
        "operation_id",
        "kind",
    }:
        raise MigrationError("ownership marker has an unexpected shape")
    if type(payload["schema_version"]) is not int or payload["schema_version"] != 1:
        raise MigrationError("ownership marker has an unsupported schema version")
    if payload["kind"] != "owned-staging":
        raise MigrationError("ownership marker has an unexpected kind")
    return _canonical_operation_id(payload["operation_id"], label="ownership marker")


def _paths_overlap(source: StatePaths, destination_root: Path) -> bool:
    destination_root = destination_root.resolve(strict=False)
    for path in asdict(source.resolved()).values():
        candidate = Path(path)
        if candidate == destination_root or destination_root in candidate.parents:
            return True
        if candidate in destination_root.parents:
            return True
    return False


def _cleanup_owned_staging(parent: Path, destination_name: str) -> None:
    """Remove only abandoned staging dirs carrying our exact ownership marker."""

    prefix = f".{destination_name}.migration-"
    for candidate in parent.glob(f"{prefix}*"):
        if not candidate.is_dir() or candidate.is_symlink():
            continue
        marker = candidate / STAGING_MARKER
        try:
            operation_id = _validate_staging_marker(
                _read_owned_json(marker, label="staging ownership marker")
            )
        except MigrationError:
            continue
        if candidate.name == f"{prefix}{operation_id}":
            shutil.rmtree(candidate)


def _validate_published_generation(
    destination_root: Path,
    source: StatePaths,
    *,
    _source_database_connection: sqlite3.Connection | None = None,
) -> tuple[str, str]:
    """Validate an owned published generation without mutating it."""

    marker = destination_root / STAGING_MARKER
    if not marker.exists():
        raise MigrationError("destination has no unconfirmed publish marker")
    try:
        marker_payload = _read_owned_json(
            marker, label="published ownership marker"
        )
        operation_id = _validate_staging_marker(marker_payload)
    except MigrationError as exc:
        raise MigrationError(
            "destination is a published-but-unconfirmed generation with an "
            f"unreadable ownership marker: {exc}"
        ) from exc
    manifest_payload = _load_manifest(destination_root / MANIFEST_NAME)
    source_payload = (
        manifest_payload.get("source") if isinstance(manifest_payload, dict) else None
    )
    baseline = (
        manifest_payload.get("baseline") if isinstance(manifest_payload, dict) else None
    )
    destination_git = manifest_payload.get("destination_git")
    expected_source = {key: str(value) for key, value in asdict(source).items()}
    if (
        not isinstance(manifest_payload, dict)
        or manifest_payload.get("schema_version") != 1
        or manifest_payload.get("tool") != "agentstack-mail-migrate"
        or manifest_payload.get("status") != "C3_MIGRATION_VERIFIED"
        or manifest_payload.get("operation_id") != operation_id
        or manifest_payload.get("destination_root") != str(destination_root)
        or source_payload != expected_source
        or not isinstance(baseline, dict)
        or not isinstance(destination_git, dict)
    ):
        raise MigrationError(
            "destination is a published-but-unconfirmed generation whose "
            "ownership records do not match"
        )
    baseline_digest = baseline.get("state_sha256")
    if baseline_digest != _state_snapshot_digest(baseline):
        raise MigrationError(
            "published-but-unconfirmed baseline digest is internally inconsistent"
        )
    baseline_snapshot_digest = baseline.get("snapshot_sha256")
    if baseline_snapshot_digest != _snapshot_digest(baseline):
        raise MigrationError(
            "published-but-unconfirmed source snapshot digest is internally inconsistent"
        )
    source_now = snapshot_state(
        source, _database_connection=_source_database_connection
    )
    destination_now = snapshot_state(
        StatePaths.from_root(destination_root), require_baseline_git=True
    )
    if (
        source_now["snapshot_sha256"] != baseline_snapshot_digest
        or destination_now["state_sha256"] != baseline_digest
        or destination_now["git"] != destination_git
    ):
        raise VerificationError(
            "published-but-unconfirmed generation does not match its source baseline"
        )
    return operation_id, str(baseline_digest)


def _finalize_published_generation(
    destination_root: Path,
    source: StatePaths,
    *,
    _source_database_connection: sqlite3.Connection | None = None,
) -> tuple[str, str] | None:
    """Remove an owned publish marker only after read-only validation."""

    marker = destination_root / STAGING_MARKER
    if not marker.exists():
        return None
    result = _validate_published_generation(
        destination_root,
        source,
        _source_database_connection=_source_database_connection,
    )
    marker.unlink()
    for directory in (destination_root, destination_root.parent):
        descriptor = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    return result


def _verify_confirmed_generation(
    destination_root: Path,
    source: StatePaths,
    *,
    _source_database_connection: sqlite3.Connection | None = None,
) -> tuple[str, str]:
    if (destination_root / STAGING_MARKER).exists():
        raise MigrationError("destination still carries an unconfirmed publish marker")
    manifest = _load_manifest(destination_root / MANIFEST_NAME)
    expected_source = {key: str(value) for key, value in asdict(source).items()}
    baseline = manifest.get("baseline")
    destination_git = manifest.get("destination_git")
    if (
        manifest.get("source") != expected_source
        or not isinstance(baseline, dict)
        or not isinstance(destination_git, dict)
    ):
        raise MigrationError("confirmed generation ownership records do not match")
    baseline_digest = baseline.get("state_sha256")
    baseline_snapshot_digest = baseline.get("snapshot_sha256")
    if baseline_digest != _state_snapshot_digest(baseline):
        raise MigrationError("confirmed generation baseline is internally inconsistent")
    if baseline_snapshot_digest != _snapshot_digest(baseline):
        raise MigrationError("confirmed source snapshot is internally inconsistent")
    source_now = snapshot_state(
        source, _database_connection=_source_database_connection
    )
    destination_now = snapshot_state(
        StatePaths.from_root(destination_root), require_baseline_git=True
    )
    if source_now["snapshot_sha256"] != baseline_snapshot_digest:
        raise VerificationError("legacy source no longer matches the recorded baseline")
    if destination_now["state_sha256"] != baseline_digest:
        raise VerificationError("destination authority data does not match its baseline")
    if destination_now["git"] != destination_git:
        raise VerificationError("destination baseline Git no longer matches its manifest")
    return str(manifest["operation_id"]), str(baseline_digest)


def copy_state(
    source: StatePaths,
    destination_root: Path,
    *,
    fault_hook: FaultHook | None = None,
) -> MigrationResult:
    """Copy one quiesced authority into an atomically published destination."""

    source = source.resolved()
    destination_root = _absolute_without_symlinks(destination_root)
    _assert_no_symlink_components(destination_root)
    destination = StatePaths.from_root(destination_root).resolved()
    if source == destination:
        source_state = snapshot_state(source)
        return MigrationResult(
            status="noop",
            destination_root=str(destination_root),
            operation_id=None,
            state_sha256=str(source_state["state_sha256"]),
        )
    if _paths_overlap(source, destination_root):
        raise MigrationError("source and destination paths overlap")

    with _database_writer_guard(source.database) as source_database_connection:
        source_before = snapshot_state(
            source, _database_connection=source_database_connection
        )
        if destination_root.exists():
            if not destination_root.is_dir() or destination_root.is_symlink():
                raise MigrationError(
                    f"destination exists but is not a directory: {destination_root}"
                )
            try:
                destination_state = snapshot_state(
                    destination, require_baseline_git=True
                )
            except VerificationError as exc:
                raise MigrationError(
                    f"destination already exists with different state: {exc}"
                ) from exc
            if destination_state["state_sha256"] == source_before["state_sha256"]:
                recovered = _finalize_published_generation(
                    destination_root,
                    source,
                    _source_database_connection=source_database_connection,
                )
                if recovered is None:
                    _verify_confirmed_generation(
                        destination_root,
                        source,
                        _source_database_connection=source_database_connection,
                    )
                return MigrationResult(
                    status="recovered" if recovered is not None else "noop",
                    destination_root=str(destination_root),
                    operation_id=recovered[0] if recovered is not None else None,
                    state_sha256=(
                        recovered[1]
                        if recovered is not None
                        else str(source_before["state_sha256"])
                    ),
                )
            raise MigrationError("destination already exists with different state")

        parent = destination_root.parent
        if not parent.is_dir():
            raise MigrationError(f"destination parent must already exist: {parent}")
        parent_lock = os.open(parent, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            try:
                fcntl.flock(parent_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise MigrationError(
                    "another migration owns the destination parent"
                ) from exc
            _cleanup_owned_staging(parent, destination_root.name)
            return _copy_state_locked(
                source,
                source_before,
                destination_root,
                source_database_connection=source_database_connection,
                fault_hook=fault_hook,
            )
        finally:
            os.close(parent_lock)


def _copy_state_locked(
    source: StatePaths,
    source_before: dict[str, Any],
    destination_root: Path,
    *,
    source_database_connection: sqlite3.Connection,
    fault_hook: FaultHook | None,
) -> MigrationResult:
    parent = destination_root.parent
    operation_id = str(uuid.uuid4())
    staging = parent / f".{destination_root.name}.migration-{operation_id}"
    created_at = _utc_now()
    try:
        _call_fault(fault_hook, "before_staging")
        staging.mkdir(mode=0o700)
        _write_manifest(
            staging,
            {
                "schema_version": 1,
                "operation_id": operation_id,
                "kind": "owned-staging",
            },
        )
        (staging / MANIFEST_NAME).replace(staging / STAGING_MARKER)
        staging_descriptor = os.open(staging, os.O_RDONLY)
        try:
            os.fsync(staging_descriptor)
        finally:
            os.close(staging_descriptor)
        _copy_database(
            source.database,
            staging / "storage.sqlite3",
            fault_hook,
        )
        _copy_tree(
            source.archive,
            staging / "archive",
            required=True,
            hook=fault_hook,
            phase="archive_copy",
            excluded_root_names=ARCHIVE_EXCLUDED_ROOT_NAMES,
        )
        _copy_tree(
            source.signals,
            staging / "signals",
            required=False,
            hook=fault_hook,
            phase="signals_copy",
        )
        destination_git = _create_baseline_git(
            staging / "archive",
            authority_state_sha256=str(source_before["state_sha256"]),
            timestamp=created_at,
            hook=fault_hook,
        )
        _call_fault(fault_hook, "before_verification")
        staged_state = snapshot_state(
            StatePaths.from_root(staging), require_baseline_git=True
        )
        source_after = snapshot_state(
            source, _database_connection=source_database_connection
        )
        if source_before["snapshot_sha256"] != source_after["snapshot_sha256"]:
            raise VerificationError("source changed while migration was being copied")
        if staged_state["state_sha256"] != source_after["state_sha256"]:
            raise VerificationError("staged copy does not match the source")
        if staged_state["git"] != destination_git:
            raise VerificationError("staged baseline Git changed after it was created")
        manifest = {
            "schema_version": 1,
            "tool": "agentstack-mail-migrate",
            "operation_id": operation_id,
            "status": "C3_MIGRATION_VERIFIED",
            "created_at": created_at,
            "source": {key: str(value) for key, value in asdict(source).items()},
            "destination_root": str(destination_root),
            "baseline": source_after,
            "destination_git": destination_git,
            "archive_policy": dict(ARCHIVE_POLICY),
            "database_policy": dict(DATABASE_POLICY),
            "rollback": {
                "post_authority_reverse_transform": "not_implemented",
                "reversibility_boundary": "first_new_authority_durable_write",
                "client_switching_before_boundary": (
                    "reversible_if_both_authorities_equal_baseline"
                ),
            },
        }
        _write_manifest(staging, manifest)
        _fsync_tree(staging, fault_hook)
        _call_fault(fault_hook, "before_publish")
        source_final = snapshot_state(
            source, _database_connection=source_database_connection
        )
        if source_final["snapshot_sha256"] != source_after["snapshot_sha256"]:
            raise VerificationError("source changed before migration publication")
        if destination_root.exists() or destination_root.is_symlink():
            raise MigrationError("destination appeared before publication")
        os.replace(staging, destination_root)
        _call_fault(fault_hook, "after_publish")
        parent_descriptor = os.open(parent, os.O_RDONLY)
        try:
            os.fsync(parent_descriptor)
        finally:
            os.close(parent_descriptor)
        finalized = _finalize_published_generation(
            destination_root,
            source,
            _source_database_connection=source_database_connection,
        )
        if (
            finalized is None
            or finalized[0] != operation_id
            or finalized[1] != source_after["state_sha256"]
        ):
            raise VerificationError(
                "published generation did not retain the exact migration identity"
            )
        return MigrationResult(
            status="copied",
            destination_root=str(destination_root),
            operation_id=operation_id,
            state_sha256=str(source_after["state_sha256"]),
        )
    except Exception as primary:
        if staging.exists():
            try:
                shutil.rmtree(staging)
            except Exception as cleanup:
                primary.add_note(
                    "owned staging cleanup also failed: "
                    f"{type(cleanup).__name__}: {cleanup}"
                )
        raise


def verify_copy(source: StatePaths, destination_root: Path) -> dict[str, Any]:
    source = source.resolved()
    destination_root = destination_root.expanduser().absolute()
    if (destination_root / STAGING_MARKER).exists():
        _operation_id, state_sha256 = _validate_published_generation(
            destination_root, source
        )
    else:
        _operation_id, state_sha256 = _verify_confirmed_generation(
            destination_root, source
        )
    return {"status": "verified", "state_sha256": state_sha256}


def _load_manifest(path: Path) -> dict[str, Any]:
    payload = _read_owned_json(path, label="migration manifest")
    expected_keys = {
        "schema_version",
        "tool",
        "operation_id",
        "status",
        "created_at",
        "source",
        "destination_root",
        "baseline",
        "destination_git",
        "archive_policy",
        "database_policy",
        "rollback",
    }
    if not isinstance(payload, dict) or set(payload) != expected_keys:
        raise MigrationError("migration manifest has an unexpected shape")
    if type(payload["schema_version"]) is not int or payload["schema_version"] != 1:
        raise MigrationError("unsupported migration manifest schema version")
    if payload["tool"] != "agentstack-mail-migrate":
        raise MigrationError("manifest was not created by agentstack-mail-migrate")
    _canonical_operation_id(payload["operation_id"], label="migration manifest")
    if payload["status"] != "C3_MIGRATION_VERIFIED":
        raise MigrationError("migration manifest is not a verified C3 baseline")
    if not isinstance(payload["created_at"], str):
        raise MigrationError("migration manifest created_at is malformed")
    source = payload["source"]
    if not isinstance(source, dict) or set(source) != {"database", "archive", "signals"}:
        raise MigrationError("migration manifest source paths are malformed")
    for name, value in source.items():
        _canonical_absolute_path(value, label=f"manifest source.{name}")
    _canonical_absolute_path(
        payload["destination_root"], label="manifest destination_root"
    )
    if not isinstance(payload["baseline"], dict):
        raise MigrationError("migration manifest baseline is malformed")
    if not isinstance(payload["destination_git"], dict):
        raise MigrationError("migration manifest destination Git state is malformed")
    if payload["archive_policy"] != ARCHIVE_POLICY:
        raise MigrationError("migration manifest has an unexpected archive policy")
    if payload["database_policy"] != DATABASE_POLICY:
        raise MigrationError("migration manifest has an unexpected database policy")
    if payload["rollback"] != {
        "post_authority_reverse_transform": "not_implemented",
        "reversibility_boundary": "first_new_authority_durable_write",
        "client_switching_before_boundary": (
            "reversible_if_both_authorities_equal_baseline"
        ),
    }:
        raise MigrationError("migration manifest has an unexpected rollback policy")
    return payload


def assess_rollback(manifest_path: Path, cutover_stage: str) -> dict[str, Any]:
    """Assess a rollback without mutating data, services, or clients."""

    if cutover_stage not in ASSESSABLE_STAGES:
        raise MigrationError(
            "rollback-assess requires a migration manifest and therefore only "
            f"accepts C3-C6, not {cutover_stage!r}"
        )
    manifest = _load_manifest(manifest_path)
    source_payload = manifest.get("source")
    baseline = manifest.get("baseline")
    if not isinstance(source_payload, dict) or not isinstance(baseline, dict):
        raise MigrationError("manifest is missing source or baseline state")
    baseline_digest = baseline.get("state_sha256")
    if baseline_digest != _state_snapshot_digest(baseline):
        raise MigrationError("manifest baseline digest is internally inconsistent")
    source = StatePaths(
        database=Path(str(source_payload["database"])),
        archive=Path(str(source_payload["archive"])),
        signals=Path(str(source_payload["signals"])),
    )
    destination_root = Path(str(manifest["destination_root"]))
    baseline_snapshot_digest = baseline.get("snapshot_sha256")
    if baseline_snapshot_digest != _snapshot_digest(baseline):
        raise MigrationError("manifest source snapshot digest is internally inconsistent")
    destination_git = manifest.get("destination_git")
    if not isinstance(destination_git, dict):
        raise MigrationError("manifest is missing destination baseline Git state")
    source_error: str | None = None
    destination_error: str | None = None
    try:
        source_now = snapshot_state(source)
        source_matches = source_now["snapshot_sha256"] == baseline_snapshot_digest
    except VerificationError as exc:
        source_matches = False
        source_error = str(exc)
    try:
        destination_now = snapshot_state(
            StatePaths.from_root(destination_root), require_baseline_git=True
        )
        destination_matches = (
            destination_now["state_sha256"] == baseline_digest
            and destination_now["git"] == destination_git
        )
    except VerificationError as exc:
        destination_matches = False
        destination_error = str(exc)
    if cutover_stage == "C6_NEW_AUTHORITY_VERIFIED":
        reversible = False
        reason = (
            "caller asserted C6, which is at or beyond the first durable new-authority "
            "write boundary; rollback is fix-forward-only even if both snapshots still "
            "equal the migration baseline"
        )
    elif not source_matches:
        reversible = False
        reason = "legacy source no longer equals its pre-cutover baseline"
    elif destination_matches:
        reversible = True
        reason = "new authority contains no durable writes after the migration baseline"
    else:
        reversible = False
        reason = (
            "new authority diverged after baseline and no verified reverse transform exists; "
            "do not partially merge records"
        )

    reversible_actions_by_stage = {
        "C3_MIGRATION_VERIFIED": [
            "retain the verified copy for diagnosis",
            "start only the unchanged legacy service",
        ],
        "C4_NEW_SERVICE_READY": [
            "stop the new service",
            "verify it still equals the migration baseline",
            "start the legacy service and verify clients still target it",
        ],
        "C5_CLIENT_SWITCHING": [
            "quiesce all consumers and stop the new service",
            "proceed only if this assessment reports reversible=true",
            "restore client before-images only with compare-and-swap checks",
        ],
    }
    if cutover_stage == "C6_NEW_AUTHORITY_VERIFIED":
        actions = [
            "keep all consumers quiesced and keep the legacy service stopped",
            "stop and inspect only the exact owned new job",
            "repair the new authority in place and start only that exact owned new job",
            "require bounded MCP readiness before resuming consumers",
            (
                "if the new job cannot become ready, start neither authority and "
                "enter incident/no-writer state"
            ),
        ]
    elif reversible:
        actions = reversible_actions_by_stage[cutover_stage]
    elif not destination_matches:
        actions = [
            "keep all consumers quiesced and keep the legacy service stopped",
            "start only the exact owned new job for fix-forward",
            "require bounded MCP readiness before resuming consumers",
            (
                "if the new job cannot become ready, start neither authority and "
                "enter incident/no-writer state"
            ),
        ]
    else:
        actions = [
            "keep all consumers quiesced",
            "start neither authority automatically because the legacy baseline drifted",
            "enter incident/no-writer state until the divergence is reconciled",
        ]
    return {
        "status": "reversible" if reversible else "no_go",
        "cutover_stage": cutover_stage,
        "cutover_stage_provenance": "caller_asserted_unverified",
        "source_matches_baseline": source_matches,
        "destination_matches_baseline": destination_matches,
        "data_reversible": reversible,
        "reason": reason,
        "source_verification_error": source_error,
        "destination_verification_error": destination_error,
        "actions": actions,
        "service_and_client_state_requires_external_verification": True,
    }


def _state_paths_from_args(args: argparse.Namespace) -> StatePaths:
    return StatePaths(
        database=_canonical_absolute_path(args.source_db, label="--source-db"),
        archive=_canonical_absolute_path(
            args.source_archive, label="--source-archive"
        ),
        signals=_canonical_absolute_path(
            args.source_signals, label="--source-signals"
        ),
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agentstack-mail-migrate",
        description=(
            "Copy and verify AgentStack Mail state without controlling a service. "
            "All paths must be canonical absolute paths without symlink components."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    descriptions = {
        "copy": (
            "Copy a quiesced SQLite database, signals, and archive working tree; "
            "exclude legacy .git/server.pid and create one unrelated baseline commit."
        ),
        "verify": (
            "Read and verify source plus destination against the recorded logical "
            "database, working-tree, signals, and baseline-Git policy."
        ),
    }
    for name, description in descriptions.items():
        command = subparsers.add_parser(name, description=description)
        command.add_argument(
            "--source-db", required=True, help="canonical absolute SQLite main-file path"
        )
        command.add_argument(
            "--source-archive",
            required=True,
            help="canonical absolute legacy archive worktree path",
        )
        command.add_argument(
            "--source-signals",
            required=True,
            help="canonical absolute legacy signals directory",
        )
        command.add_argument(
            "--destination-root",
            required=True,
            help="absent destination below an existing same-filesystem parent",
        )
    rollback = subparsers.add_parser(
        "rollback-assess",
        description=(
            "Read a verified migration manifest and fail closed when either authority "
            "has diverged from the recorded logical baseline."
        ),
    )
    rollback.add_argument(
        "--manifest",
        required=True,
        help="canonical absolute migration-manifest.json path",
    )
    rollback.add_argument(
        "--cutover-stage",
        required=True,
        choices=ASSESSABLE_STAGES,
        help="caller-asserted C3-C6 stage; the tool does not infer service state",
    )
    cold_backup = subparsers.add_parser(
        "cold-backup",
        description=(
            "Seal the raw SQLite main/WAL/SHM family before migration opens it. "
            "Both authorities must already be stopped."
        ),
    )
    cold_backup.add_argument(
        "--source-db", required=True, help="canonical absolute SQLite main-file path"
    )
    cold_backup.add_argument(
        "--backup-dir", required=True, help="canonical absolute absent backup directory"
    )
    cold_backup.add_argument(
        "--services-stopped",
        action="store_true",
        required=True,
        help="caller assertion that both mail authorities are stopped",
    )
    cold_restore = subparsers.add_parser(
        "cold-restore",
        description=(
            "Restore PRESENT raw files by atomic replace, remove ABSENT sidecars, "
            "and accept the result by logical equality."
        ),
    )
    cold_restore.add_argument(
        "--backup-dir", required=True, help="canonical absolute sealed backup directory"
    )
    cold_restore.add_argument(
        "--destination-db",
        required=True,
        help="canonical absolute SQLite main-file restore target",
    )
    cold_restore.add_argument(
        "--restore-receipt",
        required=True,
        help="canonical absolute absent path for the machine-readable restore receipt",
    )
    cold_restore.add_argument(
        "--migration-manifest",
        required=True,
        help="canonical absolute verified C3 manifest binding backup and logical baseline",
    )
    cold_restore.add_argument(
        "--services-stopped",
        action="store_true",
        required=True,
        help="caller assertion that both mail authorities are stopped",
    )
    cold_restore.add_argument(
        "--target-kind",
        required=True,
        choices=("rehearsal-copy", "production-source"),
        help="distinguish a non-production rehearsal from an operator-authorized restore",
    )
    cold_restore.add_argument(
        "--fault-injection",
        required=True,
        help="caller-asserted injected fault description; rehearsal-copy cannot use 'none'",
    )
    rehearsal = subparsers.add_parser(
        "cold-restore-rehearse",
        description=(
            "On an isolated seed, retain source/backup/damaged/restored raw families, "
            "apply one built-in non-no-op fault, restore, and publish a terminal receipt."
        ),
    )
    rehearsal.add_argument(
        "--seed-db", required=True, help="canonical absolute isolated seed SQLite main file"
    )
    rehearsal.add_argument(
        "--production-source-db",
        required=True,
        help="canonical production path that the isolated seed must not equal",
    )
    rehearsal.add_argument(
        "--run-dir", required=True, help="canonical absolute absent evidence directory"
    )
    rehearsal.add_argument(
        "--migration-manifest",
        required=True,
        help="verified C3 manifest whose source database is the isolated seed",
    )
    rehearsal.add_argument(
        "--candidate-repo",
        required=True,
        help="canonical clean Git checkout whose HEAD is the candidate commit",
    )
    rehearsal.add_argument(
        "--candidate-commit",
        required=True,
        help="full lowercase 40-hex candidate Git commit",
    )
    rehearsal.add_argument(
        "--seed-provenance",
        required=True,
        help="canonical JSON acquisition receipt binding seed and production paths",
    )
    rehearsal.add_argument(
        "--generator-receipt",
        required=True,
        help="canonical candidate-bound synthetic seed generator receipt",
    )
    rehearsal.add_argument(
        "--expected-generator-receipt-sha256",
        required=True,
        help="out-of-run SHA-256 pin for the synthetic seed generator receipt",
    )
    rehearsal_verify = subparsers.add_parser(
        "cold-restore-rehearsal-verify",
        description=(
            "Independently re-hash and logically re-open retained rehearsal raw artifacts; "
            "a final boolean without raw artifacts is unverifiable."
        ),
    )
    rehearsal_verify.add_argument(
        "--receipt",
        required=True,
        help="canonical absolute cold-restore-rehearsal-receipt.json path",
    )
    rehearsal_verify.add_argument(
        "--verification-receipt",
        required=True,
        help=(
            "canonical verifier receipt path: absent for initial publication, "
            "existing for --check-only"
        ),
    )
    rehearsal_verify.add_argument("--expected-receipt-sha256", required=True)
    rehearsal_verify.add_argument(
        "--expected-verification-receipt-sha256",
        help="required with --check-only; external pin for the existing verifier receipt",
    )
    rehearsal_verify.add_argument("--expected-run-id", required=True)
    rehearsal_verify.add_argument("--expected-candidate-commit", required=True)
    rehearsal_verify.add_argument(
        "--check-only",
        action="store_true",
        help="recompute an existing verifier receipt without writing or replacing evidence",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    args = _parser().parse_args(argv)
    try:
        if args.command == "copy":
            result: Any = asdict(
                copy_state(
                    _state_paths_from_args(args),
                    _canonical_absolute_path(
                        args.destination_root, label="--destination-root"
                    ),
                )
            )
        elif args.command == "verify":
            result = verify_copy(
                _state_paths_from_args(args),
                _canonical_absolute_path(
                    args.destination_root, label="--destination-root"
                ),
            )
        elif args.command == "rollback-assess":
            result = assess_rollback(
                _canonical_absolute_path(args.manifest, label="--manifest"),
                args.cutover_stage,
            )
        elif args.command == "cold-backup":
            result = cold_backup_database(
                _canonical_absolute_path(args.source_db, label="--source-db"),
                _canonical_absolute_path(args.backup_dir, label="--backup-dir"),
                services_stopped=args.services_stopped,
            )
        elif args.command == "cold-restore":
            result = cold_restore_database(
                _canonical_absolute_path(args.backup_dir, label="--backup-dir"),
                _canonical_absolute_path(
                    args.destination_db, label="--destination-db"
                ),
                _canonical_absolute_path(
                    args.restore_receipt, label="--restore-receipt"
                ),
                _canonical_absolute_path(
                    args.migration_manifest, label="--migration-manifest"
                ),
                services_stopped=args.services_stopped,
                target_kind=args.target_kind,
                fault_injection=args.fault_injection,
            )
        elif args.command == "cold-restore-rehearse":
            result = rehearse_cold_restore(
                _canonical_absolute_path(args.seed_db, label="--seed-db"),
                _canonical_absolute_path(
                    args.production_source_db, label="--production-source-db"
                ),
                _canonical_absolute_path(args.run_dir, label="--run-dir"),
                _canonical_absolute_path(
                    args.migration_manifest, label="--migration-manifest"
                ),
                _canonical_absolute_path(
                    args.candidate_repo, label="--candidate-repo"
                ),
                _canonical_absolute_path(
                    args.seed_provenance, label="--seed-provenance"
                ),
                generator_receipt=_canonical_absolute_path(
                    args.generator_receipt, label="--generator-receipt"
                ),
                candidate_commit=args.candidate_commit,
                expected_generator_receipt_sha256=(
                    args.expected_generator_receipt_sha256
                ),
            )
        else:
            receipt_path = _canonical_absolute_path(args.receipt, label="--receipt")
            verification_receipt = _canonical_absolute_path(
                args.verification_receipt, label="--verification-receipt"
            )
            if args.check_only:
                if args.expected_verification_receipt_sha256 is None:
                    raise MigrationError(
                        "--expected-verification-receipt-sha256 is required with "
                        "--check-only"
                    )
                result = check_cold_restore_rehearsal_verification(
                    receipt_path,
                    verification_receipt,
                    expected_receipt_sha256=args.expected_receipt_sha256,
                    expected_verification_receipt_sha256=(
                        args.expected_verification_receipt_sha256
                    ),
                    expected_run_id=args.expected_run_id,
                    expected_candidate_commit=args.expected_candidate_commit,
                )
            else:
                if args.expected_verification_receipt_sha256 is not None:
                    raise MigrationError(
                        "--expected-verification-receipt-sha256 requires --check-only"
                    )
                result = verify_cold_restore_rehearsal(
                    receipt_path,
                    verification_receipt,
                    expected_receipt_sha256=args.expected_receipt_sha256,
                    expected_run_id=args.expected_run_id,
                    expected_candidate_commit=args.expected_candidate_commit,
                )
    except (MigrationError, OSError, sqlite3.Error) as exc:
        print(f"agentstack-mail-migrate: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    if isinstance(result, dict) and result.get("status") == "no_go":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
