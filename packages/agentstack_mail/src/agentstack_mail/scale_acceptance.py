"""Run a candidate-bound synthetic migration at the sealed H6 archive scale."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

from agentstack_mail import migration

SCRIPT_RELATIVE = Path(
    "packages/agentstack_mail/src/agentstack_mail/scale_acceptance.py"
)
MIGRATION_RELATIVE = Path(
    "packages/agentstack_mail/src/agentstack_mail/migration.py"
)
H6_SCALE: dict[str, int] = {
    "files": 52_850,
    "directories": 7_134,
    "nonempty_directories": 7_132,
    "empty_directories": 2,
    "bytes": 101_934_592,
    "unique_blobs": 26_714,
    "unique_trees": 6_799,
    "baseline_objects": 33_514,
    "max_file_depth": 8,
    "source_loose_objects": 74_295,
    "source_pack_files": 2,
}
H6_FORENSIC_EXACT: dict[str, int] = {
    "unique_blobs": 26_712,
    "unique_trees": 6_800,
    "baseline_objects": 33_513,
}


class ScaleAcceptanceError(RuntimeError):
    """The synthetic scale gate failed closed."""


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
    return environment


def _git(root: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-c", f"core.hooksPath={os.devnull}", "-C", str(root), *arguments],
        check=False,
        capture_output=True,
        text=True,
        env=_git_environment(),
        timeout=180,
    )
    if completed.returncode:
        raise ScaleAcceptanceError(
            f"git {' '.join(arguments)} failed: "
            f"{(completed.stderr or completed.stdout).strip()}"
        )
    return completed.stdout


def _candidate(repository: Path, commit: str) -> dict[str, Any]:
    head = _git(repository, "rev-parse", "--verify", "HEAD^{commit}").strip()
    if head != commit or _git(repository, "status", "--porcelain"):
        raise ScaleAcceptanceError("candidate checkout must be exact and clean")
    hashes: dict[str, str] = {}
    for relative, executing in (
        (SCRIPT_RELATIVE, Path(__file__).resolve()),
        (MIGRATION_RELATIVE, Path(migration.__file__).resolve()),
    ):
        blob = subprocess.run(
            ["git", "-C", str(repository), "show", f"{commit}:{relative}"],
            check=False,
            capture_output=True,
            env=_git_environment(),
            timeout=30,
        )
        data = executing.read_bytes()
        if blob.returncode or blob.stdout != data:
            raise ScaleAcceptanceError(f"executing {relative} is not candidate-bound")
        hashes[str(relative)] = hashlib.sha256(data).hexdigest()
    return {"head": head, "executing_file_sha256": hashes}


def _database(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            """
            PRAGMA foreign_keys=ON;
            CREATE TABLE projects (id INTEGER PRIMARY KEY, slug TEXT NOT NULL,
              human_key TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE agents (id INTEGER PRIMARY KEY, project_id INTEGER NOT NULL,
              name TEXT NOT NULL, program TEXT NOT NULL, model TEXT NOT NULL,
              task_description TEXT NOT NULL, inception_ts TEXT NOT NULL,
              last_active_ts TEXT NOT NULL);
            CREATE TABLE messages (id INTEGER PRIMARY KEY, project_id INTEGER NOT NULL,
              sender_id INTEGER NOT NULL, thread_id TEXT, subject TEXT NOT NULL,
              body_md TEXT NOT NULL, importance TEXT NOT NULL,
              ack_required INTEGER NOT NULL, created_ts TEXT NOT NULL,
              attachments TEXT NOT NULL);
            CREATE TABLE message_recipients (message_id INTEGER NOT NULL,
              agent_id INTEGER NOT NULL, kind TEXT NOT NULL, read_ts TEXT, ack_ts TEXT,
              PRIMARY KEY(message_id, agent_id));
            CREATE TABLE file_reservations (id INTEGER PRIMARY KEY,
              project_id INTEGER NOT NULL, agent_id INTEGER NOT NULL,
              path_pattern TEXT NOT NULL, exclusive INTEGER NOT NULL,
              reason TEXT NOT NULL, created_ts TEXT NOT NULL,
              expires_ts TEXT NOT NULL, released_ts TEXT);
            INSERT INTO projects VALUES (1,'scale','/synthetic/scale','2026-08-12');
            INSERT INTO agents VALUES (1,1,'ScaleAgent','synthetic','candidate','','2026-08-12','2026-08-12');
            """
        )
        connection.commit()
    finally:
        connection.close()


def _payload(index: int, size: int) -> bytes:
    prefix = f"orrery-scale-{index:08d}\n".encode()
    block = hashlib.sha256(prefix).digest()
    return prefix + (block * ((size - len(prefix) + 31) // 32))[: size - len(prefix)]


def _archive(root: Path) -> None:
    duplicate_directories = [root / f"duplicate-{index:04d}" for index in range(335)]
    for directory in duplicate_directories:
        directory.mkdir()
    unique_directories = [root / f"unique-{index:04d}" for index in range(6_790)]
    for directory in unique_directories:
        directory.mkdir()
    current = root
    for depth in range(7):
        current /= f"depth-{depth}"
        current.mkdir()
    unique_directories.append(current)
    (root / "empty-a").mkdir()
    (root / "empty-b").mkdir()
    base, remainder = divmod(H6_SCALE["bytes"], H6_SCALE["files"])
    for index in range(H6_SCALE["files"]):
        size = base
        if index < len(duplicate_directories):
            target = duplicate_directories[index] / "same.json"
            payload_index = 0
        else:
            target = unique_directories[
                (index - len(duplicate_directories)) % len(unique_directories)
            ] / f"f-{index:08d}.json"
            payload_index = 1 + (
                (index - len(duplicate_directories))
                % (H6_SCALE["unique_blobs"] - 2)
            )
        if index == H6_SCALE["files"] - 1:
            size += H6_SCALE["bytes"] - base * H6_SCALE["files"]
        target.write_bytes(_payload(payload_index, size))
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.name", "ORRERY Scale Acceptance")
    _git(root, "config", "user.email", "orrery-scale@localhost")
    _git(root, "add", "-f", "--all")
    _git(root, "commit", "-q", "-m", "synthetic H6 source")


def _tree_scale(root: Path) -> dict[str, int]:
    files = directories = bytes_count = empty = 0
    max_depth = 0
    for current, names, filenames in os.walk(root, followlinks=False):
        path = Path(current)
        if path == root:
            names[:] = [name for name in names if name != ".git"]
        directories += len(names)
        regular = []
        for name in filenames:
            if path == root and name == "server.pid":
                continue
            item = path / name
            info = item.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ScaleAcceptanceError(f"unsafe fixture entry: {item}")
            regular.append(name)
            files += 1
            bytes_count += int(info.st_size)
            max_depth = max(max_depth, len(item.relative_to(root).parts))
        for name in names:
            item = path / name
            if item.is_symlink():
                raise ScaleAcceptanceError(f"unsafe fixture entry: {item}")
        if path != root and not names and not regular:
            empty += 1
    return {
        "files": files,
        "directories": directories,
        "empty_directories": empty,
        "nonempty_directories": directories - empty,
        "bytes": bytes_count,
        "max_file_depth": max_depth,
    }


def _git_scale(root: Path) -> dict[str, int]:
    types: dict[str, int] = {}
    output = _git(
        root,
        "cat-file",
        "--batch-check=%(objecttype)",
        "--batch-all-objects",
    )
    for object_type in output.splitlines():
        types[object_type] = types.get(object_type, 0) + 1
    pack_files = len(list((root / ".git" / "objects" / "pack").glob("*.pack")))
    return {
        "unique_blobs": types.get("blob", 0),
        "unique_trees": types.get("tree", 0),
        "baseline_objects": sum(types.values()),
        "pack_files": pack_files,
    }


def _write_once(path: Path, payload: dict[str, Any]) -> str:
    raw = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    os.chmod(path, 0o400)
    return hashlib.sha256(raw).hexdigest()


def run(output: Path, repository: Path, commit: str) -> dict[str, Any]:
    if output.exists() or output.is_symlink():
        raise ScaleAcceptanceError(f"output must be absent: {output}")
    identity = _candidate(repository, commit)
    output.mkdir(mode=0o700)
    source_root = output / "legacy"
    destination = output / "mail"
    source_root.mkdir(mode=0o700)
    _database(source_root / "storage.sqlite3")
    (source_root / "signals").mkdir()
    archive = source_root / "archive"
    archive.mkdir()
    _archive(archive)
    source_tree = _tree_scale(archive)
    source_git = _git_scale(archive)
    expected_tree = {
        key: H6_SCALE[key]
        for key in (
            "files",
            "directories",
            "empty_directories",
            "nonempty_directories",
            "bytes",
            "max_file_depth",
        )
    }
    expected_git = {
        key: H6_SCALE[key]
        for key in ("unique_blobs", "unique_trees", "baseline_objects")
    }
    if source_tree != expected_tree or any(
        source_git[key] != expected_git[key] for key in expected_git
    ):
        raise ScaleAcceptanceError(
            f"fixture mismatch: tree={source_tree}, git={source_git}"
        )
    hostile = {
        "GIT_CONFIG_COUNT": "3",
        "GIT_CONFIG_KEY_0": "gc.auto",
        "GIT_CONFIG_VALUE_0": "1",
        "GIT_CONFIG_KEY_1": "gc.autoDetach",
        "GIT_CONFIG_VALUE_1": "true",
        "GIT_CONFIG_KEY_2": "maintenance.auto",
        "GIT_CONFIG_VALUE_2": "true",
    }
    before_environment = {name: os.environ.get(name) for name in hostile}
    os.environ.update(hostile)
    try:
        copied = migration.copy_state(migration.StatePaths.from_root(source_root), destination)
        verified = migration.verify_copy(
            migration.StatePaths.from_root(source_root), destination
        )
    finally:
        for name, value in before_environment.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
    destination_tree = _tree_scale(destination / "archive")
    destination_git = _git_scale(destination / "archive")
    gc_artifacts = [
        str(path.relative_to(destination))
        for name in ("gc.pid", "gc.log")
        for path in destination.rglob(name)
    ]
    if (
        copied.status != "copied"
        or verified.get("status") != "verified"
        or destination_tree != expected_tree
        or any(destination_git[key] != expected_git[key] for key in expected_git)
        or destination_git["pack_files"] != 0
        or gc_artifacts
    ):
        raise ScaleAcceptanceError("candidate full-scale copy/verify assertions failed")
    receipt = {
        "schema_version": 1,
        "kind": "orrery-migration-scale-acceptance",
        "status": "passed",
        "run_id": str(uuid.uuid4()),
        "candidate_commit": commit,
        "candidate": identity,
        "production_h6_observation": H6_SCALE,
        "production_h6_forensic_exact": H6_FORENSIC_EXACT,
        "fixture": {
            "origin": "synthetic-no-production-bytes",
            "tree": source_tree,
            "fresh_baseline_git": source_git,
            "ratios": {key: source_tree[key] / expected_tree[key] for key in expected_tree},
            "non_equivalent": {
                "names_and_content_hashes": "synthetic",
                "database_rows_and_size": "covered by restore acceptance",
                "source_git_history": {
                    "production_loose_objects": H6_SCALE["source_loose_objects"],
                    "production_pack_files": H6_SCALE["source_pack_files"],
                    "fixture_history": "one synthetic root commit",
                },
                "fresh_baseline_object_split": {
                    "forensic": H6_FORENSIC_EXACT,
                    "fixture": expected_git,
                    "total_ratio": (
                        expected_git["baseline_objects"]
                        / H6_FORENSIC_EXACT["baseline_objects"]
                    ),
                    "reason": (
                        "synthetic flat-content generator preserves exact total loose "
                        "object pressure with a one-object blob/tree redistribution"
                    ),
                },
            },
        },
        "hostile_git_input": hostile,
        "candidate_effective_git_policy": {
            "gc.auto": "0",
            "gc.autoDetach": "false",
            "maintenance.auto": "false",
        },
        "result": {
            "copy": copied.status,
            "verify": verified["status"],
            "state_sha256": copied.state_sha256,
            "destination_tree": destination_tree,
            "destination_git": destination_git,
            "gc_artifacts": gc_artifacts,
        },
    }
    receipt_path = output / "final.json"
    receipt_sha = _write_once(receipt_path, receipt)
    _write_once(output / "final.sha256.json", {"sha256": receipt_sha, "file": "final.json"})
    return {"status": "passed", "receipt": str(receipt_path), "sha256": receipt_sha}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    parser.add_argument("--candidate-repo", required=True)
    parser.add_argument("--candidate-commit", required=True)
    args = parser.parse_args()
    try:
        result = run(Path(args.output), Path(args.candidate_repo), args.candidate_commit)
    except (OSError, sqlite3.Error, subprocess.SubprocessError, migration.MigrationError, ScaleAcceptanceError) as exc:
        print(f"migration-scale-acceptance: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
