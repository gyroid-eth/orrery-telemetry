"""Atomic, copy-only migration and fail-closed rollback assessment.

The migration command deliberately accepts every path explicitly.  It never
loads the running service configuration and never starts, stops, or contacts a
service.  Database, archive, and signal state are staged below one sibling
directory and become visible together through one directory rename.
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
from collections.abc import Callable, Iterable, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final


MANIFEST_NAME: Final[str] = "migration-manifest.json"
STAGING_MARKER: Final[str] = ".agentstack-mail-migration-staging.json"
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
        return StatePaths(
            database=self.database.expanduser().resolve(strict=False),
            archive=self.archive.expanduser().resolve(strict=False),
            signals=self.signals.expanduser().resolve(strict=False),
        )


@dataclass(frozen=True, slots=True)
class MigrationResult:
    status: str
    destination_root: str
    operation_id: str | None
    state_sha256: str


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


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
    """Return a credential-safe logical snapshot of every SQLite table."""

    path = path.expanduser().resolve(strict=False)
    if not path.is_file():
        raise VerificationError(f"database is missing or not a file: {path}")
    uri = f"file:{path.as_posix()}?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True)
    except sqlite3.Error as exc:
        raise VerificationError(f"cannot open database read-only: {path}: {exc}") from exc
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
    finally:
        connection.close()


def _tree_entries(root: Path) -> Iterable[tuple[Path, Path]]:
    root = root.resolve(strict=True)
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if path.name.endswith(".lock"):
            raise VerificationError(
                f"active or stale writer lock must be resolved before migration: {path}"
            )
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise VerificationError(f"symbolic links are not accepted: {root / relative}")
        if stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode):
            yield path, relative
            continue
        raise VerificationError(f"special filesystem entry is not accepted: {root / relative}")


def snapshot_tree(root: Path, *, required: bool) -> dict[str, Any]:
    root = root.expanduser().resolve(strict=False)
    if not root.exists():
        if required:
            raise VerificationError(f"required directory is missing: {root}")
        return {"exists": False, "entries": 0, "sha256": _sha256([])}
    if not root.is_dir() or root.is_symlink():
        raise VerificationError(f"state tree is not a real directory: {root}")
    entries: list[list[Any]] = [
        ["directory", ".", stat.S_IMODE(root.lstat().st_mode)]
    ]
    for path, relative in _tree_entries(root):
        info = path.lstat()
        mode = stat.S_IMODE(info.st_mode)
        if path.is_dir():
            entries.append(["directory", relative.as_posix(), mode])
        else:
            digest = hashlib.sha256()
            with path.open("rb") as handle:
                while chunk := handle.read(1024 * 1024):
                    digest.update(chunk)
            entries.append(
                ["file", relative.as_posix(), mode, info.st_size, digest.hexdigest()]
            )
    return {"exists": True, "entries": len(entries), "sha256": _sha256(entries)}


def _git_snapshot(root: Path) -> dict[str, Any]:
    if not (root / ".git").exists():
        raise VerificationError(f"archive is not a normal Git worktree: {root}")

    def run(*arguments: str) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment["GIT_OPTIONAL_LOCKS"] = "0"
        return subprocess.run(
            ["git", "-C", str(root), *arguments],
            check=False,
            capture_output=True,
            text=True,
            env=environment,
        )

    fsck = run("fsck", "--full")
    if fsck.returncode != 0:
        raise VerificationError(f"Git archive fsck failed: {fsck.stderr.strip()}")
    head = run("rev-parse", "--verify", "HEAD")
    refs = run("for-each-ref", "--format=%(refname) %(objectname)")
    status_result = run("status", "--porcelain=v1", "--untracked-files=all")
    for name, result in (("HEAD", head), ("refs", refs), ("status", status_result)):
        if result.returncode != 0:
            raise VerificationError(
                f"Git archive {name} inspection failed: {result.stderr.strip()}"
            )
    state = {
        "exists": True,
        "head": head.stdout.strip(),
        "refs_sha256": hashlib.sha256(refs.stdout.encode()).hexdigest(),
        "status_sha256": hashlib.sha256(status_result.stdout.encode()).hexdigest(),
    }
    return {**state, "sha256": _sha256(state)}


def snapshot_state(paths: StatePaths) -> dict[str, Any]:
    paths = paths.resolved()
    database = snapshot_database(paths.database)
    archive = snapshot_tree(paths.archive, required=True)
    signals = snapshot_tree(paths.signals, required=False)
    git = _git_snapshot(paths.archive)
    state = {
        "database": database,
        "archive": archive,
        "signals": signals,
        "git": git,
    }
    return {**state, "state_sha256": _state_snapshot_digest(state)}


def _state_snapshot_digest(state: dict[str, Any]) -> str:
    try:
        database = state["database"]
        logical_database = {
            "schema_sha256": database["schema_sha256"],
            "pragmas": database["pragmas"],
            "tables": database["tables"],
            "relations": database["relations"],
        }
        logical_sha256 = _sha256(logical_database)
        if logical_sha256 != database["logical_sha256"]:
            raise VerificationError("database snapshot digest is internally inconsistent")
        git = state["git"]
        git_logical = {key: value for key, value in git.items() if key != "sha256"}
        if _sha256(git_logical) != git.get("sha256"):
            raise VerificationError("Git snapshot digest is internally inconsistent")
        comparable = {
            "database": logical_sha256,
            "archive": state["archive"],
            "signals": state["signals"],
            "git": git,
        }
    except (KeyError, TypeError) as exc:
        raise VerificationError("state snapshot is malformed") from exc
    return _sha256(comparable)


def _call_fault(hook: FaultHook | None, phase: str) -> None:
    if phase not in MIGRATION_FAULT_PHASES:
        raise AssertionError(f"unenumerated migration fault seam: {phase}")
    if hook is not None:
        hook(phase)


def _copy_database(source: Path, destination: Path, hook: FaultHook | None) -> None:
    _call_fault(hook, "before_database_backup")
    source_connection = sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True)
    destination_connection = sqlite3.connect(destination)
    try:
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
        destination_connection.close()
        source_connection.close()
    _call_fault(hook, "after_database_backup")


def _copy_tree(
    source: Path,
    destination: Path,
    *,
    required: bool,
    hook: FaultHook | None,
    phase: str,
) -> None:
    if not source.exists():
        if required:
            raise MigrationError(f"required source directory is missing: {source}")
        return
    destination.mkdir(mode=0o700)
    directories: list[tuple[Path, Path]] = [(source, destination)]
    for path, relative in _tree_entries(source):
        target = destination / relative
        if path.is_dir():
            target.mkdir(mode=0o700)
            directories.append((path, target))
            continue
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        _call_fault(hook, f"{phase}:before_file")
        with path.open("rb") as reader, target.open("xb") as writer:
            while chunk := reader.read(1024 * 1024):
                _call_fault(hook, f"{phase}:copy_chunk")
                writer.write(chunk)
            writer.flush()
            os.fsync(writer.fileno())
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
            payload = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        operation_id = payload.get("operation_id")
        if (
            payload.get("schema_version") == 1
            and payload.get("kind") == "owned-staging"
            and isinstance(operation_id, str)
            and candidate.name == f"{prefix}{operation_id}"
        ):
            shutil.rmtree(candidate)


def _finalize_published_generation(
    destination_root: Path, source: StatePaths
) -> tuple[str, str] | None:
    """Remove an owned publish marker only after matching both manifests."""

    marker = destination_root / STAGING_MARKER
    if not marker.exists():
        return None
    try:
        marker_payload = json.loads(marker.read_text(encoding="utf-8"))
        manifest_payload = json.loads(
            (destination_root / MANIFEST_NAME).read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError) as exc:
        raise MigrationError(
            "destination is a published-but-unconfirmed generation with an "
            f"unreadable ownership marker: {exc}"
        ) from exc
    operation_id = (
        marker_payload.get("operation_id") if isinstance(marker_payload, dict) else None
    )
    source_payload = (
        manifest_payload.get("source") if isinstance(manifest_payload, dict) else None
    )
    baseline = (
        manifest_payload.get("baseline") if isinstance(manifest_payload, dict) else None
    )
    expected_source = {key: str(value) for key, value in asdict(source).items()}
    if (
        not isinstance(marker_payload, dict)
        or marker_payload.get("schema_version") != 1
        or marker_payload.get("kind") != "owned-staging"
        or not isinstance(operation_id, str)
        or not isinstance(manifest_payload, dict)
        or manifest_payload.get("schema_version") != 1
        or manifest_payload.get("tool") != "agentstack-mail-migrate"
        or manifest_payload.get("status") != "C3_MIGRATION_VERIFIED"
        or manifest_payload.get("operation_id") != operation_id
        or manifest_payload.get("destination_root") != str(destination_root)
        or source_payload != expected_source
        or not isinstance(baseline, dict)
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
    source_now = snapshot_state(source)
    destination_now = snapshot_state(StatePaths.from_root(destination_root))
    if (
        source_now["state_sha256"] != baseline_digest
        or destination_now["state_sha256"] != baseline_digest
    ):
        raise VerificationError(
            "published-but-unconfirmed generation no longer matches its source baseline"
        )
    marker.unlink()
    for directory in (destination_root, destination_root.parent):
        descriptor = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    return operation_id, str(baseline_digest)


def copy_state(
    source: StatePaths,
    destination_root: Path,
    *,
    fault_hook: FaultHook | None = None,
) -> MigrationResult:
    """Copy one quiesced authority into an atomically published destination."""

    source = source.resolved()
    destination_root = destination_root.expanduser().resolve(strict=False)
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

    source_before = snapshot_state(source)
    if destination_root.exists():
        if not destination_root.is_dir() or destination_root.is_symlink():
            raise MigrationError(f"destination exists but is not a directory: {destination_root}")
        destination_state = snapshot_state(destination)
        if destination_state["state_sha256"] == source_before["state_sha256"]:
            recovered = _finalize_published_generation(destination_root, source)
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
    parent_lock = os.open(parent, os.O_RDONLY)
    try:
        try:
            fcntl.flock(parent_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise MigrationError("another migration owns the destination parent") from exc
        _cleanup_owned_staging(parent, destination_root.name)
        return _copy_state_locked(
            source,
            source_before,
            destination_root,
            fault_hook=fault_hook,
        )
    finally:
        os.close(parent_lock)


def _copy_state_locked(
    source: StatePaths,
    source_before: dict[str, Any],
    destination_root: Path,
    *,
    fault_hook: FaultHook | None,
) -> MigrationResult:
    parent = destination_root.parent
    operation_id = str(uuid.uuid4())
    staging = parent / f".{destination_root.name}.migration-{operation_id}"
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
        _copy_database(source.database, staging / "storage.sqlite3", fault_hook)
        _copy_tree(
            source.archive,
            staging / "archive",
            required=True,
            hook=fault_hook,
            phase="archive_copy",
        )
        _copy_tree(
            source.signals,
            staging / "signals",
            required=False,
            hook=fault_hook,
            phase="signals_copy",
        )
        _call_fault(fault_hook, "before_verification")
        staged_state = snapshot_state(StatePaths.from_root(staging))
        source_after = snapshot_state(source)
        if source_before["state_sha256"] != source_after["state_sha256"]:
            raise VerificationError("source changed while migration was being copied")
        if staged_state["state_sha256"] != source_after["state_sha256"]:
            raise VerificationError("staged copy does not match the source")
        manifest = {
            "schema_version": 1,
            "tool": "agentstack-mail-migrate",
            "operation_id": operation_id,
            "status": "C3_MIGRATION_VERIFIED",
            "created_at": _utc_now(),
            "source": {key: str(value) for key, value in asdict(source).items()},
            "destination_root": str(destination_root),
            "baseline": source_after,
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
        source_final = snapshot_state(source)
        if source_final["state_sha256"] != source_after["state_sha256"]:
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
        finalized = _finalize_published_generation(destination_root, source)
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
    source_state = snapshot_state(source)
    destination_state = snapshot_state(StatePaths.from_root(destination_root))
    matches = source_state["state_sha256"] == destination_state["state_sha256"]
    if not matches:
        raise VerificationError("destination state does not match source state")
    return {"status": "verified", "state_sha256": source_state["state_sha256"]}


def _load_manifest(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MigrationError(f"cannot read migration manifest {path}: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise MigrationError("unsupported or malformed migration manifest")
    if payload.get("tool") != "agentstack-mail-migrate":
        raise MigrationError("manifest was not created by agentstack-mail-migrate")
    if payload.get("status") != "C3_MIGRATION_VERIFIED":
        raise MigrationError("migration manifest is not a verified C3 baseline")
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
    source_now = snapshot_state(source)
    destination_now = snapshot_state(StatePaths.from_root(destination_root))
    source_matches = source_now["state_sha256"] == baseline_digest
    destination_matches = destination_now["state_sha256"] == baseline_digest
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
        "actions": actions,
        "service_and_client_state_requires_external_verification": True,
    }


def _state_paths_from_args(args: argparse.Namespace) -> StatePaths:
    return StatePaths(
        database=Path(args.source_db),
        archive=Path(args.source_archive),
        signals=Path(args.source_signals),
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agentstack-mail-migrate",
        description="Copy and verify AgentStack Mail state without touching a service.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in ("copy", "verify"):
        command = subparsers.add_parser(name)
        command.add_argument("--source-db", required=True)
        command.add_argument("--source-archive", required=True)
        command.add_argument("--source-signals", required=True)
        command.add_argument("--destination-root", required=True)
    rollback = subparsers.add_parser("rollback-assess")
    rollback.add_argument("--manifest", required=True)
    rollback.add_argument("--cutover-stage", required=True, choices=ASSESSABLE_STAGES)
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    args = _parser().parse_args(argv)
    try:
        if args.command == "copy":
            result: Any = asdict(
                copy_state(_state_paths_from_args(args), Path(args.destination_root))
            )
        elif args.command == "verify":
            result = verify_copy(
                _state_paths_from_args(args), Path(args.destination_root)
            )
        else:
            result = assess_rollback(Path(args.manifest), args.cutover_stage)
    except (MigrationError, OSError, sqlite3.Error) as exc:
        print(f"agentstack-mail-migrate: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    if isinstance(result, dict) and result.get("status") == "no_go":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
