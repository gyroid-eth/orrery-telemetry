"""Build one candidate-bound, production-shaped synthetic rehearsal seed."""

from __future__ import annotations

import argparse
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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

PACKAGE_SOURCE = Path(__file__).resolve().parents[1] / "src"
if str(PACKAGE_SOURCE) not in sys.path:
    sys.path.insert(0, str(PACKAGE_SOURCE))

from agentstack_mail import migration

AGENT_COUNT = 800
MESSAGE_COUNT = 8_200
RECIPIENT_COUNT = MESSAGE_COUNT
RESERVATION_COUNT = 2_000
BODY_BYTES = 8_192
MINIMUM_DATABASE_BYTES = 50 * 1024 * 1024
SCRIPT_RELATIVE = Path(
    "packages/agentstack_mail/scripts/build_rehearsal_seed.py"
)
MIGRATION_RELATIVE = Path(
    "packages/agentstack_mail/src/agentstack_mail/migration.py"
)


class SeedBuildError(RuntimeError):
    """The synthetic seed could not be built or candidate-bound."""


def _git_environment() -> dict[str, str]:
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
    return environment


def _canonical_absolute(path: Path, *, label: str) -> Path:
    value = os.fspath(path.expanduser())
    absolute = Path(os.path.abspath(value))
    if not path.is_absolute() or os.path.normpath(value) != value or str(absolute) != value:
        raise SeedBuildError(f"{label} must be a canonical absolute path")
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current /= part
        try:
            info = current.lstat()
        except FileNotFoundError:
            break
        if stat.S_ISLNK(info.st_mode):
            raise SeedBuildError(f"{label} contains a symbolic path component: {current}")
    return absolute


def _git(repository: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        [
            "git",
            "-c",
            f"core.hooksPath={os.devnull}",
            "-C",
            str(repository),
            *arguments,
        ],
        check=False,
        capture_output=True,
        text=True,
        env=_git_environment(),
        timeout=30,
    )
    if result.returncode != 0:
        diagnostic = (result.stderr or result.stdout).strip()
        raise SeedBuildError(f"git {' '.join(arguments)} failed: {diagnostic}")
    return result


def _archive_git(repository: Path, *arguments: str) -> None:
    subprocess.run(
        [
            "git",
            "-c",
            f"core.hooksPath={os.devnull}",
            "-c",
            "commit.gpgSign=false",
            "-C",
            str(repository),
            *arguments,
        ],
        check=True,
        timeout=30,
        env=_git_environment(),
    )


def _candidate_identity(repository: Path, candidate_commit: str) -> dict[str, Any]:
    if (
        re.fullmatch(r"[0-9a-f]{40}", candidate_commit) is None
        or candidate_commit == "0" * 40
    ):
        raise SeedBuildError("candidate commit must be one full lowercase SHA-1")
    if not repository.is_dir() or repository.is_symlink():
        raise SeedBuildError("candidate repository must be a real directory")
    head = _git(repository, "rev-parse", "--verify", "HEAD^{commit}").stdout.strip()
    if head != candidate_commit:
        raise SeedBuildError("candidate commit must be the exact checkout HEAD")
    if _git(repository, "status", "--porcelain").stdout:
        raise SeedBuildError("candidate repository must be completely clean")

    expected_files = {
        SCRIPT_RELATIVE: Path(__file__).resolve(),
        MIGRATION_RELATIVE: Path(migration.__file__).resolve(),
    }
    identities: dict[str, Any] = {}
    for relative, executing in expected_files.items():
        candidate = subprocess.run(
            [
                "git",
                "-c",
                f"core.hooksPath={os.devnull}",
                "-C",
                str(repository),
                "show",
                f"{candidate_commit}:{relative.as_posix()}",
            ],
            check=False,
            capture_output=True,
            env=_git_environment(),
            timeout=30,
        )
        executing_bytes = executing.read_bytes()
        if candidate.returncode != 0 or candidate.stdout != executing_bytes:
            raise SeedBuildError(
                f"executing {relative.name} bytes must equal the candidate commit blob"
            )
        identities[relative.as_posix()] = hashlib.sha256(executing_bytes).hexdigest()
    return {
        "repository": str(repository),
        "head": head,
        "tracked_and_untracked_worktree_clean": True,
        "executing_file_sha256": identities,
    }


def _message_rows() -> Iterator[tuple[Any, ...]]:
    for message_id in range(1, MESSAGE_COUNT + 1):
        prefix = f"message-{message_id:05d}:"
        body = prefix + "x" * (BODY_BYTES - len(prefix))
        yield (
            message_id,
            1,
            ((message_id - 1) % AGENT_COUNT) + 1,
            f"thread-{((message_id - 1) // 20) + 1}",
            f"synthetic subject {message_id}",
            body,
            "normal",
            message_id % 2,
            "2026-08-11T00:00:00+00:00",
            "[]",
        )


def _build_database(database: Path) -> dict[str, int]:
    connection = sqlite3.connect(database)
    try:
        connection.executescript(
            """
            PRAGMA page_size=4096;
            PRAGMA journal_mode=DELETE;
            PRAGMA synchronous=FULL;
            PRAGMA foreign_keys=ON;
            CREATE TABLE projects (
              id INTEGER PRIMARY KEY, slug TEXT NOT NULL, human_key TEXT NOT NULL,
              created_at TEXT NOT NULL
            );
            CREATE TABLE agents (
              id INTEGER PRIMARY KEY, project_id INTEGER NOT NULL,
              name TEXT NOT NULL, program TEXT NOT NULL, model TEXT NOT NULL,
              task_description TEXT NOT NULL, inception_ts TEXT NOT NULL,
              last_active_ts TEXT NOT NULL,
              FOREIGN KEY(project_id) REFERENCES projects(id)
            );
            CREATE TABLE messages (
              id INTEGER PRIMARY KEY, project_id INTEGER NOT NULL,
              sender_id INTEGER NOT NULL, thread_id TEXT, subject TEXT NOT NULL,
              body_md TEXT NOT NULL, importance TEXT NOT NULL,
              ack_required INTEGER NOT NULL, created_ts TEXT NOT NULL,
              attachments TEXT NOT NULL,
              FOREIGN KEY(project_id) REFERENCES projects(id),
              FOREIGN KEY(sender_id) REFERENCES agents(id)
            );
            CREATE TABLE message_recipients (
              message_id INTEGER NOT NULL, agent_id INTEGER NOT NULL,
              kind TEXT NOT NULL, read_ts TEXT, ack_ts TEXT,
              PRIMARY KEY(message_id, agent_id),
              FOREIGN KEY(message_id) REFERENCES messages(id),
              FOREIGN KEY(agent_id) REFERENCES agents(id)
            );
            CREATE TABLE file_reservations (
              id INTEGER PRIMARY KEY, project_id INTEGER NOT NULL,
              agent_id INTEGER NOT NULL, path_pattern TEXT NOT NULL,
              exclusive INTEGER NOT NULL, reason TEXT NOT NULL,
              created_ts TEXT NOT NULL, expires_ts TEXT NOT NULL, released_ts TEXT,
              FOREIGN KEY(project_id) REFERENCES projects(id),
              FOREIGN KEY(agent_id) REFERENCES agents(id)
            );
            """
        )
        connection.execute(
            "INSERT INTO projects VALUES (?, ?, ?, ?)",
            (1, "synthetic-project", "/synthetic/project", "2026-08-11T00:00:00+00:00"),
        )
        connection.executemany(
            "INSERT INTO agents VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                (
                    agent_id,
                    1,
                    f"SyntheticAgent{agent_id:04d}",
                    "synthetic",
                    "candidate-bound",
                    "production-shaped restore rehearsal",
                    "2026-08-11T00:00:00+00:00",
                    "2026-08-11T00:00:00+00:00",
                )
                for agent_id in range(1, AGENT_COUNT + 1)
            ),
        )
        connection.executemany(
            "INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            _message_rows(),
        )
        connection.executemany(
            "INSERT INTO message_recipients VALUES (?, ?, ?, ?, ?)",
            (
                (
                    message_id,
                    (message_id % AGENT_COUNT) + 1,
                    "to",
                    None,
                    None,
                )
                for message_id in range(1, RECIPIENT_COUNT + 1)
            ),
        )
        connection.executemany(
            "INSERT INTO file_reservations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                (
                    reservation_id,
                    1,
                    ((reservation_id - 1) % AGENT_COUNT) + 1,
                    f"synthetic/path/{reservation_id:05d}",
                    reservation_id % 2,
                    "synthetic rehearsal",
                    "2026-08-11T00:00:00+00:00",
                    "2026-08-12T00:00:00+00:00",
                    None,
                )
                for reservation_id in range(1, RESERVATION_COUNT + 1)
            ),
        )
        connection.commit()
        integrity = connection.execute("PRAGMA integrity_check").fetchone()
        foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
        if integrity != ("ok",) or foreign_keys:
            raise SeedBuildError("generated SQLite seed failed integrity checks")
        counts = {
            table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in (
                "projects",
                "agents",
                "messages",
                "message_recipients",
                "file_reservations",
            )
        }
    finally:
        connection.close()
    if database.stat().st_size < MINIMUM_DATABASE_BYTES:
        raise SeedBuildError("generated database did not reach the 50 MiB scale floor")
    return counts


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    content = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    with path.open("x", encoding="utf-8") as stream:
        os.chmod(path, 0o600)
        stream.write(content + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def _fsync_tree(root: Path) -> None:
    for path in sorted(root.rglob("*")):
        if path.is_file() and not path.is_symlink():
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    directories = [path for path in root.rglob("*") if path.is_dir()]
    for directory in [*sorted(directories, reverse=True), root]:
        descriptor = os.open(directory, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def build_rehearsal_seed(
    output_root: Path,
    production_source_database: Path,
    candidate_repository: Path,
    *,
    candidate_commit: str,
) -> dict[str, Any]:
    output_root = _canonical_absolute(output_root, label="output root")
    production_source_database = _canonical_absolute(
        production_source_database, label="production source database"
    )
    candidate_repository = _canonical_absolute(
        candidate_repository, label="candidate repository"
    )
    if output_root.exists() or output_root.is_symlink():
        raise SeedBuildError(f"output root must be absent: {output_root}")
    if not output_root.parent.is_dir() or output_root.parent.is_symlink():
        raise SeedBuildError("output parent must be a real existing directory")
    candidate = _candidate_identity(candidate_repository, candidate_commit)

    run_id = str(uuid.uuid4())
    started_at = datetime.now(timezone.utc).isoformat()
    staging = output_root.parent / f".{output_root.name}.seed-{run_id}"
    unconfirmed = output_root.parent / f".{output_root.name}.seed-{run_id}.unconfirmed"
    published = False
    if staging.exists() or staging.is_symlink():
        raise SeedBuildError("owned seed staging path unexpectedly exists")
    staging.mkdir(mode=0o700)
    try:
        legacy = staging / "legacy"
        archive = legacy / "archive"
        signals = legacy / "signals"
        archive.mkdir(parents=True, mode=0o700)
        signals.mkdir(mode=0o700)
        database = legacy / "storage.sqlite3"
        counts = _build_database(database)

        message_file = archive / "projects" / "synthetic-project" / "messages.md"
        message_file.parent.mkdir(parents=True, mode=0o700)
        message_file.write_text("candidate-bound synthetic archive\n", encoding="utf-8")
        _archive_git(archive, "init", "-q")
        _archive_git(archive, "config", "user.name", "Rehearsal Seed")
        _archive_git(archive, "config", "user.email", "seed@example.test")
        _archive_git(archive, "add", ".")
        _archive_git(archive, "commit", "-q", "-m", "synthetic seed")
        signal = signals / "projects" / "synthetic-project" / "agents" / "SyntheticAgent0002"
        signal.mkdir(parents=True, mode=0o700)
        (signal / "1.signal").write_text('{"message_id":1}\n', encoding="utf-8")

        final_database = output_root / "legacy" / "storage.sqlite3"
        final_archive = output_root / "legacy" / "archive"
        final_signals = output_root / "legacy" / "signals"
        final_provenance = output_root / "seed-provenance.json"
        final_receipt = output_root / "generator-receipt.json"
        provenance = {
            "schema_version": 1,
            "kind": "production-shaped-synthetic",
            "created_at": started_at,
            "seed_database": str(final_database),
            "production_source_database": str(production_source_database),
            "acquisition_method": "deterministic candidate-bound synthetic generator",
            "source_reference": (
                f"candidate:{candidate_commit};script:{SCRIPT_RELATIVE.as_posix()};"
                f"receipt:{final_receipt}"
            ),
        }
        provenance_path = staging / "seed-provenance.json"
        _write_json(provenance_path, provenance)
        receipt = {
            "schema_version": 1,
            "kind": "production-shaped-synthetic-seed-generation",
            "run_id": run_id,
            "started_at": started_at,
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "candidate_commit": candidate_commit,
            "candidate_checkout": candidate,
            "output_root": str(output_root),
            "production_source_database": str(production_source_database),
            "production_source_opened": False,
            "seed_database": str(final_database),
            "seed_database_size": database.stat().st_size,
            "seed_database_sha256": _sha256(database),
            "seed_database_family": {
                "main": {
                    "state": "PRESENT",
                    "size": database.stat().st_size,
                    "sha256": _sha256(database),
                },
                "wal": {"state": "ABSENT"},
                "shm": {"state": "ABSENT"},
            },
            "seed_archive": {
                "path": str(final_archive),
                "snapshot": migration.snapshot_tree(
                    archive,
                    required=True,
                    excluded_root_names=migration.ARCHIVE_EXCLUDED_ROOT_NAMES,
                ),
            },
            "seed_signals": {
                "path": str(final_signals),
                "snapshot": migration.snapshot_tree(signals, required=False),
            },
            "major_table_rows": counts,
            "seed_provenance": str(final_provenance),
            "seed_provenance_sha256": _sha256(provenance_path),
            "scale_floor": {
                "database_family_bytes": MINIMUM_DATABASE_BYTES,
                "agents": 700,
                "messages": 8_000,
                "message_recipients": 8_000,
            },
        }
        receipt_path = staging / "generator-receipt.json"
        _write_json(receipt_path, receipt)
        os.chmod(database, 0o600)
        _fsync_tree(staging)
        os.replace(staging, output_root)
        published = True
        try:
            parent_descriptor = os.open(output_root.parent, os.O_RDONLY)
            try:
                os.fsync(parent_descriptor)
            finally:
                os.close(parent_descriptor)
        except OSError:
            os.replace(output_root, unconfirmed)
            try:
                parent_descriptor = os.open(output_root.parent, os.O_RDONLY)
                try:
                    os.fsync(parent_descriptor)
                finally:
                    os.close(parent_descriptor)
            except OSError:
                pass
            raise
        receipt_sha256 = _sha256(final_receipt)
        return {
            "status": "generated",
            "run_id": run_id,
            "candidate_commit": candidate_commit,
            "output_root": str(output_root),
            "seed_database": str(final_database),
            "seed_database_size": receipt["seed_database_size"],
            "major_table_rows": counts,
            "generator_receipt": str(final_receipt),
            "generator_receipt_sha256": receipt_sha256,
        }
    except Exception:
        if staging.exists() and not staging.is_symlink():
            shutil.rmtree(staging)
        if published and output_root.exists() and not output_root.is_symlink():
            os.replace(output_root, unconfirmed)
            try:
                parent_descriptor = os.open(output_root.parent, os.O_RDONLY)
                try:
                    os.fsync(parent_descriptor)
                finally:
                    os.close(parent_descriptor)
            except OSError:
                pass
        raise


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", required=True)
    parser.add_argument("--production-source-db", required=True)
    parser.add_argument("--candidate-repo", required=True)
    parser.add_argument("--candidate-commit", required=True)
    return parser


def main() -> None:
    args = _parser().parse_args()
    try:
        result = build_rehearsal_seed(
            Path(args.output_root),
            Path(args.production_source_db),
            Path(args.candidate_repo),
            candidate_commit=args.candidate_commit,
        )
    except (SeedBuildError, OSError, sqlite3.Error, subprocess.SubprocessError) as exc:
        print(f"build-rehearsal-seed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
