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
    "st_nlink",
    "st_size",
    "st_mtime_ns",
    "st_ctime_ns",
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
    """Fingerprint the directory whose entry names the database parent."""

    path = _absolute_without_symlinks(path)
    container = path.parent.parent
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
    ):
        environment.pop(name, None)
    environment.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_OPTIONAL_LOCKS": "0",
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
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MigrationError(f"cannot parse {label} {path}: {exc}") from exc


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
                "last_reversible_stage_without_new_durable_writes": (
                    "C4_NEW_SERVICE_READY"
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
    except Exception:
        if staging.exists():
            shutil.rmtree(staging)
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
        "last_reversible_stage_without_new_durable_writes": "C4_NEW_SERVICE_READY",
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
    if not source_matches:
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
        "C6_NEW_AUTHORITY_VERIFIED": [
            "quiesce all consumers and stop the new service",
            "restore client before-images with compare-and-swap checks",
            "start only the unchanged legacy service and verify client handshakes",
        ],
    }
    if reversible:
        actions = reversible_actions_by_stage[cutover_stage]
    elif not destination_matches:
        actions = [
            "keep all consumers quiesced and keep the legacy service stopped",
            "start only the exact owned new job for fix-forward",
            "require bounded MCP readiness before resuming consumers",
            "if the new job cannot become ready, start neither authority and enter incident/no-writer state",
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
        else:
            result = assess_rollback(
                _canonical_absolute_path(args.manifest, label="--manifest"),
                args.cutover_stage,
            )
    except (MigrationError, OSError, sqlite3.Error) as exc:
        print(f"agentstack-mail-migrate: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    if isinstance(result, dict) and result.get("status") == "no_go":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
