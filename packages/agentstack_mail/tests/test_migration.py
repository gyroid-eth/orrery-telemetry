from __future__ import annotations

import errno
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from agentstack_mail import migration
from agentstack_mail.migration import (
    MANIFEST_NAME,
    MIGRATION_FAULT_PHASES,
    POST_PUBLICATION_FAULT_PHASES,
    PRE_PUBLICATION_FAULT_PHASES,
    MigrationError,
    StatePaths,
    VerificationError,
    assess_rollback,
    copy_state,
    main,
    snapshot_state,
    verify_copy,
)


def _create_database(path: Path, *, wal: bool = False) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    if wal:
        connection.execute("PRAGMA journal_mode=WAL")
    connection.executescript(
        """
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
        INSERT INTO projects VALUES (1, 'project', '/tmp/project', '2026-08-10T00:00:00');
        INSERT INTO agents VALUES
          (10, 1, 'ProOpus', 'claude-code', 'opus', '', '2026-08-10T00:00:00', '2026-08-10T00:00:00'),
          (11, 1, 'PluckyEinstein', 'codex', 'sol', '', '2026-08-10T00:00:00', '2026-08-10T00:00:00');
        INSERT INTO messages VALUES
          (20, 1, 10, 'thread-7', 'subject', 'body', 'high', 1,
           '2026-08-10T00:01:00', '[]');
        INSERT INTO message_recipients VALUES
          (20, 11, 'to', '2026-08-10T00:02:00', '2026-08-10T00:03:00');
        INSERT INTO file_reservations VALUES
          (30, 1, 11, 'src/**', 1, 'migration', '2026-08-10T00:04:00',
           '2026-08-10T01:04:00', NULL);
        """
    )
    connection.commit()
    return connection


def _source(tmp_path: Path, *, wal: bool = False) -> tuple[StatePaths, sqlite3.Connection]:
    root = tmp_path / "legacy"
    root.mkdir()
    connection = _create_database(root / "storage.sqlite3", wal=wal)
    archive = root / "archive"
    (archive / "projects" / "project" / "messages" / "threads").mkdir(parents=True)
    (archive / "projects" / "project" / "messages" / "threads" / "thread-7.md").write_text(
        "thread-7 / message 20\n", encoding="utf-8"
    )
    subprocess.run(["git", "init", "-q", str(archive)], check=True)
    subprocess.run(
        ["git", "-C", str(archive), "config", "user.name", "Migration Test"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(archive), "config", "user.email", "migration@example.test"],
        check=True,
    )
    subprocess.run(["git", "-C", str(archive), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(archive), "commit", "-q", "-m", "fixture"],
        check=True,
    )
    signals = root / "signals" / "projects" / "project" / "agents" / "PluckyEinstein"
    signals.mkdir(parents=True)
    (signals / "20.signal").write_text('{"message_id":20}\n', encoding="utf-8")
    return StatePaths.from_root(root), connection


def _filesystem_state(root: Path) -> dict[str, tuple[int, int, int]]:
    return {
        path.relative_to(root).as_posix(): (
            path.stat().st_ino,
            path.stat().st_mtime_ns,
            path.stat().st_size,
        )
        for path in [root, *sorted(root.rglob("*"))]
    }


def test_copy_then_identical_rerun_is_true_noop(tmp_path: Path) -> None:
    source, connection = _source(tmp_path)
    destination = tmp_path / "new"
    try:
        copied = copy_state(source, destination)
        before = _filesystem_state(destination)
        manifest_before = (destination / MANIFEST_NAME).read_bytes()

        noop = copy_state(source, destination)

        assert copied.status == "copied"
        assert noop.status == "noop"
        assert noop.operation_id is None
        assert _filesystem_state(destination) == before
        assert (destination / MANIFEST_NAME).read_bytes() == manifest_before
        assert verify_copy(source, destination)["status"] == "verified"
    finally:
        connection.close()


def test_copy_keeps_all_six_state_snapshots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source, connection = _source(tmp_path)
    destination = tmp_path / "new"
    original = migration.snapshot_state
    calls: list[StatePaths] = []

    def recording_snapshot(paths: StatePaths, **kwargs: object) -> dict[str, object]:
        calls.append(paths.resolved())
        return original(paths, **kwargs)

    monkeypatch.setattr(migration, "snapshot_state", recording_snapshot)
    try:
        copy_state(source, destination)
        source_calls = [paths for paths in calls if paths.database == source.database]
        assert len(calls) == 6
        assert len(source_calls) == 4
    finally:
        connection.close()


def test_copy_replaces_legacy_history_with_one_exact_baseline_commit(
    tmp_path: Path,
) -> None:
    source, connection = _source(tmp_path)
    destination = tmp_path / "new"
    legacy_extra = source.archive / "legacy-extra.md"
    legacy_extra.write_text("second legacy commit\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(source.archive), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(source.archive), "commit", "-q", "-m", "legacy second"],
        check=True,
    )
    legacy_head = subprocess.run(
        ["git", "-C", str(source.archive), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    (source.archive / "server.pid").write_text("123\n", encoding="utf-8")
    try:
        copy_state(source, destination)
        archive = destination / "archive"
        new_head = subprocess.run(
            ["git", "-C", str(archive), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        commit_count = subprocess.run(
            ["git", "-C", str(archive), "rev-list", "--all", "--count"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        roots = subprocess.run(
            ["git", "-C", str(archive), "rev-list", "--all", "--max-parents=0", "--count"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        status = subprocess.run(
            ["git", "-C", str(archive), "status", "--porcelain=v1", "--untracked-files=all"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        manifest = json.loads((destination / MANIFEST_NAME).read_text(encoding="utf-8"))
        git_baseline = manifest["destination_git"]["baseline"]

        assert new_head != legacy_head
        assert commit_count == "1"
        assert roots == "1"
        assert status == ""
        assert not (archive / "server.pid").exists()
        assert legacy_extra.read_bytes() == (archive / "legacy-extra.md").read_bytes()
        assert manifest["archive_policy"] == {
            "copied": "working_tree",
            "excluded_root_names": [".git", "server.pid"],
            "legacy_git_history": "not_copied",
            "new_git_history": "single_root_baseline_commit",
        }
        assert manifest["database_policy"] == {
            "copied": "sqlite_logical_backup_including_committed_wal",
            "compared": "main_database_schema_rows_relations_and_pragmas",
            "sqlite_runtime_sidecars": (
                "excluded_ro_may_create_rw_guard_may_checkpoint_or_remove"
            ),
        }
        assert git_baseline["commit_count"] == 1
        assert git_baseline["root_count"] == 1
        assert git_baseline["branch"] == "main"
        assert git_baseline["author_name"] == "AgentStack Mail Migration"
        assert git_baseline["author_email"] == "agentstack-mail-migration@localhost"
        assert git_baseline["author_date"] == manifest["created_at"]
        assert git_baseline["committer_date"] == manifest["created_at"]
        assert git_baseline["subject"] == "AgentStack Mail migration baseline"
        assert (
            f"Authority-Data-SHA256: {manifest['baseline']['state_sha256']}"
            in git_baseline["message"]
        )
        assert verify_copy(source, destination)["status"] == "verified"
    finally:
        connection.close()


def test_destination_git_history_or_tree_tampering_is_rejected(
    tmp_path: Path,
) -> None:
    source, connection = _source(tmp_path)
    destination = tmp_path / "new"
    try:
        copy_state(source, destination)
        archive = destination / "archive"
        (archive / "post-baseline.md").write_text("tamper\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(archive), "add", "."], check=True)
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=Tamper",
                "-c",
                "user.email=tamper@example.test",
                "-C",
                str(archive),
                "commit",
                "-q",
                "-m",
                "tamper",
            ],
            check=True,
        )

        with pytest.raises(VerificationError, match="exactly one root commit"):
            verify_copy(source, destination)
        rollback = assess_rollback(
            destination / MANIFEST_NAME,
            "C5_CLIENT_SWITCHING",
        )
        assert rollback["status"] == "no_go"
        assert rollback["destination_matches_baseline"] is False
        assert "exactly one root commit" in rollback["destination_verification_error"]
    finally:
        connection.close()


def test_unreachable_destination_git_object_is_rejected(tmp_path: Path) -> None:
    source, connection = _source(tmp_path)
    destination = tmp_path / "new"
    try:
        copy_state(source, destination)
        archive = destination / "archive"
        subprocess.run(
            ["git", "-C", str(archive), "hash-object", "-w", "--stdin"],
            input="unreachable legacy residue\n",
            text=True,
            check=True,
            capture_output=True,
        )

        with pytest.raises(VerificationError, match="exactly its reachable set"):
            verify_copy(source, destination)
        rollback = assess_rollback(
            destination / MANIFEST_NAME,
            "C4_NEW_SERVICE_READY",
        )
        assert rollback["status"] == "no_go"
        assert rollback["destination_matches_baseline"] is False
        assert "reachable set" in rollback["destination_verification_error"]
    finally:
        connection.close()


def test_exact_same_source_and_destination_is_noop(tmp_path: Path) -> None:
    source, connection = _source(tmp_path)
    root = source.database.parent
    before = _filesystem_state(root)
    try:
        result = copy_state(source, root)
        assert result.status == "noop"
        assert _filesystem_state(root) == before
        assert not list(tmp_path.glob(".legacy.migration-*"))
        assert not (root / MANIFEST_NAME).exists()
    finally:
        connection.close()


def test_sqlite_backup_includes_committed_wal_content(tmp_path: Path) -> None:
    source, writer = _source(tmp_path, wal=True)
    writer.execute(
        "INSERT INTO messages VALUES (21, 1, 10, 'thread-7', 'wal', 'committed', "
        "'normal', 0, '2026-08-10T00:05:00', '[]')"
    )
    writer.execute("INSERT INTO message_recipients VALUES (21, 11, 'to', NULL, NULL)")
    writer.commit()
    try:
        destination = tmp_path / "new"
        copy_state(source, destination)
        copied = sqlite3.connect(destination / "storage.sqlite3")
        try:
            assert copied.execute("SELECT subject FROM messages WHERE id=21").fetchone() == (
                "wal",
            )
        finally:
            copied.close()
    finally:
        writer.close()


def test_copy_preserves_logical_rows_from_a_crashed_committed_wal(
    tmp_path: Path,
) -> None:
    source, writer = _source(tmp_path, wal=True)
    writer.close()
    subprocess.run(
        [
            sys.executable,
            "-c",
            """
import os
import sqlite3
import sys

connection = sqlite3.connect(sys.argv[1])
connection.execute("PRAGMA wal_autocheckpoint=0")
connection.execute(
    "INSERT INTO messages VALUES (21, 1, 10, 'thread-7', 'wal', "
    "'committed-before-crash', 'normal', 0, '2026-08-10T00:05:00', '[]')"
)
connection.execute(
    "INSERT INTO message_recipients VALUES (21, 11, 'to', NULL, NULL)"
)
connection.commit()
os._exit(0)
""",
            str(source.database),
        ],
        check=True,
    )
    assert source.database.with_name(f"{source.database.name}-wal").exists()
    assert source.database.with_name(f"{source.database.name}-shm").exists()

    destination = tmp_path / "new"
    copy_state(source, destination)

    source_state = snapshot_state(source)
    destination_state = snapshot_state(StatePaths.from_root(destination))
    assert source_state["database"]["tables"]["messages"]["count"] == 2
    assert destination_state["database"]["tables"]["messages"]["count"] == 2
    assert source_state["state_sha256"] == destination_state["state_sha256"]


def test_relational_change_with_equal_counts_is_detected(tmp_path: Path) -> None:
    source, connection = _source(tmp_path)
    destination = tmp_path / "new"
    try:
        copy_state(source, destination)
        changed = sqlite3.connect(destination / "storage.sqlite3")
        changed.execute("UPDATE message_recipients SET agent_id=10 WHERE message_id=20")
        changed.commit()
        changed.close()

        with pytest.raises(VerificationError, match="does not match"):
            verify_copy(source, destination)
    finally:
        connection.close()


def test_truncated_database_fails_without_destination(tmp_path: Path) -> None:
    source, connection = _source(tmp_path)
    connection.close()
    source.database.write_bytes(source.database.read_bytes()[:128])
    destination = tmp_path / "new"

    with pytest.raises(VerificationError):
        copy_state(source, destination)

    assert not destination.exists()
    assert not list(tmp_path.glob(".new.migration-*"))


@pytest.mark.parametrize(
    ("failure", "phase"),
    (
        (PermissionError(errno.EACCES, "denied"), "archive_copy:before_file"),
        (OSError(errno.ENOSPC, "full"), "archive_copy:copy_chunk"),
    ),
)
def test_injected_write_failures_leave_no_partial_destination(
    tmp_path: Path,
    failure: OSError,
    phase: str,
) -> None:
    source, connection = _source(tmp_path)
    source_before = snapshot_state(source)
    destination = tmp_path / "new"

    def fault(current: str) -> None:
        if current == phase:
            raise failure

    try:
        with pytest.raises(OSError) as raised:
            copy_state(source, destination, fault_hook=fault)
        assert raised.value.errno == failure.errno
        assert snapshot_state(source) == source_before
        assert not destination.exists()
        assert not list(tmp_path.glob(".new.migration-*"))
    finally:
        connection.close()


@pytest.mark.parametrize("phase", PRE_PUBLICATION_FAULT_PHASES)
def test_every_enumerated_pre_publication_seam_fails_without_canonical_state(
    tmp_path: Path,
    phase: str,
) -> None:
    source, connection = _source(tmp_path)
    source_before = snapshot_state(source)
    destination = tmp_path / "new"
    observed: list[str] = []

    def fault(current: str) -> None:
        observed.append(current)
        if current == phase:
            raise OSError(errno.EIO, f"interrupted at {phase}")

    try:
        with pytest.raises(OSError, match="interrupted at"):
            copy_state(source, destination, fault_hook=fault)
        assert phase in observed
        assert snapshot_state(source) == source_before
        assert not destination.exists()
        assert not list(tmp_path.glob(".new.migration-*"))
    finally:
        connection.close()


def test_fault_seam_partition_is_complete_and_unique() -> None:
    assert MIGRATION_FAULT_PHASES == (
        PRE_PUBLICATION_FAULT_PHASES + POST_PUBLICATION_FAULT_PHASES
    )
    assert len(MIGRATION_FAULT_PHASES) == len(set(MIGRATION_FAULT_PHASES))


def test_existing_different_destination_is_never_overwritten(tmp_path: Path) -> None:
    source, connection = _source(tmp_path)
    destination = tmp_path / "new"
    try:
        copy_state(source, destination)
        marker = destination / "archive" / "extra"
        marker.write_text("foreign", encoding="utf-8")
        before = _filesystem_state(destination)

        with pytest.raises(MigrationError, match="different state"):
            copy_state(source, destination)

        assert _filesystem_state(destination) == before
        assert marker.read_text(encoding="utf-8") == "foreign"
    finally:
        connection.close()


def test_retry_removes_only_marker_owned_abandoned_staging(tmp_path: Path) -> None:
    source, connection = _source(tmp_path)
    operation_id = "8a5f32be-65d8-4bdf-918e-dc35b9ce6e8d"
    owned = tmp_path / f".new.migration-{operation_id}"
    owned.mkdir()
    (owned / ".agentstack-mail-migration-staging.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "operation_id": operation_id,
                "kind": "owned-staging",
            }
        ),
        encoding="utf-8",
    )
    unknown = tmp_path / ".new.migration-unknown"
    unknown.mkdir()
    (unknown / "keep").write_text("not ours", encoding="utf-8")
    try:
        result = copy_state(source, tmp_path / "new")
        assert result.status == "copied"
        assert not owned.exists()
        assert (unknown / "keep").read_text(encoding="utf-8") == "not ours"
    finally:
        connection.close()


def test_retry_does_not_trust_a_symlinked_staging_marker(tmp_path: Path) -> None:
    source, connection = _source(tmp_path)
    operation_id = "8a5f32be-65d8-4bdf-918e-dc35b9ce6e8d"
    candidate = tmp_path / f".new.migration-{operation_id}"
    candidate.mkdir()
    sentinel = candidate / "keep"
    sentinel.write_text("not owned", encoding="utf-8")
    external = tmp_path / "external-marker.json"
    external.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "operation_id": operation_id,
                "kind": "owned-staging",
            }
        ),
        encoding="utf-8",
    )
    (candidate / migration.STAGING_MARKER).symlink_to(external)
    try:
        assert copy_state(source, tmp_path / "new").status == "copied"
        assert sentinel.read_text(encoding="utf-8") == "not owned"
    finally:
        connection.close()


def test_source_mutation_during_copy_is_blocked_before_publish(tmp_path: Path) -> None:
    source, connection = _source(tmp_path)
    destination = tmp_path / "new"

    def fault(phase: str) -> None:
        if phase == "before_verification":
            connection.execute("UPDATE messages SET body_md='changed' WHERE id=20")
            connection.commit()

    try:
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            copy_state(source, destination, fault_hook=fault)
        assert not destination.exists()
    finally:
        connection.close()


def test_source_mutation_after_fsync_is_blocked_before_publish(tmp_path: Path) -> None:
    source, connection = _source(tmp_path)
    destination = tmp_path / "new"

    def fault(phase: str) -> None:
        if phase == "after_fsync":
            connection.execute("UPDATE messages SET body_md='late-change' WHERE id=20")
            connection.commit()

    try:
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            copy_state(source, destination, fault_hook=fault)
        assert not destination.exists()
    finally:
        connection.close()


def test_source_mutation_at_final_pre_publish_seam_is_blocked(tmp_path: Path) -> None:
    source, connection = _source(tmp_path)
    destination = tmp_path / "new"

    def fault(phase: str) -> None:
        if phase == "before_publish":
            connection.execute("UPDATE messages SET body_md='last-seam' WHERE id=20")
            connection.commit()

    try:
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            copy_state(source, destination, fault_hook=fault)
        assert not destination.exists()
    finally:
        connection.close()


def test_atomic_publish_never_replaces_a_concurrently_created_destination(
    tmp_path: Path,
) -> None:
    source, connection = _source(tmp_path)
    destination = tmp_path / "new"

    def fault(phase: str) -> None:
        if phase == "before_publish":
            destination.mkdir()
            (destination / "foreign").write_text("keep", encoding="utf-8")

    try:
        with pytest.raises(MigrationError, match="destination appeared"):
            copy_state(source, destination, fault_hook=fault)
        assert (destination / "foreign").read_text(encoding="utf-8") == "keep"
        assert not list(tmp_path.glob(".new.migration-*"))
    finally:
        connection.close()


def test_source_mutation_at_post_publish_seam_is_blocked_and_unconfirmed(
    tmp_path: Path,
) -> None:
    source, connection = _source(tmp_path)
    destination = tmp_path / "new"

    def fault(phase: str) -> None:
        if phase == "after_publish":
            connection.execute("UPDATE messages SET body_md='post-publish' WHERE id=20")
            connection.commit()

    try:
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            copy_state(source, destination, fault_hook=fault)
        assert destination.is_dir()
        assert (destination / ".agentstack-mail-migration-staging.json").is_file()
        assert verify_copy(source, destination)["status"] == "verified"
        assert (destination / ".agentstack-mail-migration-staging.json").is_file()
    finally:
        connection.close()


def test_manifest_corruption_at_post_publish_seam_blocks_normal_confirmation(
    tmp_path: Path,
) -> None:
    source, connection = _source(tmp_path)
    destination = tmp_path / "new"

    def fault(phase: str) -> None:
        if phase == "after_publish":
            manifest_path = destination / MANIFEST_NAME
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["baseline"]["database"]["tables"]["messages"]["count"] = 999
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    try:
        with pytest.raises(MigrationError, match="internally inconsistent"):
            copy_state(source, destination, fault_hook=fault)
        assert (destination / ".agentstack-mail-migration-staging.json").is_file()
    finally:
        connection.close()


def test_retry_finalizes_complete_generation_after_post_publish_interruption(
    tmp_path: Path,
) -> None:
    source, connection = _source(tmp_path)
    destination = tmp_path / "new"
    assert POST_PUBLICATION_FAULT_PHASES == ("after_publish",)

    def fault(phase: str) -> None:
        if phase == "after_publish":
            raise OSError(errno.EIO, "interrupted after atomic rename")

    try:
        with pytest.raises(OSError, match="interrupted after atomic rename"):
            copy_state(source, destination, fault_hook=fault)
        assert destination.is_dir()
        assert (destination / ".agentstack-mail-migration-staging.json").is_file()
        assert verify_copy(source, destination)["status"] == "verified"
        assert (destination / ".agentstack-mail-migration-staging.json").is_file()

        recovered = copy_state(source, destination)

        assert recovered.status == "recovered"
        assert recovered.operation_id is not None
        assert not (destination / ".agentstack-mail-migration-staging.json").exists()
        assert copy_state(source, destination).status == "noop"
    finally:
        connection.close()


def test_normal_and_recovery_paths_share_one_confirmation_function(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recovery_base = tmp_path / "recovery"
    normal_base = tmp_path / "normal"
    recovery_base.mkdir()
    normal_base.mkdir()
    recovery_source, recovery_connection = _source(recovery_base)
    recovery_destination = tmp_path / "recovery-new"

    def interrupt_after_publish(phase: str) -> None:
        if phase == "after_publish":
            raise OSError(errno.EIO, "leave recovery generation")

    try:
        with pytest.raises(OSError):
            copy_state(
                recovery_source,
                recovery_destination,
                fault_hook=interrupt_after_publish,
            )
        normal_source, normal_connection = _source(normal_base)
        normal_destination = tmp_path / "normal-new"
        calls: list[Path] = []

        def broken_common_confirmation(
            destination_root: Path,
            _source_paths: StatePaths,
            *,
            _source_database_connection: sqlite3.Connection | None = None,
        ) -> tuple[str, str] | None:
            assert _source_database_connection is not None
            calls.append(destination_root)
            raise VerificationError("mutated common confirmation")

        monkeypatch.setattr(
            migration,
            "_finalize_published_generation",
            broken_common_confirmation,
        )
        try:
            with pytest.raises(VerificationError, match="mutated common confirmation"):
                copy_state(normal_source, normal_destination)
            with pytest.raises(VerificationError, match="mutated common confirmation"):
                copy_state(recovery_source, recovery_destination)
        finally:
            normal_connection.close()

        assert calls == [normal_destination.resolve(), recovery_destination.resolve()]
    finally:
        recovery_connection.close()


def test_recovery_refuses_tampered_published_baseline(tmp_path: Path) -> None:
    source, connection = _source(tmp_path)
    destination = tmp_path / "new"

    def fault(phase: str) -> None:
        if phase == "after_publish":
            raise OSError(errno.EIO, "interrupted after atomic rename")

    try:
        with pytest.raises(OSError):
            copy_state(source, destination, fault_hook=fault)
        manifest_path = destination / MANIFEST_NAME
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["baseline"]["database"]["tables"]["messages"]["count"] = 999
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        with pytest.raises(MigrationError, match="internally inconsistent"):
            copy_state(source, destination)

        assert (destination / ".agentstack-mail-migration-staging.json").exists()
    finally:
        connection.close()


def test_archive_must_be_a_valid_git_worktree(tmp_path: Path) -> None:
    source, connection = _source(tmp_path)
    (source.archive / ".git").rename(source.archive / ".not-git")
    try:
        with pytest.raises(VerificationError, match="not a normal Git worktree"):
            copy_state(source, tmp_path / "new")
    finally:
        connection.close()


def test_writer_lock_at_any_archive_depth_is_rejected(tmp_path: Path) -> None:
    source, connection = _source(tmp_path)
    lock = source.archive / "projects" / "project" / ".archive.lock"
    lock.write_text("writer", encoding="utf-8")
    try:
        with pytest.raises(VerificationError, match="writer lock"):
            copy_state(source, tmp_path / "new")
    finally:
        connection.close()


@pytest.mark.parametrize(
    ("relative", "message"),
    (
        (Path("projects/project/write.lock.owner.json"), "writer lock"),
        (Path(".git/index.lock"), "Git writer lock"),
    ),
)
def test_all_lock_artifact_forms_are_rejected(
    tmp_path: Path,
    relative: Path,
    message: str,
) -> None:
    source, connection = _source(tmp_path)
    lock = source.archive / relative
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text("writer", encoding="utf-8")
    try:
        with pytest.raises(VerificationError, match=message):
            copy_state(source, tmp_path / "new")
    finally:
        connection.close()


@pytest.mark.parametrize(
    ("kind", "message"),
    (
        ("symlink", "symbolic links"),
        ("hardlink", "hard-linked"),
        ("fifo", "special filesystem entry"),
        ("nested_git", "nested Git repositories"),
    ),
)
def test_archive_rejects_non_regular_or_nested_repository_entries(
    tmp_path: Path,
    kind: str,
    message: str,
) -> None:
    source, connection = _source(tmp_path)
    target = source.archive / "projects" / "project" / f"unsafe-{kind}"
    if kind == "symlink":
        target.symlink_to(source.archive / "projects")
    elif kind == "hardlink":
        os.link(
            source.archive
            / "projects"
            / "project"
            / "messages"
            / "threads"
            / "thread-7.md",
            target,
        )
    elif kind == "fifo":
        os.mkfifo(target)
    else:
        (target / ".git").mkdir(parents=True)
    try:
        with pytest.raises(VerificationError, match=message):
            copy_state(source, tmp_path / "new")
    finally:
        connection.close()


def test_excluded_server_pid_must_be_a_regular_single_link_file(
    tmp_path: Path,
) -> None:
    source, connection = _source(tmp_path)
    (source.archive / "server.pid").symlink_to(source.archive / "projects")
    try:
        with pytest.raises(VerificationError, match="excluded runtime files"):
            copy_state(source, tmp_path / "new")
    finally:
        connection.close()


@pytest.mark.parametrize("kind", ("symlink", "hardlink"))
def test_source_database_aliases_are_rejected(tmp_path: Path, kind: str) -> None:
    source, connection = _source(tmp_path)
    if kind == "symlink":
        real_database = source.database.with_name("real.sqlite3")
        source.database.rename(real_database)
        source.database.symlink_to(real_database)
        match = "symbolic path components"
    else:
        os.link(source.database, source.database.with_name("database-hardlink"))
        match = "hard-linked databases"
    try:
        with pytest.raises(VerificationError, match=match):
            copy_state(source, tmp_path / "new")
        assert not (tmp_path / "new").exists()
    finally:
        connection.close()


def test_destination_database_hardlink_is_rejected_by_verify_and_rollback(
    tmp_path: Path,
) -> None:
    source, connection = _source(tmp_path)
    destination = tmp_path / "new"
    try:
        copy_state(source, destination)
        destination_database = destination / "storage.sqlite3"
        external = tmp_path / "external.sqlite3"
        external.write_bytes(destination_database.read_bytes())
        destination_database.unlink()
        os.link(external, destination_database)

        with pytest.raises(VerificationError, match="hard-linked databases"):
            verify_copy(source, destination)
        rollback = assess_rollback(
            destination / MANIFEST_NAME,
            "C4_NEW_SERVICE_READY",
        )
        assert rollback["status"] == "no_go"
        assert rollback["destination_matches_baseline"] is False
        assert "hard-linked databases" in rollback["destination_verification_error"]
    finally:
        connection.close()


def test_active_source_database_writer_is_rejected(tmp_path: Path) -> None:
    source, writer = _source(tmp_path, wal=True)
    writer.execute("BEGIN IMMEDIATE")
    writer.execute("UPDATE messages SET body_md='uncommitted' WHERE id=20")
    try:
        with pytest.raises(VerificationError, match="active writer"):
            copy_state(source, tmp_path / "new")
        assert not (tmp_path / "new").exists()
    finally:
        writer.rollback()
        writer.close()


def test_generic_snapshot_does_not_take_the_copy_writer_fence(tmp_path: Path) -> None:
    source, writer = _source(tmp_path, wal=True)
    writer.execute("BEGIN IMMEDIATE")
    writer.execute("UPDATE messages SET body_md='uncommitted' WHERE id=20")
    try:
        snapshot = snapshot_state(source)
        assert snapshot["database"]["tables"]["messages"]["count"] == 1
    finally:
        writer.rollback()
        writer.close()


def test_generic_snapshot_uses_one_read_transaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, writer = _source(tmp_path, wal=True)
    baseline = snapshot_state(source)
    writer.execute("BEGIN IMMEDIATE")
    writer.execute("UPDATE agents SET model='changed-during-snapshot' WHERE id=10")
    original_rows_digest = migration._rows_digest
    committed = False

    def commit_after_agents_digest(
        connection: sqlite3.Connection, query: str
    ) -> dict[str, object]:
        nonlocal committed
        result = original_rows_digest(connection, query)
        if 'FROM "agents"' in query and not committed:
            writer.commit()
            committed = True
        return result

    monkeypatch.setattr(migration, "_rows_digest", commit_after_agents_digest)
    try:
        during = snapshot_state(source)
        assert committed is True
        assert during["database"]["logical_sha256"] == baseline["database"][
            "logical_sha256"
        ]
        after = snapshot_state(source)
        assert after["database"]["logical_sha256"] != baseline["database"][
            "logical_sha256"
        ]
    finally:
        if writer.in_transaction:
            writer.rollback()
        writer.close()


def test_source_root_and_destination_parent_symlinks_are_rejected(
    tmp_path: Path,
) -> None:
    source_base = tmp_path / "source-case"
    source_base.mkdir()
    source, connection = _source(source_base)
    real_archive = source.archive.with_name("real-archive")
    source.archive.rename(real_archive)
    source.archive.symlink_to(real_archive, target_is_directory=True)
    try:
        with pytest.raises(VerificationError, match="symbolic path components"):
            copy_state(source, source_base / "new")
    finally:
        connection.close()

    destination_case = tmp_path / "destination-case"
    destination_case.mkdir()
    source, connection = _source(destination_case)
    real_parent = destination_case / "real-parent"
    real_parent.mkdir()
    alias_parent = destination_case / "alias-parent"
    alias_parent.symlink_to(real_parent, target_is_directory=True)
    try:
        with pytest.raises(VerificationError, match="symbolic path components"):
            copy_state(source, alias_parent / "new")
        assert not (real_parent / "new").exists()
    finally:
        connection.close()


def test_database_parent_swap_and_restore_during_connect_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    primary_base = tmp_path / "primary"
    primary_base.mkdir()
    source, source_connection = _source(primary_base)
    source_connection.close()
    alternate_base = tmp_path / "alternate"
    alternate_base.mkdir()
    alternate, alternate_connection = _source(alternate_base)
    alternate_connection.execute("UPDATE agents SET model='alternate' WHERE id=10")
    alternate_connection.commit()
    alternate_connection.close()
    source_root = source.database.parent
    saved_root = source_root.with_name("legacy-saved")
    real_connect = sqlite3.connect
    swapped = False

    def connect_after_parent_swap(*args: object, **kwargs: object) -> sqlite3.Connection:
        nonlocal swapped
        database = str(args[0]) if args else str(kwargs.get("database", ""))
        if not swapped and str(source.database) in database:
            swapped = True
            source_root.rename(saved_root)
            source_root.symlink_to(alternate.database.parent, target_is_directory=True)
            try:
                return real_connect(*args, **kwargs)
            finally:
                source_root.unlink()
                saved_root.rename(source_root)
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(migration.sqlite3, "connect", connect_after_parent_swap)
    with pytest.raises(VerificationError, match="database parent changed"):
        copy_state(source, tmp_path / "new")
    assert swapped is True
    assert not (tmp_path / "new").exists()


def test_source_file_mutation_during_copy_is_detected(tmp_path: Path) -> None:
    source, connection = _source(tmp_path)
    destination = tmp_path / "new"
    message = (
        source.archive
        / "projects"
        / "project"
        / "messages"
        / "threads"
        / "thread-7.md"
    )
    mutated = False

    def fault(phase: str) -> None:
        nonlocal mutated
        if phase == "archive_copy:copy_chunk" and not mutated:
            mutated = True
            message.write_text("mutated during copy\n", encoding="utf-8")

    try:
        with pytest.raises(VerificationError, match="changed while it was copied"):
            copy_state(source, destination, fault_hook=fault)
        assert mutated is True
        assert not destination.exists()
    finally:
        connection.close()


def test_database_pragmas_are_preserved_and_verified(tmp_path: Path) -> None:
    source, writer = _source(tmp_path, wal=True)
    destination = tmp_path / "new"
    try:
        copy_state(source, destination)
        source_pragmas = snapshot_state(source)["database"]["pragmas"]
        destination_pragmas = snapshot_state(StatePaths.from_root(destination))[
            "database"
        ]["pragmas"]
        assert destination_pragmas == source_pragmas
        assert destination_pragmas["journal_mode"].lower() == "wal"

        changed = sqlite3.connect(destination / "storage.sqlite3")
        changed.execute("PRAGMA schema_version=999")
        changed.commit()
        changed.close()
        with pytest.raises(VerificationError, match="does not match"):
            verify_copy(source, destination)
    finally:
        writer.close()


def test_rollback_assessment_is_stage_aware_and_fails_closed_after_writes(
    tmp_path: Path,
) -> None:
    source, connection = _source(tmp_path)
    destination = tmp_path / "new"
    try:
        copy_state(source, destination)
        manifest = destination / MANIFEST_NAME
        before_writes = assess_rollback(manifest, "C4_NEW_SERVICE_READY")
        assert before_writes["status"] == "reversible"
        assert before_writes["destination_matches_baseline"] is True

        changed = sqlite3.connect(destination / "storage.sqlite3")
        changed.execute("UPDATE messages SET body_md='new-authority-write' WHERE id=20")
        changed.commit()
        changed.close()

        false_early_claim = assess_rollback(manifest, "C3_MIGRATION_VERIFIED")
        after_authority = assess_rollback(manifest, "C5_CLIENT_SWITCHING")
        assert false_early_claim["status"] == "no_go"
        assert after_authority["status"] == "no_go"
        assert after_authority["data_reversible"] is False
        assert after_authority["cutover_stage_provenance"] == "caller_asserted_unverified"
        assert "no verified reverse transform" in after_authority["reason"]
        actions = "\n".join(after_authority["actions"])
        assert "exact owned new job" in actions
        assert "bounded MCP readiness" in actions
        assert "start neither authority" in actions
        assert "start the legacy" not in actions
        assert "start only the legacy" not in actions
        assert "restore client" not in actions
    finally:
        connection.close()


def test_rollback_assessment_rejects_pre_manifest_stages(tmp_path: Path) -> None:
    source, connection = _source(tmp_path)
    destination = tmp_path / "new"
    try:
        copy_state(source, destination)
        with pytest.raises(MigrationError, match="only accepts C3-C6"):
            assess_rollback(destination / MANIFEST_NAME, "C2_LEGACY_QUIESCED")
    finally:
        connection.close()


def test_rollback_assessment_rejects_non_verified_manifest_status(
    tmp_path: Path,
) -> None:
    source, connection = _source(tmp_path)
    destination = tmp_path / "new"
    try:
        copy_state(source, destination)
        manifest_path = destination / MANIFEST_NAME
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["status"] = "C2_LEGACY_QUIESCED"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        with pytest.raises(MigrationError, match="verified C3 baseline"):
            assess_rollback(manifest_path, "C5_CLIENT_SWITCHING")
    finally:
        connection.close()


@pytest.mark.parametrize("policy", ("archive_policy", "database_policy"))
def test_manifest_copy_policy_tampering_is_rejected(
    tmp_path: Path, policy: str
) -> None:
    source, connection = _source(tmp_path)
    destination = tmp_path / "new"
    try:
        copy_state(source, destination)
        manifest_path = destination / MANIFEST_NAME
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest[policy]["copied"] = "legacy_git_history"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        with pytest.raises(MigrationError, match="unexpected .* policy"):
            verify_copy(source, destination)
        with pytest.raises(MigrationError, match="unexpected .* policy"):
            assess_rollback(manifest_path, "C4_NEW_SERVICE_READY")
    finally:
        connection.close()


def test_database_paths_with_uri_metacharacters_are_supported(tmp_path: Path) -> None:
    root = tmp_path / "mail #1?"
    root.mkdir()
    source, connection = _source(root)
    destination = root / "new #2?"
    try:
        assert copy_state(source, destination).status == "copied"
        assert verify_copy(source, destination)["status"] == "verified"
    finally:
        connection.close()


@pytest.mark.parametrize("surface", ("archive", "signals"))
def test_rollback_rejects_non_database_destination_divergence(
    tmp_path: Path,
    surface: str,
) -> None:
    source, connection = _source(tmp_path)
    destination = tmp_path / "new"
    try:
        copy_state(source, destination)
        target = destination / surface / "post-baseline"
        target.write_text("changed", encoding="utf-8")
        result = assess_rollback(
            destination / MANIFEST_NAME, "C6_NEW_AUTHORITY_VERIFIED"
        )
        assert result["status"] == "no_go"
        assert result["destination_matches_baseline"] is False
        if surface == "archive":
            assert "working tree is not clean" in result[
                "destination_verification_error"
            ]
    finally:
        connection.close()


def test_rollback_rejects_legacy_source_drift(tmp_path: Path) -> None:
    source, connection = _source(tmp_path)
    destination = tmp_path / "new"
    try:
        copy_state(source, destination)
        connection.execute("UPDATE messages SET body_md='legacy-drift' WHERE id=20")
        connection.commit()
        result = assess_rollback(destination / MANIFEST_NAME, "C4_NEW_SERVICE_READY")
        assert result["status"] == "no_go"
        assert result["source_matches_baseline"] is False
        actions = "\n".join(result["actions"])
        assert "start neither authority automatically" in actions
        assert "incident/no-writer" in actions
        assert "start the legacy" not in actions
    finally:
        connection.close()


def test_rollback_cli_returns_one_for_post_baseline_write(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source, connection = _source(tmp_path)
    destination = tmp_path / "new"
    try:
        copy_state(source, destination)
        (destination / "signals" / "new.signal").write_text("changed", encoding="utf-8")
        with pytest.raises(SystemExit) as exited:
            main(
                [
                    "rollback-assess",
                    "--manifest",
                    str(destination / MANIFEST_NAME),
                    "--cutover-stage",
                    "C5_CLIENT_SWITCHING",
                ]
            )
        assert exited.value.code == 1
        assert json.loads(capsys.readouterr().out)["status"] == "no_go"
    finally:
        connection.close()


def test_copy_cli_help_names_the_selected_working_tree_policy(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exited:
        migration._parser().parse_args(["copy", "--help"])
    output = " ".join(capsys.readouterr().out.split())
    assert exited.value.code == 0
    assert "archive working tree" in output
    assert "exclude legacy .git/server.pid" in output
    assert "canonical absolute" in output


@pytest.mark.parametrize(
    "value",
    (
        "legacy/storage.sqlite3",
        "/private/tmp/../tmp/storage.sqlite3",
        "~/storage.sqlite3",
        "/private/tmp//storage.sqlite3",
    ),
)
def test_cli_paths_reject_noncanonical_text(value: str) -> None:
    with pytest.raises(MigrationError, match="canonical absolute"):
        migration._canonical_absolute_path(value, label="test path")


def test_copy_cli_rejects_relative_paths_before_writing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source, connection = _source(tmp_path)
    destination = tmp_path / "new"
    monkeypatch.chdir(tmp_path)
    try:
        with pytest.raises(SystemExit) as exited:
            main(
                [
                    "copy",
                    "--source-db",
                    source.database.relative_to(tmp_path).as_posix(),
                    "--source-archive",
                    str(source.archive),
                    "--source-signals",
                    str(source.signals),
                    "--destination-root",
                    str(destination),
                ]
            )
        stderr = capsys.readouterr().err
        assert exited.value.code == 1
        assert "canonical absolute" in stderr
        assert "Traceback" not in stderr
        assert len(stderr.splitlines()) == 1
        assert not destination.exists()
    finally:
        connection.close()


@pytest.mark.parametrize("kind", ("symlink", "hardlink", "fifo", "oversize"))
def test_manifest_reader_rejects_unsafe_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    manifest = tmp_path / MANIFEST_NAME
    external = tmp_path / "external.json"
    if kind == "symlink":
        external.write_text("{}", encoding="utf-8")
        manifest.symlink_to(external)
    elif kind == "hardlink":
        external.write_text("{}", encoding="utf-8")
        os.link(external, manifest)
    elif kind == "fifo":
        os.mkfifo(manifest)
    else:
        monkeypatch.setattr(migration, "OWNERSHIP_JSON_MAX_BYTES", 8)
        manifest.write_bytes(b"123456789")

    with pytest.raises(MigrationError):
        migration._load_manifest(manifest)


def test_manifest_rejects_duplicate_bool_uuid_and_missing_fields(tmp_path: Path) -> None:
    source, connection = _source(tmp_path)
    destination = tmp_path / "new"
    try:
        copy_state(source, destination)
        manifest_path = destination / MANIFEST_NAME
        original = json.loads(manifest_path.read_text(encoding="utf-8"))

        duplicate = json.dumps(original, separators=(",", ":"))
        duplicate = duplicate[:-1] + ',"schema_version":1}'
        manifest_path.write_text(duplicate, encoding="utf-8")
        with pytest.raises(MigrationError, match="duplicate key"):
            verify_copy(source, destination)

        for key in original:
            missing = dict(original)
            missing.pop(key)
            manifest_path.write_text(json.dumps(missing), encoding="utf-8")
            with pytest.raises(MigrationError):
                verify_copy(source, destination)

        boolean_schema = dict(original)
        boolean_schema["schema_version"] = True
        manifest_path.write_text(json.dumps(boolean_schema), encoding="utf-8")
        with pytest.raises(MigrationError, match="schema version"):
            verify_copy(source, destination)

        non_uuid = dict(original)
        non_uuid["operation_id"] = "not-a-uuid"
        manifest_path.write_text(json.dumps(non_uuid), encoding="utf-8")
        with pytest.raises(MigrationError, match="UUID"):
            verify_copy(source, destination)
    finally:
        connection.close()


def test_rollback_cli_bounds_missing_manifest_paths(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source, connection = _source(tmp_path)
    destination = tmp_path / "new"
    try:
        copy_state(source, destination)
        manifest_path = destination / MANIFEST_NAME
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["source"].pop("database")
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        with pytest.raises(SystemExit) as exited:
            main(
                [
                    "rollback-assess",
                    "--manifest",
                    str(manifest_path),
                    "--cutover-stage",
                    "C4_NEW_SERVICE_READY",
                ]
            )
        stderr = capsys.readouterr().err
        assert exited.value.code == 1
        assert "source paths are malformed" in stderr
        assert "Traceback" not in stderr
        assert len(stderr.splitlines()) == 1
    finally:
        connection.close()


def test_manifest_contains_no_database_values(tmp_path: Path) -> None:
    source, connection = _source(tmp_path)
    destination = tmp_path / "new"
    try:
        copy_state(source, destination)
        payload = (destination / MANIFEST_NAME).read_text(encoding="utf-8")
        manifest = json.loads(payload)
        assert manifest["status"] == "C3_MIGRATION_VERIFIED"
        assert "body" not in payload
        assert "ProOpus" not in payload
        assert manifest["baseline"]["database"]["relations"]["thread_membership"]["count"] == 1
    finally:
        connection.close()


def test_rollback_rejects_internally_inconsistent_baseline_manifest(
    tmp_path: Path,
) -> None:
    source, connection = _source(tmp_path)
    destination = tmp_path / "new"
    try:
        copy_state(source, destination)
        manifest_path = destination / MANIFEST_NAME
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["baseline"]["database"]["tables"]["messages"]["count"] = 999
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        with pytest.raises(MigrationError, match="internally inconsistent"):
            assess_rollback(manifest_path, "C4_NEW_SERVICE_READY")
    finally:
        connection.close()
