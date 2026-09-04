from __future__ import annotations

import errno
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from agentstack_mail import migration
from agentstack_mail.migration import (
    COLD_BACKUP_RECEIPT_NAME,
    COLD_REHEARSAL_DAMAGE_PLAN,
    COLD_REHEARSAL_MARKER_NAME,
    COLD_REHEARSAL_RECEIPT_NAME,
    COLD_REHEARSAL_VERIFICATION_NAME,
    COLD_RESTORE_MARKER_NAME,
    MANIFEST_NAME,
    MIGRATION_FAULT_PHASES,
    POST_PUBLICATION_FAULT_PHASES,
    PRE_PUBLICATION_FAULT_PHASES,
    MigrationError,
    StatePaths,
    VerificationError,
    assess_rollback,
    check_cold_restore_rehearsal_verification,
    cold_backup_database,
    cold_restore_database,
    copy_state,
    main,
    rehearse_cold_restore,
    snapshot_state,
    verify_cold_restore_rehearsal,
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


def _descriptor_names_path(descriptor: int, path: Path) -> bool:
    try:
        path_info = path.lstat()
    except FileNotFoundError:
        return False
    descriptor_info = os.fstat(descriptor)
    return (
        descriptor_info.st_dev == path_info.st_dev
        and descriptor_info.st_ino == path_info.st_ino
    )


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
        assert manifest["rollback"] == {
            "post_authority_reverse_transform": "not_implemented",
            "reversibility_boundary": "first_new_authority_durable_write",
            "client_switching_before_boundary": (
                "reversible_if_both_authorities_equal_baseline"
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


@pytest.mark.skipif(
    os.environ.get("GITHUB_ACTIONS") == "true",
    reason="starts a real dashboard/service process; fails only on GitHub-hosted runners (no interactive user session), cause not isolated yet — run 33846626836",
)
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


@pytest.mark.parametrize(
    ("scenario", "expected"),
    [
        pytest.param("parent_swap", "rejected", id="parent-swap-is-rejected"),
        pytest.param(
            "container_sibling_churn",
            "accepted",
            id="unrelated-container-sibling-churn-is-accepted",
        ),
    ],
)
def test_database_parent_identity_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    scenario: str,
    expected: str,
) -> None:
    primary_base = tmp_path / "primary"
    primary_base.mkdir()
    source, source_connection = _source(primary_base)
    source_connection.close()
    real_connect = sqlite3.connect
    changed = False

    if scenario == "parent_swap":
        alternate_base = tmp_path / "alternate"
        alternate_base.mkdir()
        alternate, alternate_connection = _source(alternate_base)
        alternate_connection.execute("UPDATE agents SET model='alternate' WHERE id=10")
        alternate_connection.commit()
        alternate_connection.close()
        source_root = source.database.parent
        saved_root = source_root.with_name("legacy-saved")

        def connect_after_change(
            *args: object, **kwargs: object
        ) -> sqlite3.Connection:
            nonlocal changed
            database = str(args[0]) if args else str(kwargs.get("database", ""))
            if not changed and str(source.database) in database:
                changed = True
                source_root.rename(saved_root)
                shutil.copytree(alternate.database.parent, source_root)
            return real_connect(*args, **kwargs)

    else:

        def connect_after_change(
            *args: object, **kwargs: object
        ) -> sqlite3.Connection:
            nonlocal changed
            database = str(args[0]) if args else str(kwargs.get("database", ""))
            if not changed and str(source.database) in database:
                changed = True
                sibling = source.database.parent.parent / "unrelated-sibling"
                sibling.mkdir()
                sibling.rmdir()
            return real_connect(*args, **kwargs)

    monkeypatch.setattr(migration.sqlite3, "connect", connect_after_change)
    destination = tmp_path / "new"
    if expected == "rejected":
        try:
            with pytest.raises(
                VerificationError, match="database (?:parent )?changed"
            ):
                copy_state(source, destination)
            assert not destination.exists()
        finally:
            if source_root.exists():
                shutil.rmtree(source_root)
            saved_root.rename(source_root)
    else:
        assert copy_state(source, destination).status == "copied"
    assert changed is True


def test_git_environment_disables_all_automatic_maintenance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "gc.auto")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_9", "maintenance.auto")
    monkeypatch.setenv("GIT_CONFIG_VALUE_9", "true")
    monkeypatch.setenv("GIT_CONFIG_PARAMETERS", "'gc.auto=1'")
    environment = migration._git_environment(None)

    assert "GIT_CONFIG_PARAMETERS" not in environment
    assert environment["GIT_CONFIG_COUNT"] == "3"
    assert environment["GIT_CONFIG_KEY_0"] == "gc.auto"
    assert environment["GIT_CONFIG_VALUE_0"] == "0"
    assert environment["GIT_CONFIG_KEY_1"] == "gc.autoDetach"
    assert environment["GIT_CONFIG_VALUE_1"] == "false"
    assert environment["GIT_CONFIG_KEY_2"] == "maintenance.auto"
    assert environment["GIT_CONFIG_VALUE_2"] == "false"
    assert environment["GIT_CONFIG_KEY_9"] == "maintenance.auto"

    observed = {}
    for key in ("gc.auto", "gc.autoDetach", "maintenance.auto"):
        completed = subprocess.run(
            ["git", "config", "--get", key],
            check=False,
            capture_output=True,
            text=True,
            env=environment,
            timeout=5,
        )
        assert completed.returncode == 0 and completed.stderr == ""
        observed[key] = completed.stdout.strip()
    assert observed == {
        "gc.auto": "0",
        "gc.autoDetach": "false",
        "maintenance.auto": "false",
    }


def test_cleanup_failure_does_not_mask_primary_migration_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, connection = _source(tmp_path)
    destination = tmp_path / "new"

    def primary_failure(phase: str) -> None:
        if phase == "before_verification":
            raise VerificationError("primary snapshot failure")

    def cleanup_failure(_path: Path) -> None:
        raise FileNotFoundError(errno.ENOENT, "missing", "gc.pid")

    monkeypatch.setattr(migration.shutil, "rmtree", cleanup_failure)
    try:
        with pytest.raises(VerificationError, match="primary snapshot failure") as raised:
            copy_state(source, destination, fault_hook=primary_failure)
        assert raised.value.__notes__ == [
            "owned staging cleanup also failed: "
            "FileNotFoundError: [Errno 2] missing: 'gc.pid'"
        ]
    finally:
        connection.close()


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
        before_writes = assess_rollback(manifest, "C5_CLIENT_SWITCHING")
        assert before_writes["status"] == "reversible"
        assert before_writes["destination_matches_baseline"] is True

        c6 = assess_rollback(manifest, "C6_NEW_AUTHORITY_VERIFIED")
        assert c6["status"] == "no_go"
        assert c6["data_reversible"] is False
        assert c6["destination_matches_baseline"] is True
        assert c6["cutover_stage"] == "C6_NEW_AUTHORITY_VERIFIED"
        assert c6["cutover_stage_provenance"] == "caller_asserted_unverified"
        assert "fix-forward-only" in c6["reason"]
        c6_actions = "\n".join(c6["actions"])
        assert "exact owned new job" in c6_actions
        assert "legacy service stopped" in c6_actions
        assert "start the legacy" not in c6_actions
        assert "restore client" not in c6_actions

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


@pytest.mark.parametrize(
    "kind", ("symlink", "parent_symlink", "hardlink", "fifo", "oversize")
)
def test_manifest_reader_rejects_unsafe_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    manifest = tmp_path / MANIFEST_NAME
    external = tmp_path / "external.json"
    if kind == "symlink":
        external.write_text("{}", encoding="utf-8")
        manifest.symlink_to(external)
    elif kind == "parent_symlink":
        real_parent = tmp_path / "real-parent"
        real_parent.mkdir()
        alias_parent = tmp_path / "alias-parent"
        alias_parent.symlink_to(real_parent, target_is_directory=True)
        manifest = alias_parent / MANIFEST_NAME
        (real_parent / MANIFEST_NAME).write_text("{}", encoding="utf-8")
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


@pytest.mark.parametrize(
    "payload",
    (
        '{"schema_version":' + ("9" * 5000) + "}",
        ("[" * 5000) + "0" + ("]" * 5000),
    ),
)
def test_rollback_cli_bounds_pathological_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], payload: str
) -> None:
    manifest = tmp_path / MANIFEST_NAME
    manifest.write_text(payload, encoding="utf-8")
    with pytest.raises(SystemExit) as exited:
        main(
            [
                "rollback-assess",
                "--manifest",
                str(manifest),
                "--cutover-stage",
                "C4_NEW_SERVICE_READY",
            ]
        )
    stderr = capsys.readouterr().err
    assert exited.value.code == 1
    assert stderr.startswith("agentstack-mail-migrate:")
    assert "Traceback" not in stderr
    assert len(stderr.splitlines()) == 1


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


def _copy_cold_family_to_target(backup: Path, destination: Path) -> None:
    receipt = json.loads(
        (backup / COLD_BACKUP_RECEIPT_NAME).read_text(encoding="utf-8")
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    targets = {
        "main": destination,
        "wal": Path(f"{destination}-wal"),
        "shm": Path(f"{destination}-shm"),
    }
    for role, target in targets.items():
        record = receipt["files"][role]
        if record["state"] == "PRESENT":
            shutil.copy2(backup / record["backup_name"], target)


def _cold_restore_manifest(source: StatePaths, root: Path) -> Path:
    destination = root / "migration-copy"
    copy_state(source, destination)
    return destination / MANIFEST_NAME


def _isolated_rehearsal_inputs(tmp_path: Path) -> tuple[StatePaths, Path]:
    source, connection = _source(tmp_path)
    connection.close()
    return source, _cold_restore_manifest(source, tmp_path)


def _rehearsal_candidate(tmp_path: Path) -> tuple[Path, str]:
    repository = tmp_path / "candidate-repository"
    repository.mkdir()
    subprocess.run(["git", "init", "-q", str(repository)], check=True)
    subprocess.run(
        ["git", "-C", str(repository), "config", "user.name", "Candidate Test"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repository), "config", "user.email", "candidate@example.test"],
        check=True,
    )
    module_target = (
        repository
        / "packages"
        / "agentstack_mail"
        / "src"
        / "agentstack_mail"
        / "migration.py"
    )
    module_target.parent.mkdir(parents=True)
    shutil.copy2(Path(migration.__file__), module_target)
    generator_source = (
        Path(__file__).parents[1] / "scripts" / "build_rehearsal_seed.py"
    )
    generator_target = (
        repository
        / "packages"
        / "agentstack_mail"
        / "scripts"
        / "build_rehearsal_seed.py"
    )
    generator_target.parent.mkdir(parents=True)
    shutil.copy2(generator_source, generator_target)
    subprocess.run(["git", "-C", str(repository), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(repository), "commit", "-q", "-m", "candidate"],
        check=True,
    )
    commit = subprocess.run(
        ["git", "-C", str(repository), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return repository, commit


def _rehearsal_provenance(tmp_path: Path, seed_database: Path) -> Path:
    path = tmp_path / "seed-provenance.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "production-shaped-synthetic",
                "created_at": "2026-08-11T00:00:00+00:00",
                "seed_database": str(seed_database),
                "production_source_database": "/production/mcp-agent-mail/storage.sqlite3",
                "acquisition_method": "deterministic test generator",
                "source_reference": (
                    f"candidate-bound unit generator receipt:{tmp_path / 'generator-receipt.json'}"
                ),
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    repository = tmp_path / "candidate-repository"
    candidate_commit = subprocess.run(
        ["git", "-C", str(repository), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    main = migration._fingerprint_regular_file(seed_database, required=True)
    provenance_fingerprint = migration._fingerprint_regular_file(path, required=True)
    assert main is not None
    assert provenance_fingerprint is not None
    connection = sqlite3.connect(seed_database)
    try:
        counts = {
            table: connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            for table in sorted(migration.REQUIRED_TABLES)
        }
    finally:
        connection.close()
    generator_relative = Path(
        "packages/agentstack_mail/scripts/build_rehearsal_seed.py"
    )
    migration_relative = Path(
        "packages/agentstack_mail/src/agentstack_mail/migration.py"
    )
    generator_fingerprint = migration._fingerprint_regular_file(
        repository / generator_relative, required=True
    )
    migration_fingerprint = migration._fingerprint_regular_file(
        repository / migration_relative, required=True
    )
    assert generator_fingerprint is not None
    assert migration_fingerprint is not None
    receipt = tmp_path / "generator-receipt.json"
    receipt.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "production-shaped-synthetic-seed-generation",
                "run_id": "22222222-2222-4222-8222-222222222222",
                "started_at": "2026-08-11T00:00:00+00:00",
                "completed_at": "2026-08-11T00:00:01+00:00",
                "candidate_commit": candidate_commit,
                "candidate_checkout": {
                    "repository": str(repository),
                    "head": candidate_commit,
                    "tracked_and_untracked_worktree_clean": True,
                    "executing_file_sha256": {
                        generator_relative.as_posix(): generator_fingerprint["sha256"],
                        migration_relative.as_posix(): migration_fingerprint["sha256"],
                    },
                },
                "output_root": str(seed_database.parent.parent),
                "production_source_database": (
                    "/production/mcp-agent-mail/storage.sqlite3"
                ),
                "production_source_opened": False,
                "seed_database": str(seed_database),
                "seed_database_size": main["size"],
                "seed_database_sha256": main["sha256"],
                "seed_database_family": {
                    "main": {
                        "state": "PRESENT",
                        "size": main["size"],
                        "sha256": main["sha256"],
                    },
                    "wal": {"state": "ABSENT"},
                    "shm": {"state": "ABSENT"},
                },
                "seed_archive": {
                    "path": str(seed_database.parent / "archive"),
                    "snapshot": migration.snapshot_tree(
                        seed_database.parent / "archive",
                        required=True,
                        excluded_root_names=migration.ARCHIVE_EXCLUDED_ROOT_NAMES,
                    ),
                },
                "seed_signals": {
                    "path": str(seed_database.parent / "signals"),
                    "snapshot": migration.snapshot_tree(
                        seed_database.parent / "signals", required=False
                    ),
                },
                "major_table_rows": counts,
                "seed_provenance": str(path),
                "seed_provenance_sha256": provenance_fingerprint["sha256"],
                "scale_floor": migration.REHEARSAL_SCALE_MINIMUMS,
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def _generator_receipt_evidence(provenance: Path) -> tuple[Path, str]:
    receipt = provenance.with_name("generator-receipt.json")
    fingerprint = migration._fingerprint_regular_file(receipt, required=True)
    assert fingerprint is not None
    return receipt, fingerprint["sha256"]


def _generator_receipt_arguments(provenance: Path) -> dict[str, object]:
    receipt, sha256 = _generator_receipt_evidence(provenance)
    return {
        "generator_receipt": receipt,
        "expected_generator_receipt_sha256": sha256,
    }


def _completed_rehearsal_for_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, dict[str, object], Path, str]:
    source, manifest = _isolated_rehearsal_inputs(tmp_path)
    repository, candidate_commit = _rehearsal_candidate(tmp_path)
    provenance = _rehearsal_provenance(tmp_path, source.database)
    run_directory = tmp_path / "completed-rehearsal"
    monkeypatch.setattr(migration, "_assert_production_shaped_scale", lambda _scale: None)
    result = rehearse_cold_restore(
        source.database,
        Path("/production/mcp-agent-mail/storage.sqlite3"),
        run_directory,
        manifest,
        repository,
        provenance,
        **_generator_receipt_arguments(provenance),
        candidate_commit=candidate_commit,
    )
    return run_directory, result, repository, candidate_commit


def test_cold_backup_and_restore_rehearsal_preserves_committed_wal_logically(
    tmp_path: Path,
) -> None:
    source, connection = _source(tmp_path, wal=True)
    backup = tmp_path / "sealed-backup"
    rehearsal_database = tmp_path / "rehearsal" / "storage.sqlite3"
    restore_receipt = tmp_path / "receipts" / "restore.json"
    restore_receipt.parent.mkdir()
    try:
        connection.execute(
            "INSERT INTO messages VALUES "
            "(21, 1, 10, 'thread-7', 'wal-only', 'committed-wal', 'normal', 0, "
            "'2026-08-10T00:05:00', '[]')"
        )
        connection.commit()
        assert Path(f"{source.database}-wal").is_file()
        assert Path(f"{source.database}-shm").is_file()

        backed_up = cold_backup_database(
            source.database, backup, services_stopped=True
        )
        migration_manifest = _cold_restore_manifest(source, tmp_path)
        backup_receipt = json.loads(
            (backup / COLD_BACKUP_RECEIPT_NAME).read_text(encoding="utf-8")
        )
        assert backed_up["status"] == "backed_up"
        assert backup_receipt["files"]["main"]["state"] == "PRESENT"
        assert backup_receipt["files"]["wal"]["state"] == "PRESENT"
        assert backup_receipt["files"]["shm"]["state"] == "PRESENT"

        _copy_cold_family_to_target(backup, rehearsal_database)
        rehearsal_database.write_bytes(b"injected-corruption")
        restored = cold_restore_database(
            backup,
            rehearsal_database,
            restore_receipt,
            migration_manifest,
            services_stopped=True,
            target_kind="rehearsal-copy",
            fault_injection="truncate-main",
        )

        receipt = json.loads(restore_receipt.read_text(encoding="utf-8"))
        assert restored["status"] == "restored"
        assert receipt["target"] == {
            "kind": "rehearsal-copy",
            "database": str(rehearsal_database),
            "production_source": False,
        }
        assert receipt["fault_injection"] == {
            "description": "truncate-main",
            "observed": True,
            "pre_restore_generation_sha256": receipt["fault_injection"][
                "pre_restore_generation_sha256"
            ],
            "backup_generation_sha256": receipt["fault_injection"][
                "backup_generation_sha256"
            ],
            "provenance": "observed_file_divergence",
        }
        assert (
            receipt["fault_injection"]["pre_restore_generation_sha256"]
            != receipt["fault_injection"]["backup_generation_sha256"]
        )
        assert receipt["migration_identity"]["manifest"] == str(migration_manifest)
        assert receipt["restore_result"]["status"] == "restored"
        assert receipt["logical_validator"] == {
            "status": "matched",
            "comparison": "schema_rows_relations_pragmas",
            "logical_sha256": backup_receipt["logical_sha256"],
        }
        restored_connection = sqlite3.connect(rehearsal_database)
        try:
            assert restored_connection.execute(
                "SELECT body_md FROM messages WHERE id=21"
            ).fetchone() == ("committed-wal",)
        finally:
            restored_connection.close()
    finally:
        connection.close()


def test_cold_restore_removes_sidecars_recorded_absent(tmp_path: Path) -> None:
    source, connection = _source(tmp_path)
    backup = tmp_path / "sealed-backup"
    destination = tmp_path / "rehearsal" / "storage.sqlite3"
    receipt_path = tmp_path / "restore-receipt.json"
    connection.close()

    cold_backup_database(source.database, backup, services_stopped=True)
    migration_manifest = _cold_restore_manifest(source, tmp_path)
    backup_receipt = json.loads(
        (backup / COLD_BACKUP_RECEIPT_NAME).read_text(encoding="utf-8")
    )
    assert backup_receipt["files"]["wal"]["state"] == "ABSENT"
    assert backup_receipt["files"]["shm"]["state"] == "ABSENT"

    destination.parent.mkdir()
    destination.write_bytes(b"corrupt-main")
    Path(f"{destination}-wal").write_bytes(b"unexpected-wal")
    Path(f"{destination}-shm").write_bytes(b"unexpected-shm")
    cold_restore_database(
        backup,
        destination,
        receipt_path,
        migration_manifest,
        services_stopped=True,
        target_kind="rehearsal-copy",
        fault_injection="corrupt-main-and-create-sidecars",
    )

    assert not Path(f"{destination}-wal").exists()
    assert not Path(f"{destination}-shm").exists()
    restore_receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert restore_receipt["restore_result"]["files"]["wal"] == {
        "expected_state": "ABSENT",
        "result": "atomic_quarantine_remove",
    }
    assert restore_receipt["restore_result"]["files"]["shm"] == {
        "expected_state": "ABSENT",
        "result": "atomic_quarantine_remove",
    }


def test_cold_commands_require_services_stopped_and_rehearsal_fault(
    tmp_path: Path,
) -> None:
    source, connection = _source(tmp_path)
    backup = tmp_path / "sealed-backup"
    try:
        with pytest.raises(MigrationError, match="services-stopped"):
            cold_backup_database(source.database, backup, services_stopped=False)
        cold_backup_database(source.database, backup, services_stopped=True)
        migration_manifest = _cold_restore_manifest(source, tmp_path)
        with pytest.raises(MigrationError, match="services-stopped"):
            cold_restore_database(
                backup,
                tmp_path / "target.sqlite3",
                tmp_path / "restore.json",
                migration_manifest,
                services_stopped=False,
                target_kind="rehearsal-copy",
                fault_injection="truncate-main",
            )
        with pytest.raises(MigrationError, match="fault injection"):
            cold_restore_database(
                backup,
                tmp_path / "target.sqlite3",
                tmp_path / "restore.json",
                migration_manifest,
                services_stopped=True,
                target_kind="rehearsal-copy",
                fault_injection="none",
            )
    finally:
        connection.close()


def test_cold_restore_rejects_tampered_backup_before_mutating_target(
    tmp_path: Path,
) -> None:
    source, connection = _source(tmp_path)
    backup = tmp_path / "sealed-backup"
    destination = tmp_path / "target.sqlite3"
    try:
        cold_backup_database(source.database, backup, services_stopped=True)
        migration_manifest = _cold_restore_manifest(source, tmp_path)
        destination.write_bytes(b"unchanged-target")
        (backup / "storage.sqlite3").write_bytes(b"tampered")
        with pytest.raises(VerificationError, match="do not match.*receipt"):
            cold_restore_database(
                backup,
                destination,
                tmp_path / "restore.json",
                migration_manifest,
                services_stopped=True,
                target_kind="rehearsal-copy",
                fault_injection="truncate-main",
            )
        assert destination.read_bytes() == b"unchanged-target"
        assert not (tmp_path / "restore.json").exists()
    finally:
        connection.close()


def test_cold_backup_fsyncs_regular_files_receipt_and_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, connection = _source(tmp_path)
    backup = tmp_path / "sealed-backup"
    original_fsync = os.fsync
    fsynced_modes: list[int] = []

    def recording_fsync(descriptor: int) -> None:
        fsynced_modes.append(os.fstat(descriptor).st_mode)
        original_fsync(descriptor)

    monkeypatch.setattr(migration.os, "fsync", recording_fsync)
    try:
        cold_backup_database(source.database, backup, services_stopped=True)
    finally:
        connection.close()
    assert sum(stat.S_ISREG(mode) for mode in fsynced_modes) >= 3
    assert sum(stat.S_ISDIR(mode) for mode in fsynced_modes) >= 3


@pytest.mark.parametrize(
    "seam", ["raw-file", "receipt-file", "receipt-directory", "publish-parent"]
)
def test_cold_backup_eio_never_leaves_a_canonical_success_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, seam: str
) -> None:
    source, connection = _source(tmp_path)
    connection.close()
    backup = tmp_path / "sealed-backup"
    injected = False
    original_fsync = migration.os.fsync
    original_fsync_directory = migration._fsync_directory

    if seam in {"raw-file", "receipt-file"}:

        def fail_file_fsync(descriptor: int) -> None:
            nonlocal injected
            staging = list(tmp_path.glob(".sealed-backup.cold-backup-*"))
            target_name = (
                "storage.sqlite3"
                if seam == "raw-file"
                else COLD_BACKUP_RECEIPT_NAME
            )
            target = staging[0] / target_name if staging else tmp_path / "missing"
            if not injected and _descriptor_names_path(descriptor, target):
                injected = True
                raise OSError(errno.EIO, f"injected backup {seam} fsync failure")
            original_fsync(descriptor)

        monkeypatch.setattr(migration.os, "fsync", fail_file_fsync)
    else:

        def fail_directory_fsync(path: Path) -> None:
            nonlocal injected
            staging_receipts = list(
                tmp_path.glob(
                    f".sealed-backup.cold-backup-*/{COLD_BACKUP_RECEIPT_NAME}"
                )
            )
            should_fail = (
                seam == "receipt-directory"
                and path.name.startswith(".sealed-backup.cold-backup-")
                and bool(staging_receipts)
            ) or (seam == "publish-parent" and path == backup.parent and backup.exists())
            if should_fail and not injected:
                injected = True
                raise OSError(errno.EIO, f"injected backup {seam} fsync failure")
            original_fsync_directory(path)

        monkeypatch.setattr(migration, "_fsync_directory", fail_directory_fsync)

    with pytest.raises(OSError, match=f"backup {seam} fsync failure"):
        cold_backup_database(source.database, backup, services_stopped=True)
    assert injected is True
    assert not backup.exists()
    assert not (backup / COLD_BACKUP_RECEIPT_NAME).exists()
    staging = list(tmp_path.glob(".sealed-backup.cold-backup-*"))
    if seam == "publish-parent":
        unconfirmed = [path for path in staging if path.name.endswith(".unconfirmed")]
        assert len(unconfirmed) == 1
        receipt = json.loads(
            (unconfirmed[0] / COLD_BACKUP_RECEIPT_NAME).read_text(encoding="utf-8")
        )
        assert receipt["kind"] == "cold-backup"
    else:
        assert staging == []


def test_cold_backup_rejects_cross_generation_database_family(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, connection = _source(tmp_path, wal=True)
    backup = tmp_path / "sealed-backup"
    original_copy = migration._copy_regular_file_exact
    mutated = False

    def copy_then_checkpoint(source_path: Path, destination: Path) -> dict[str, object]:
        nonlocal mutated
        result = original_copy(source_path, destination)
        if source_path == source.database and not mutated:
            connection.execute(
                "UPDATE messages SET body_md='new-generation' WHERE id=20"
            )
            connection.commit()
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            mutated = True
        return result

    monkeypatch.setattr(migration, "_copy_regular_file_exact", copy_then_checkpoint)
    try:
        with pytest.raises(VerificationError, match="family changed"):
            cold_backup_database(source.database, backup, services_stopped=True)
        assert mutated is True
        assert not backup.exists()
    finally:
        connection.close()


def test_cold_restore_rehearsal_requires_observed_divergence(tmp_path: Path) -> None:
    source, connection = _source(tmp_path)
    backup = tmp_path / "sealed-backup"
    destination = tmp_path / "pristine" / "storage.sqlite3"
    receipt = tmp_path / "restore.json"
    try:
        cold_backup_database(source.database, backup, services_stopped=True)
        manifest = _cold_restore_manifest(source, tmp_path)
        _copy_cold_family_to_target(backup, destination)
        before = destination.read_bytes()
        with pytest.raises(MigrationError, match="observed target divergence"):
            cold_restore_database(
                backup,
                destination,
                receipt,
                manifest,
                services_stopped=True,
                target_kind="rehearsal-copy",
                fault_injection="fake-label-only",
            )
        assert destination.read_bytes() == before
        assert not receipt.exists()
    finally:
        connection.close()


def test_cold_restore_target_kind_matches_recorded_source(tmp_path: Path) -> None:
    source, connection = _source(tmp_path)
    backup = tmp_path / "sealed-backup"
    try:
        cold_backup_database(source.database, backup, services_stopped=True)
        manifest = _cold_restore_manifest(source, tmp_path)
        with pytest.raises(MigrationError, match="rehearsal restore cannot target"):
            cold_restore_database(
                backup,
                source.database,
                tmp_path / "rehearsal.json",
                manifest,
                services_stopped=True,
                target_kind="rehearsal-copy",
                fault_injection="truncate-main",
            )
        with pytest.raises(MigrationError, match="must target the recorded source"):
            cold_restore_database(
                backup,
                tmp_path / "different.sqlite3",
                tmp_path / "production.json",
                manifest,
                services_stopped=True,
                target_kind="production-source",
                fault_injection="none",
            )
    finally:
        connection.close()


def test_cold_restore_binds_migration_manifest_to_backup(tmp_path: Path) -> None:
    (tmp_path / "one").mkdir()
    (tmp_path / "two").mkdir()
    source, connection = _source(tmp_path / "one")
    other_source, other_connection = _source(tmp_path / "two")
    backup = tmp_path / "sealed-backup"
    destination = tmp_path / "target.sqlite3"
    try:
        cold_backup_database(source.database, backup, services_stopped=True)
        wrong_manifest = _cold_restore_manifest(other_source, tmp_path / "two")
        destination.write_bytes(b"corrupt")
        with pytest.raises(MigrationError, match="different source databases"):
            cold_restore_database(
                backup,
                destination,
                tmp_path / "restore.json",
                wrong_manifest,
                services_stopped=True,
                target_kind="rehearsal-copy",
                fault_injection="truncate-main",
            )
        assert destination.read_bytes() == b"corrupt"
    finally:
        connection.close()
        other_connection.close()


def test_cold_restore_rejects_receipt_database_path_collision(tmp_path: Path) -> None:
    source, connection = _source(tmp_path)
    backup = tmp_path / "sealed-backup"
    destination = tmp_path / "target.sqlite3"
    try:
        cold_backup_database(source.database, backup, services_stopped=True)
        manifest = _cold_restore_manifest(source, tmp_path)
        destination.write_bytes(b"corrupt")
        wal_path = Path(f"{destination}-wal")
        with pytest.raises(MigrationError, match="must not replace a database family"):
            cold_restore_database(
                backup,
                destination,
                wal_path,
                manifest,
                services_stopped=True,
                target_kind="rehearsal-copy",
                fault_injection="truncate-main",
            )
        assert destination.read_bytes() == b"corrupt"
        assert not wal_path.exists()
    finally:
        connection.close()


@pytest.mark.parametrize("role", ("main", "wal", "shm"))
def test_cold_restore_rejects_migration_manifest_database_collision(
    tmp_path: Path, role: str
) -> None:
    source, connection = _source(tmp_path)
    backup = tmp_path / "sealed-backup"
    try:
        cold_backup_database(source.database, backup, services_stopped=True)
        original_manifest = _cold_restore_manifest(source, tmp_path)
        if role == "main":
            manifest = original_manifest
            destination = manifest
        else:
            collision_root = tmp_path / f"collision-{role}"
            collision_root.mkdir()
            destination = collision_root / "storage.sqlite3"
            manifest = Path(f"{destination}-{role}")
            shutil.copy2(original_manifest, manifest)
        manifest_before = manifest.read_bytes()
        with pytest.raises(MigrationError, match="must not replace its migration manifest"):
            cold_restore_database(
                backup,
                destination,
                tmp_path / f"restore-{role}.json",
                manifest,
                services_stopped=True,
                target_kind="rehearsal-copy",
                fault_injection="truncate-main",
            )
        assert manifest.read_bytes() == manifest_before
    finally:
        connection.close()


def test_cold_restore_rehearsal_cannot_target_recorded_archive(tmp_path: Path) -> None:
    source, connection = _source(tmp_path)
    backup = tmp_path / "sealed-backup"
    try:
        cold_backup_database(source.database, backup, services_stopped=True)
        manifest = _cold_restore_manifest(source, tmp_path)
        archive_file = next(path for path in source.archive.rglob("*.md"))
        before = archive_file.read_bytes()
        with pytest.raises(MigrationError, match="outside every recorded authority surface"):
            cold_restore_database(
                backup,
                archive_file,
                tmp_path / "restore.json",
                manifest,
                services_stopped=True,
                target_kind="rehearsal-copy",
                fault_injection="truncate-main",
            )
        assert archive_file.read_bytes() == before
    finally:
        connection.close()


def test_cold_restore_malformed_receipt_is_one_line_cli_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    backup = tmp_path / "backup"
    backup.mkdir()
    (backup / COLD_BACKUP_RECEIPT_NAME).write_text("[]", encoding="utf-8")
    manifest = tmp_path / "manifest.json"
    manifest.write_text("{}", encoding="utf-8")
    with pytest.raises(SystemExit) as exited:
        main(
            [
                "cold-restore",
                "--backup-dir",
                str(backup),
                "--destination-db",
                str(tmp_path / "target.sqlite3"),
                "--restore-receipt",
                str(tmp_path / "restore.json"),
                "--migration-manifest",
                str(manifest),
                "--services-stopped",
                "--target-kind",
                "rehearsal-copy",
                "--fault-injection",
                "truncate-main",
            ]
        )
    stderr = capsys.readouterr().err
    assert exited.value.code == 1
    assert "cold backup receipt has an unexpected shape" in stderr
    assert "Traceback" not in stderr
    assert len(stderr.splitlines()) == 1


def test_cold_restore_writes_success_receipt_only_after_staging_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, connection = _source(tmp_path)
    backup = tmp_path / "sealed-backup"
    destination = tmp_path / "target.sqlite3"
    receipt = tmp_path / "restore.json"
    original_rmtree = migration.shutil.rmtree
    injected = False

    def fail_first_restore_cleanup(path: Path, *args: object, **kwargs: object) -> None:
        nonlocal injected
        candidate = Path(path)
        if ".cold-restore-" in candidate.name and not injected:
            injected = True
            raise OSError("injected cleanup failure")
        original_rmtree(candidate, *args, **kwargs)

    try:
        cold_backup_database(source.database, backup, services_stopped=True)
        manifest = _cold_restore_manifest(source, tmp_path)
        destination.write_bytes(b"corrupt")
        monkeypatch.setattr(migration.shutil, "rmtree", fail_first_restore_cleanup)
        with pytest.raises(OSError, match="injected cleanup failure"):
            cold_restore_database(
                backup,
                destination,
                receipt,
                manifest,
                services_stopped=True,
                target_kind="rehearsal-copy",
                fault_injection="truncate-main",
            )
        assert injected is True
        assert not receipt.exists()
    finally:
        connection.close()


def test_rollback_cli_c5_and_c6_share_one_fresh_manifest_boundary(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source, connection = _source(tmp_path)
    destination = tmp_path / "new"
    try:
        copy_state(source, destination)
        manifest = str(destination / MANIFEST_NAME)
        main(
            [
                "rollback-assess",
                "--manifest",
                manifest,
                "--cutover-stage",
                "C5_CLIENT_SWITCHING",
            ]
        )
        c5_streams = capsys.readouterr()
        c5 = json.loads(c5_streams.out)
        assert c5_streams.err == ""
        assert c5["status"] == "reversible"
        assert c5["source_matches_baseline"] is True
        assert c5["destination_matches_baseline"] is True
        assert c5["data_reversible"] is True

        with pytest.raises(SystemExit) as exited:
            main(
                [
                    "rollback-assess",
                    "--manifest",
                    manifest,
                    "--cutover-stage",
                    "C6_NEW_AUTHORITY_VERIFIED",
                ]
            )
        streams = capsys.readouterr()
        result = json.loads(streams.out)
        assert exited.value.code == 1
        assert streams.err == ""
        assert result["status"] == "no_go"
        assert result["source_matches_baseline"] is True
        assert result["destination_matches_baseline"] is True
        assert result["data_reversible"] is False
        assert "durable" in result["reason"]
        assert result["cutover_stage_provenance"] == "caller_asserted_unverified"
        assert result["service_and_client_state_requires_external_verification"] is True
        actions = "\n".join(result["actions"])
        assert "start the legacy" not in actions
        assert "restore client" not in actions
    finally:
        connection.close()


@pytest.mark.parametrize(
    "arguments, diagnostic",
    (
        (("--cutover-stage", "C5_TO_C6"), "invalid choice"),
        (("--cutover-stage", "C6_CUTOVER_COMPLETE"), "invalid choice"),
        ((), "required"),
        (("--cutover-stage", "C0_LEGACY_AUTHORITY_PREPARED"), "invalid choice"),
        (("--cutover-stage", "C1_NEW_INSTALLED"), "invalid choice"),
        (("--cutover-stage", "C2_LEGACY_QUIESCED"), "invalid choice"),
    ),
)
def test_rollback_cli_rejects_unknown_omitted_and_pre_manifest_stages(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    arguments: tuple[str, ...],
    diagnostic: str,
) -> None:
    missing_manifest = tmp_path / "does-not-exist.json"
    with pytest.raises(SystemExit) as exited:
        main(["rollback-assess", "--manifest", str(missing_manifest), *arguments])
    streams = capsys.readouterr()
    assert exited.value.code == 2
    assert streams.out == ""
    assert diagnostic in streams.err


@pytest.mark.parametrize("stage", ("C3_MIGRATION_VERIFIED", "C4_NEW_SERVICE_READY", "C5_CLIENT_SWITCHING"))
@pytest.mark.parametrize("changed_authority", ("source", "destination"))
def test_rollback_cli_fails_closed_for_each_preboundary_single_record_drift(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    stage: str,
    changed_authority: str,
) -> None:
    source, connection = _source(tmp_path)
    destination = tmp_path / "new"
    try:
        copy_state(source, destination)
        if changed_authority == "source":
            connection.execute("UPDATE messages SET body_md='source-drift' WHERE id=20")
            connection.commit()
        else:
            changed = sqlite3.connect(destination / "storage.sqlite3")
            changed.execute("UPDATE messages SET body_md='destination-drift' WHERE id=20")
            changed.commit()
            changed.close()
        with pytest.raises(SystemExit) as exited:
            main(
                [
                    "rollback-assess",
                    "--manifest",
                    str(destination / MANIFEST_NAME),
                    "--cutover-stage",
                    stage,
                ]
            )
        streams = capsys.readouterr()
        result = json.loads(streams.out)
        assert exited.value.code == 1
        assert streams.err == ""
        assert result["status"] == "no_go"
        assert result[f"{changed_authority}_matches_baseline"] is False
        assert result["data_reversible"] is False
        assert result["cutover_stage_provenance"] == "caller_asserted_unverified"
        assert result["service_and_client_state_requires_external_verification"] is True
        actions = "\n".join(result["actions"])
        assert "start the legacy" not in actions
        assert "restore client" not in actions
    finally:
        connection.close()


def test_isolated_cold_restore_rehearsal_retains_four_raw_states_and_reverifies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, manifest = _isolated_rehearsal_inputs(tmp_path)
    repository, candidate_commit = _rehearsal_candidate(tmp_path)
    provenance = _rehearsal_provenance(tmp_path, source.database)
    run_directory = tmp_path / "rehearsal-evidence"
    monkeypatch.setattr(migration, "_assert_production_shaped_scale", lambda _scale: None)
    result = rehearse_cold_restore(
        source.database,
        Path("/production/mcp-agent-mail/storage.sqlite3"),
        run_directory,
        manifest,
        repository,
        provenance,
        **_generator_receipt_arguments(provenance),
        candidate_commit=candidate_commit,
    )

    receipt_path = run_directory / COLD_REHEARSAL_RECEIPT_NAME
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert result["status"] == "completed"
    assert receipt["run_id"] == result["run_id"]
    assert receipt["candidate_commit"] == candidate_commit
    assert receipt["mode"] == "isolated_rehearsal"
    assert receipt["production_source"]["used"] is False
    assert set(receipt["artifacts"]) == {"source", "backup", "damaged", "restored"}
    assert receipt["damage"]["plan"] == COLD_REHEARSAL_DAMAGE_PLAN
    assert receipt["damage"]["damage_assertion_passed"] is True
    assert receipt["damage"]["created_absent_sidecars"] == ["wal", "shm"]
    assert receipt["seed"]["scale"]["major_table_rows"] == {
        "agents": 2,
        "file_reservations": 1,
        "message_recipients": 1,
        "messages": 1,
        "projects": 1,
    }
    marker = json.loads(
        (run_directory / COLD_REHEARSAL_MARKER_NAME).read_text(encoding="utf-8")
    )
    assert marker["phase"] == "TERMINAL_RECEIPT_PREPARED_OR_PUBLISHED"
    assert marker["run_id"] == receipt["run_id"]
    assert marker["candidate_commit"] == receipt["candidate_commit"]
    assert not list(run_directory.glob("*.prepared"))
    assert not list(run_directory.glob("*.unconfirmed"))
    verified = verify_cold_restore_rehearsal(
        receipt_path,
        run_directory / COLD_REHEARSAL_VERIFICATION_NAME,
        expected_receipt_sha256=result["rehearsal_receipt_sha256"],
        expected_run_id=result["run_id"],
        expected_candidate_commit=candidate_commit,
    )
    assert verified["status"] == "verified"
    assert verified["run_id"] == result["run_id"]
    assert verified["raw_artifact_count"] == 4


def test_isolated_rehearsal_marker_write_failure_removes_empty_run_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, manifest = _isolated_rehearsal_inputs(tmp_path)
    repository, candidate_commit = _rehearsal_candidate(tmp_path)
    provenance = _rehearsal_provenance(tmp_path, source.database)
    run_directory = tmp_path / "marker-failure-evidence"
    original_write = migration._write_json_exclusive

    def fail_initial_marker(path: Path, payload: dict[str, object]) -> None:
        if path.name == COLD_REHEARSAL_MARKER_NAME:
            raise OSError(errno.EIO, "injected rehearsal marker failure")
        original_write(path, payload)

    monkeypatch.setattr(migration, "_write_json_exclusive", fail_initial_marker)
    with pytest.raises(OSError, match="rehearsal marker failure"):
        rehearse_cold_restore(
            source.database,
            Path("/production/mcp-agent-mail/storage.sqlite3"),
            run_directory,
            manifest,
            repository,
            provenance,
            **_generator_receipt_arguments(provenance),
            candidate_commit=candidate_commit,
        )
    assert not run_directory.exists()


def test_isolated_rehearsal_terminal_marker_failure_retains_prepared_incident(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, manifest = _isolated_rehearsal_inputs(tmp_path)
    repository, candidate_commit = _rehearsal_candidate(tmp_path)
    provenance = _rehearsal_provenance(tmp_path, source.database)
    run_directory = tmp_path / "terminal-marker-failure-evidence"
    monkeypatch.setattr(migration, "_assert_production_shaped_scale", lambda _scale: None)
    original_replace = migration._replace_json_fsynced

    def fail_terminal_marker(path: Path, payload: dict[str, object]) -> None:
        if payload.get("phase") == "TERMINAL_RECEIPT_PREPARED_OR_PUBLISHED":
            raise OSError(errno.EIO, "injected terminal marker failure")
        original_replace(path, payload)

    monkeypatch.setattr(migration, "_replace_json_fsynced", fail_terminal_marker)
    with pytest.raises(OSError, match="terminal marker failure"):
        rehearse_cold_restore(
            source.database,
            Path("/production/mcp-agent-mail/storage.sqlite3"),
            run_directory,
            manifest,
            repository,
            provenance,
            **_generator_receipt_arguments(provenance),
            candidate_commit=candidate_commit,
        )
    assert (run_directory / COLD_REHEARSAL_MARKER_NAME).is_file()
    assert not (run_directory / COLD_REHEARSAL_RECEIPT_NAME).exists()
    prepared = list(run_directory.glob("*.prepared"))
    assert len(prepared) == 1
    assert json.loads(prepared[0].read_text(encoding="utf-8"))["status"] == "completed"


def test_isolated_rehearsal_publish_fsync_failure_quarantines_terminal_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, manifest = _isolated_rehearsal_inputs(tmp_path)
    repository, candidate_commit = _rehearsal_candidate(tmp_path)
    provenance = _rehearsal_provenance(tmp_path, source.database)
    run_directory = tmp_path / "publish-fsync-failure-evidence"
    monkeypatch.setattr(migration, "_assert_production_shaped_scale", lambda _scale: None)
    original_fsync = migration._fsync_directory
    injected = False

    def fail_terminal_parent_fsync(path: Path) -> None:
        nonlocal injected
        if (
            path == run_directory
            and (run_directory / COLD_REHEARSAL_RECEIPT_NAME).exists()
            and not injected
        ):
            injected = True
            raise OSError(errno.EIO, "injected rehearsal receipt parent fsync failure")
        original_fsync(path)

    monkeypatch.setattr(migration, "_fsync_directory", fail_terminal_parent_fsync)
    with pytest.raises(OSError, match="receipt parent fsync failure"):
        rehearse_cold_restore(
            source.database,
            Path("/production/mcp-agent-mail/storage.sqlite3"),
            run_directory,
            manifest,
            repository,
            provenance,
            **_generator_receipt_arguments(provenance),
            candidate_commit=candidate_commit,
        )
    assert injected is True
    assert not (run_directory / COLD_REHEARSAL_RECEIPT_NAME).exists()
    assert not list(run_directory.glob("*.prepared"))
    unconfirmed = list(run_directory.glob("*.unconfirmed"))
    assert len(unconfirmed) == 1
    assert json.loads(unconfirmed[0].read_text(encoding="utf-8"))["status"] == "completed"


def test_isolated_rehearsal_rejects_unrelated_clean_candidate_repository(
    tmp_path: Path,
) -> None:
    source, manifest = _isolated_rehearsal_inputs(tmp_path)
    _rehearsal_candidate(tmp_path)
    provenance = _rehearsal_provenance(tmp_path, source.database)
    repository = tmp_path / "unrelated-candidate"
    repository.mkdir()
    subprocess.run(["git", "init", "-q", str(repository)], check=True)
    subprocess.run(
        ["git", "-C", str(repository), "config", "user.name", "Candidate Test"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repository), "config", "user.email", "candidate@example.test"],
        check=True,
    )
    (repository / "candidate.txt").write_text("unrelated\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repository), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(repository), "commit", "-q", "-m", "unrelated"],
        check=True,
    )
    candidate_commit = subprocess.run(
        ["git", "-C", str(repository), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    with pytest.raises(MigrationError, match="executing migration.py bytes"):
        rehearse_cold_restore(
            source.database,
            Path("/production/mcp-agent-mail/storage.sqlite3"),
            tmp_path / "unrelated-candidate-evidence",
            manifest,
            repository,
            provenance,
            **_generator_receipt_arguments(provenance),
            candidate_commit=candidate_commit,
        )


def test_isolated_rehearsal_rejects_candidate_module_blob_mismatch(
    tmp_path: Path,
) -> None:
    source, manifest = _isolated_rehearsal_inputs(tmp_path)
    repository, _ = _rehearsal_candidate(tmp_path)
    provenance = _rehearsal_provenance(tmp_path, source.database)
    module_path = (
        repository
        / "packages"
        / "agentstack_mail"
        / "src"
        / "agentstack_mail"
        / "migration.py"
    )
    module_path.write_text("# wrong candidate module\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repository), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(repository), "commit", "-q", "-m", "wrong-module"],
        check=True,
    )
    candidate_commit = subprocess.run(
        ["git", "-C", str(repository), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    with pytest.raises(MigrationError, match="executing migration.py bytes"):
        rehearse_cold_restore(
            source.database,
            Path("/production/mcp-agent-mail/storage.sqlite3"),
            tmp_path / "wrong-module-evidence",
            manifest,
            repository,
            provenance,
            **_generator_receipt_arguments(provenance),
            candidate_commit=candidate_commit,
        )


def test_isolated_rehearsal_rejects_unauthenticated_clone_provenance(
    tmp_path: Path,
) -> None:
    source, manifest = _isolated_rehearsal_inputs(tmp_path)
    repository, candidate_commit = _rehearsal_candidate(tmp_path)
    provenance = _rehearsal_provenance(tmp_path, source.database)
    payload = json.loads(provenance.read_text(encoding="utf-8"))
    payload["kind"] = "production-read-only-clone"
    payload["acquisition_method"] = "caller assertion only"
    provenance.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    run_directory = tmp_path / "untrusted-clone-evidence"

    with pytest.raises(MigrationError, match="provenance kind is invalid"):
        rehearse_cold_restore(
            source.database,
            Path("/production/mcp-agent-mail/storage.sqlite3"),
            run_directory,
            manifest,
            repository,
            provenance,
            **_generator_receipt_arguments(provenance),
            candidate_commit=candidate_commit,
        )
    assert not run_directory.exists()


def test_isolated_rehearsal_rejects_valid_seed_mutation_after_generator_receipt(
    tmp_path: Path,
) -> None:
    source, manifest = _isolated_rehearsal_inputs(tmp_path)
    repository, candidate_commit = _rehearsal_candidate(tmp_path)
    provenance = _rehearsal_provenance(tmp_path, source.database)
    generator_arguments = _generator_receipt_arguments(provenance)
    connection = sqlite3.connect(source.database)
    try:
        connection.execute("UPDATE messages SET subject='post-generator mutation'")
        connection.commit()
    finally:
        connection.close()
    run_directory = tmp_path / "mutated-seed-evidence"

    with pytest.raises(VerificationError, match="seed changed after generator"):
        rehearse_cold_restore(
            source.database,
            Path("/production/mcp-agent-mail/storage.sqlite3"),
            run_directory,
            manifest,
            repository,
            provenance,
            **generator_arguments,
            candidate_commit=candidate_commit,
        )
    assert not run_directory.exists()


@pytest.mark.parametrize("surface", ["archive", "signals"])
def test_isolated_rehearsal_rejects_seed_tree_mutation_after_generator_receipt(
    tmp_path: Path, surface: str
) -> None:
    source, manifest = _isolated_rehearsal_inputs(tmp_path)
    repository, candidate_commit = _rehearsal_candidate(tmp_path)
    provenance = _rehearsal_provenance(tmp_path, source.database)
    generator_arguments = _generator_receipt_arguments(provenance)
    changed_root = getattr(source, surface)
    changed_root.mkdir(parents=True, exist_ok=True)
    (changed_root / "post-generator-mutation.txt").write_text(
        "not generator-bound\n", encoding="utf-8"
    )
    run_directory = tmp_path / f"mutated-{surface}-evidence"

    with pytest.raises(
        VerificationError, match="archive or signals changed after generator"
    ):
        rehearse_cold_restore(
            source.database,
            Path("/production/mcp-agent-mail/storage.sqlite3"),
            run_directory,
            manifest,
            repository,
            provenance,
            **generator_arguments,
            candidate_commit=candidate_commit,
        )
    assert not run_directory.exists()


def test_rehearsal_verifier_rejects_timestamp_and_sidecar_mutants(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_directory, _result, _repository, _candidate = (
        _completed_rehearsal_for_verification(tmp_path, monkeypatch)
    )
    receipt_path = run_directory / COLD_REHEARSAL_RECEIPT_NAME
    original = json.loads(receipt_path.read_text(encoding="utf-8"))

    timestamp_mutants = (
        {"started_at": "2026-08-11T09:00:00+09:00"},
        {
            "started_at": "2026-08-11T00:00:01+00:00",
            "completed_at": "2026-08-11T00:00:00+00:00",
        },
    )
    for changes in timestamp_mutants:
        mutant = json.loads(json.dumps(original))
        mutant.update(changes)
        with pytest.raises(VerificationError):
            migration._verify_cold_rehearsal_payload(
                mutant, receipt_path=receipt_path
            )

    for sidecars in (["wal", "wal", "shm"], ["wal"]):
        mutant = json.loads(json.dumps(original))
        mutant["damage"]["created_absent_sidecars"] = sidecars
        with pytest.raises(VerificationError, match="damage evidence is malformed"):
            migration._verify_cold_rehearsal_payload(
                mutant, receipt_path=receipt_path
            )


def test_rehearsal_verifier_rejects_extra_raw_and_run_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_directory, _result, _repository, _candidate = (
        _completed_rehearsal_for_verification(tmp_path, monkeypatch)
    )
    receipt_path = run_directory / COLD_REHEARSAL_RECEIPT_NAME
    payload = json.loads(receipt_path.read_text(encoding="utf-8"))

    run_extra = run_directory / "unexpected-run-entry"
    run_extra.write_text("unexpected\n", encoding="utf-8")
    with pytest.raises(VerificationError, match="run root exact allowlist"):
        migration._verify_cold_rehearsal_payload(payload, receipt_path=receipt_path)
    run_extra.unlink()

    raw_extra = run_directory / "raw" / "unexpected-raw-entry"
    raw_extra.write_text("unexpected\n", encoding="utf-8")
    with pytest.raises(VerificationError, match="raw root exact allowlist"):
        migration._verify_cold_rehearsal_payload(payload, receipt_path=receipt_path)


@pytest.mark.parametrize("identity_name", ("cold_backup_receipt", "cold_restore_receipt"))
def test_rehearsal_verifier_rejects_external_copied_identity_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    identity_name: str,
) -> None:
    run_directory, _result, _repository, _candidate = (
        _completed_rehearsal_for_verification(tmp_path, monkeypatch)
    )
    receipt_path = run_directory / COLD_REHEARSAL_RECEIPT_NAME
    payload = json.loads(receipt_path.read_text(encoding="utf-8"))
    external = tmp_path / f"external-{identity_name}.json"
    shutil.copy2(Path(payload["identities"][identity_name]["path"]), external)
    payload["identities"][identity_name]["path"] = str(external)

    with pytest.raises(VerificationError, match="canonical rehearsal path"):
        migration._verify_cold_rehearsal_payload(payload, receipt_path=receipt_path)


def test_rehearsal_verifier_requires_correct_out_of_band_pins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_directory, result, _repository, candidate_commit = (
        _completed_rehearsal_for_verification(tmp_path, monkeypatch)
    )
    receipt_path = run_directory / COLD_REHEARSAL_RECEIPT_NAME
    verification_receipt = run_directory / COLD_REHEARSAL_VERIFICATION_NAME
    correct = {
        "expected_receipt_sha256": result["rehearsal_receipt_sha256"],
        "expected_run_id": result["run_id"],
        "expected_candidate_commit": candidate_commit,
    }
    wrong_cases = (
        {**correct, "expected_receipt_sha256": "0" * 64},
        {**correct, "expected_run_id": "00000000-0000-4000-8000-000000000000"},
        {**correct, "expected_candidate_commit": "1" * 40},
    )
    for pins in wrong_cases:
        with pytest.raises(VerificationError):
            verify_cold_restore_rehearsal(
                receipt_path,
                verification_receipt,
                **pins,
            )
        assert not verification_receipt.exists()


def test_rehearsal_verifier_detects_receipt_change_during_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_directory, result, _repository, candidate_commit = (
        _completed_rehearsal_for_verification(tmp_path, monkeypatch)
    )
    receipt_path = run_directory / COLD_REHEARSAL_RECEIPT_NAME
    verification_receipt = run_directory / COLD_REHEARSAL_VERIFICATION_NAME
    original_verify = migration._verify_cold_rehearsal_payload

    def mutate_after_recompute(
        payload: object, *, receipt_path: Path | None
    ) -> dict[str, object]:
        verified = original_verify(payload, receipt_path=receipt_path)
        assert receipt_path is not None
        receipt_path.write_text(
            receipt_path.read_text(encoding="utf-8") + " ", encoding="utf-8"
        )
        return verified

    monkeypatch.setattr(
        migration, "_verify_cold_rehearsal_payload", mutate_after_recompute
    )
    with pytest.raises(VerificationError, match="changed during independent verification"):
        verify_cold_restore_rehearsal(
            receipt_path,
            verification_receipt,
            expected_receipt_sha256=result["rehearsal_receipt_sha256"],
            expected_run_id=result["run_id"],
            expected_candidate_commit=candidate_commit,
        )
    assert not verification_receipt.exists()


def test_rehearsal_verifier_postpublish_read_failure_quarantines_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_directory, rehearsal, _repository, candidate_commit = (
        _completed_rehearsal_for_verification(tmp_path, monkeypatch)
    )
    receipt = run_directory / COLD_REHEARSAL_RECEIPT_NAME
    verification_receipt = run_directory / COLD_REHEARSAL_VERIFICATION_NAME
    original_fingerprint = migration._fingerprint_regular_file
    injected = False

    def fail_published_verifier_read(
        path: Path, *, required: bool
    ) -> dict[str, object] | None:
        nonlocal injected
        if path == verification_receipt and path.exists() and not injected:
            injected = True
            raise OSError(errno.EIO, "injected verifier postpublish read failure")
        return original_fingerprint(path, required=required)

    monkeypatch.setattr(
        migration, "_fingerprint_regular_file", fail_published_verifier_read
    )
    with pytest.raises(OSError, match="verifier postpublish read failure"):
        verify_cold_restore_rehearsal(
            receipt,
            verification_receipt,
            expected_receipt_sha256=rehearsal["rehearsal_receipt_sha256"],
            expected_run_id=rehearsal["run_id"],
            expected_candidate_commit=candidate_commit,
        )
    assert injected is True
    assert not verification_receipt.exists()
    unconfirmed = list(
        run_directory.glob(
            ".cold-restore-rehearsal-verification.json."
            "cold-restore-rehearsal-verification-*.unconfirmed"
        )
    )
    assert len(unconfirmed) == 1
    assert json.loads(unconfirmed[0].read_text(encoding="utf-8"))["kind"] == (
        "cold-restore-rehearsal-verification"
    )


def test_rehearsal_check_only_reverifies_twice_without_mutating_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_directory, result, _repository, candidate_commit = (
        _completed_rehearsal_for_verification(tmp_path, monkeypatch)
    )
    receipt_path = run_directory / COLD_REHEARSAL_RECEIPT_NAME
    verification_receipt = run_directory / COLD_REHEARSAL_VERIFICATION_NAME
    first = verify_cold_restore_rehearsal(
        receipt_path,
        verification_receipt,
        expected_receipt_sha256=result["rehearsal_receipt_sha256"],
        expected_run_id=result["run_id"],
        expected_candidate_commit=candidate_commit,
    )

    def tree_identity() -> dict[str, tuple[int, int, int, int, bytes | None]]:
        identity: dict[str, tuple[int, int, int, int, bytes | None]] = {}
        for path in sorted(run_directory.rglob("*")):
            info = path.lstat()
            identity[str(path.relative_to(run_directory))] = (
                info.st_ino,
                info.st_mode,
                info.st_size,
                info.st_mtime_ns,
                path.read_bytes() if path.is_file() else None,
            )
        return identity

    before = tree_identity()
    for _ in range(2):
        checked = check_cold_restore_rehearsal_verification(
            receipt_path,
            verification_receipt,
            expected_receipt_sha256=result["rehearsal_receipt_sha256"],
            expected_verification_receipt_sha256=(
                first["verification_receipt_sha256"]
            ),
            expected_run_id=result["run_id"],
            expected_candidate_commit=candidate_commit,
        )
        assert checked["status"] == "verified_check_only"
        assert checked["verification_receipt_sha256"] == first[
            "verification_receipt_sha256"
        ]
    assert tree_identity() == before


@pytest.mark.parametrize("artifact_state", ("source", "backup", "damaged", "restored"))
def test_rehearsal_check_only_rejects_each_raw_mutation_after_canonical_verify(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    artifact_state: str,
) -> None:
    run_directory, result, _repository, candidate_commit = (
        _completed_rehearsal_for_verification(tmp_path, monkeypatch)
    )
    receipt_path = run_directory / COLD_REHEARSAL_RECEIPT_NAME
    verification_receipt = run_directory / COLD_REHEARSAL_VERIFICATION_NAME
    first = verify_cold_restore_rehearsal(
        receipt_path,
        verification_receipt,
        expected_receipt_sha256=result["rehearsal_receipt_sha256"],
        expected_run_id=result["run_id"],
        expected_candidate_commit=candidate_commit,
    )
    artifact_main = (
        run_directory / "backup" / "storage.sqlite3"
        if artifact_state == "backup"
        else run_directory / "raw" / artifact_state / "storage.sqlite3"
    )
    with artifact_main.open("r+b") as stream:
        stream.seek(0)
        first_byte = stream.read(1)
        stream.seek(0)
        stream.write(bytes([first_byte[0] ^ 0xFF]))

    with pytest.raises(
        VerificationError, match=f"{artifact_state} main raw artifact changed"
    ):
        check_cold_restore_rehearsal_verification(
            receipt_path,
            verification_receipt,
            expected_receipt_sha256=result["rehearsal_receipt_sha256"],
            expected_verification_receipt_sha256=(
                first["verification_receipt_sha256"]
            ),
            expected_run_id=result["run_id"],
            expected_candidate_commit=candidate_commit,
        )


def test_rehearsal_cli_routes_initial_verify_and_read_only_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source, manifest = _isolated_rehearsal_inputs(tmp_path)
    repository, candidate_commit = _rehearsal_candidate(tmp_path)
    provenance = _rehearsal_provenance(tmp_path, source.database)
    generator_receipt, generator_sha256 = _generator_receipt_evidence(provenance)
    run_directory = tmp_path / "cli-rehearsal"
    monkeypatch.setattr(migration, "_assert_production_shaped_scale", lambda _scale: None)
    main(
        [
            "cold-restore-rehearse",
            "--seed-db",
            str(source.database),
            "--production-source-db",
            "/production/mcp-agent-mail/storage.sqlite3",
            "--run-dir",
            str(run_directory),
            "--migration-manifest",
            str(manifest),
            "--candidate-repo",
            str(repository),
            "--candidate-commit",
            candidate_commit,
            "--seed-provenance",
            str(provenance),
            "--generator-receipt",
            str(generator_receipt),
            "--expected-generator-receipt-sha256",
            generator_sha256,
        ]
    )
    rehearsal_streams = capsys.readouterr()
    rehearsal = json.loads(rehearsal_streams.out)
    assert rehearsal_streams.err == ""
    assert rehearsal["status"] == "completed"

    receipt_path = run_directory / COLD_REHEARSAL_RECEIPT_NAME
    verification_receipt = run_directory / COLD_REHEARSAL_VERIFICATION_NAME
    common = [
        "--receipt",
        str(receipt_path),
        "--verification-receipt",
        str(verification_receipt),
        "--expected-receipt-sha256",
        rehearsal["rehearsal_receipt_sha256"],
        "--expected-run-id",
        rehearsal["run_id"],
        "--expected-candidate-commit",
        candidate_commit,
    ]

    with pytest.raises(SystemExit) as missing_pin:
        main(["cold-restore-rehearsal-verify", *common, "--check-only"])
    missing_streams = capsys.readouterr()
    assert missing_pin.value.code == 1
    assert missing_streams.out == ""
    assert "required with --check-only" in missing_streams.err
    assert len(missing_streams.err.splitlines()) == 1

    with pytest.raises(SystemExit) as pin_without_mode:
        main(
            [
                "cold-restore-rehearsal-verify",
                *common,
                "--expected-verification-receipt-sha256",
                "1" * 64,
            ]
        )
    extra_streams = capsys.readouterr()
    assert pin_without_mode.value.code == 1
    assert extra_streams.out == ""
    assert "requires --check-only" in extra_streams.err
    assert len(extra_streams.err.splitlines()) == 1

    main(["cold-restore-rehearsal-verify", *common])
    verification_streams = capsys.readouterr()
    verification = json.loads(verification_streams.out)
    assert verification_streams.err == ""
    assert verification["status"] == "verified"

    main(
        [
            "cold-restore-rehearsal-verify",
            *common,
            "--expected-verification-receipt-sha256",
            verification["verification_receipt_sha256"],
            "--check-only",
        ]
    )
    check_streams = capsys.readouterr()
    checked = json.loads(check_streams.out)
    assert check_streams.err == ""
    assert checked["status"] == "verified_check_only"


def test_isolated_rehearsal_rejects_noop_damage_before_restore(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, manifest = _isolated_rehearsal_inputs(tmp_path)
    repository, candidate_commit = _rehearsal_candidate(tmp_path)
    provenance = _rehearsal_provenance(tmp_path, source.database)
    run_directory = tmp_path / "noop-evidence"
    monkeypatch.setattr(migration, "_assert_production_shaped_scale", lambda _scale: None)

    def noop_damage(
        paths: dict[str, Path], _records: dict[str, dict[str, object]]
    ) -> dict[str, object]:
        observed = migration._database_family_fingerprints(paths)
        return {
            "plan": COLD_REHEARSAL_DAMAGE_PLAN,
            "main_action": "noop-mutant",
            "created_absent_sidecars": [],
            "observed_before_physical": observed,
            "observed_after_physical": observed,
        }

    monkeypatch.setattr(migration, "_damage_rehearsal_target", noop_damage)
    with pytest.raises(VerificationError, match="damage was a no-op"):
        rehearse_cold_restore(
            source.database,
            Path("/production/mcp-agent-mail/storage.sqlite3"),
            run_directory,
            manifest,
            repository,
            provenance,
            **_generator_receipt_arguments(provenance),
            candidate_commit=candidate_commit,
        )
    assert not (run_directory / COLD_REHEARSAL_RECEIPT_NAME).exists()
    assert (run_directory / COLD_REHEARSAL_MARKER_NAME).is_file()


def test_isolated_rehearsal_rejects_prediverged_target_plus_noop_damage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, manifest = _isolated_rehearsal_inputs(tmp_path)
    repository, candidate_commit = _rehearsal_candidate(tmp_path)
    provenance = _rehearsal_provenance(tmp_path, source.database)
    run_directory = tmp_path / "prediverged-noop-evidence"
    monkeypatch.setattr(migration, "_assert_production_shaped_scale", lambda _scale: None)
    original_copy = migration._copy_database_family_artifact

    def copy_then_prediverge(
        source_paths: dict[str, Path], destination: Path
    ) -> dict[str, object]:
        descriptor = original_copy(source_paths, destination)
        if destination.name == "working-target":
            target = Path(descriptor["files"]["main"]["path"])
            connection = sqlite3.connect(target)
            try:
                connection.execute("UPDATE messages SET subject='pre-existing divergence'")
                connection.commit()
            finally:
                connection.close()
        return descriptor

    def observed_noop(
        paths: dict[str, Path], _records: dict[str, dict[str, object]]
    ) -> dict[str, object]:
        observed = migration._database_family_fingerprints(paths)
        return {
            "plan": COLD_REHEARSAL_DAMAGE_PLAN,
            "main_action": "noop-mutant",
            "created_absent_sidecars": [],
            "observed_before_physical": observed,
            "observed_after_physical": observed,
        }

    monkeypatch.setattr(migration, "_copy_database_family_artifact", copy_then_prediverge)
    monkeypatch.setattr(migration, "_damage_rehearsal_target", observed_noop)
    with pytest.raises(VerificationError, match="did not bind its own before/after state"):
        rehearse_cold_restore(
            source.database,
            Path("/production/mcp-agent-mail/storage.sqlite3"),
            run_directory,
            manifest,
            repository,
            provenance,
            **_generator_receipt_arguments(provenance),
            candidate_commit=candidate_commit,
        )
    assert not (run_directory / COLD_REHEARSAL_RECEIPT_NAME).exists()


def test_isolated_rehearsal_rejects_tiny_seed_as_release_evidence(
    tmp_path: Path,
) -> None:
    source, manifest = _isolated_rehearsal_inputs(tmp_path)
    repository, candidate_commit = _rehearsal_candidate(tmp_path)
    provenance = _rehearsal_provenance(tmp_path, source.database)
    run_directory = tmp_path / "tiny-seed-evidence"
    with pytest.raises(VerificationError, match="production-shaped scale floor"):
        rehearse_cold_restore(
            source.database,
            Path("/production/mcp-agent-mail/storage.sqlite3"),
            run_directory,
            manifest,
            repository,
            provenance,
            **_generator_receipt_arguments(provenance),
            candidate_commit=candidate_commit,
        )
    assert not (run_directory / COLD_REHEARSAL_RECEIPT_NAME).exists()


def test_isolated_rehearsal_detects_restore_call_skip_mutant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, manifest = _isolated_rehearsal_inputs(tmp_path)
    repository, candidate_commit = _rehearsal_candidate(tmp_path)
    provenance = _rehearsal_provenance(tmp_path, source.database)
    run_directory = tmp_path / "restore-skip-evidence"
    monkeypatch.setattr(migration, "_assert_production_shaped_scale", lambda _scale: None)

    def skip_restore(*_args: object, **_kwargs: object) -> dict[str, object]:
        return {"status": "restored", "operation_id": "mutant"}

    monkeypatch.setattr(migration, "cold_restore_database", skip_restore)
    with pytest.raises(VerificationError, match="did not recover the seed logically"):
        rehearse_cold_restore(
            source.database,
            Path("/production/mcp-agent-mail/storage.sqlite3"),
            run_directory,
            manifest,
        repository,
        provenance,
        **_generator_receipt_arguments(provenance),
        candidate_commit=candidate_commit,
        )
    assert not (run_directory / COLD_REHEARSAL_RECEIPT_NAME).exists()


@pytest.mark.parametrize("skipped_branch", ("present-main", "absent-wal"))
def test_cold_restore_raw_validator_detects_replace_or_remove_skip_mutant(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    skipped_branch: str,
) -> None:
    source, connection = _source(tmp_path)
    connection.close()
    backup = tmp_path / "sealed-backup"
    cold_backup_database(source.database, backup, services_stopped=True)
    manifest = _cold_restore_manifest(source, tmp_path)
    destination = tmp_path / "target" / "storage.sqlite3"
    _copy_cold_family_to_target(backup, destination)
    destination.write_bytes(b"corrupt-main")
    Path(f"{destination}-wal").write_bytes(b"unexpected-wal")
    Path(f"{destination}-shm").write_bytes(b"unexpected-shm")
    receipt = tmp_path / "restore.json"
    original_replace = migration.os.replace

    def skip_one(source_path: Path | str, target_path: Path | str) -> None:
        source_candidate = Path(source_path)
        target_candidate = Path(target_path)
        if skipped_branch == "present-main" and target_candidate == destination:
            return
        if (
            skipped_branch == "absent-wal"
            and source_candidate == Path(f"{destination}-wal")
            and target_candidate.name == "quarantine-wal"
        ):
            return
        original_replace(source_candidate, target_candidate)

    monkeypatch.setattr(migration.os, "replace", skip_one)
    with pytest.raises(VerificationError):
        cold_restore_database(
            backup,
            destination,
            receipt,
            manifest,
            services_stopped=True,
            target_kind="rehearsal-copy",
            fault_injection=COLD_REHEARSAL_DAMAGE_PLAN,
        )
    assert not receipt.exists()


def test_cold_restore_marker_write_failure_leaves_no_empty_staging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, connection = _source(tmp_path)
    connection.close()
    backup = tmp_path / "sealed-backup"
    cold_backup_database(source.database, backup, services_stopped=True)
    manifest = _cold_restore_manifest(source, tmp_path)
    destination = tmp_path / "target.sqlite3"
    destination.write_bytes(b"corrupt")
    receipt = tmp_path / "restore.json"
    original_write = migration._write_json_exclusive

    def fail_marker(path: Path, payload: dict[str, object]) -> None:
        if path.name == COLD_RESTORE_MARKER_NAME:
            raise OSError("injected marker write failure")
        original_write(path, payload)

    monkeypatch.setattr(migration, "_write_json_exclusive", fail_marker)
    with pytest.raises(OSError, match="marker write failure"):
        cold_restore_database(
            backup,
            destination,
            receipt,
            manifest,
            services_stopped=True,
            target_kind="rehearsal-copy",
            fault_injection="truncate-main",
        )
    assert not receipt.exists()
    assert not list(tmp_path.glob(".target.sqlite3.cold-restore-*"))


def test_cold_restore_target_parent_fsync_failure_retains_incident_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, connection = _source(tmp_path)
    connection.close()
    backup = tmp_path / "sealed-backup"
    cold_backup_database(source.database, backup, services_stopped=True)
    manifest = _cold_restore_manifest(source, tmp_path)
    destination = tmp_path / "target.sqlite3"
    destination.write_bytes(b"corrupt")
    Path(f"{destination}-wal").write_bytes(b"unexpected")
    receipt = tmp_path / "restore.json"
    original_fsync_directory = migration._fsync_directory
    injected = False

    def fail_target_parent(path: Path) -> None:
        nonlocal injected
        staging = list(tmp_path.glob(".target.sqlite3.cold-restore-*"))
        mutation_marker = False
        if staging:
            marker_path = staging[0] / COLD_RESTORE_MARKER_NAME
            if marker_path.is_file():
                marker_payload = json.loads(marker_path.read_text(encoding="utf-8"))
                mutation_marker = marker_payload["phase"] == "TARGET_MUTATION_MAY_HAVE_STARTED"
        if path == destination.parent and mutation_marker and not injected:
            injected = True
            raise OSError(errno.EIO, "injected target-parent fsync failure")
        original_fsync_directory(path)

    monkeypatch.setattr(migration, "_fsync_directory", fail_target_parent)
    with pytest.raises(OSError, match="target-parent fsync failure"):
        cold_restore_database(
            backup,
            destination,
            receipt,
            manifest,
            services_stopped=True,
            target_kind="rehearsal-copy",
            fault_injection="corrupt-main-and-sidecar",
        )
    assert injected is True
    assert not receipt.exists()
    staging = next(iter(tmp_path.glob(".target.sqlite3.cold-restore-*")))
    marker = json.loads(
        (staging / COLD_RESTORE_MARKER_NAME).read_text(encoding="utf-8")
    )
    assert marker["phase"] == "TARGET_MUTATION_MAY_HAVE_STARTED"


@pytest.mark.parametrize(
    "seam", ["staged-file", "prepared-receipt", "absent-parent", "present-main-parent"]
)
def test_cold_restore_eio_matrix_leaves_no_canonical_success_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, seam: str
) -> None:
    source, connection = _source(tmp_path)
    connection.close()
    backup = tmp_path / "sealed-backup"
    cold_backup_database(source.database, backup, services_stopped=True)
    manifest = _cold_restore_manifest(source, tmp_path)
    destination = tmp_path / "target.sqlite3"
    destination.write_bytes(b"corrupt")
    Path(f"{destination}-wal").write_bytes(b"unexpected-wal")
    Path(f"{destination}-shm").write_bytes(b"unexpected-shm")
    receipt = tmp_path / "restore.json"
    injected = False
    original_fsync = migration.os.fsync
    original_fsync_directory = migration._fsync_directory

    if seam in {"staged-file", "prepared-receipt"}:

        def fail_file_fsync(descriptor: int) -> None:
            nonlocal injected
            staging = list(tmp_path.glob(".target.sqlite3.cold-restore-*"))
            if seam == "staged-file":
                target = (
                    staging[0] / "storage.sqlite3"
                    if staging
                    else tmp_path / "missing"
                )
            else:
                prepared = list(tmp_path.glob(".restore.json.cold-restore-*.prepared"))
                target = prepared[0] if prepared else tmp_path / "missing"
            if not injected and _descriptor_names_path(descriptor, target):
                injected = True
                raise OSError(errno.EIO, f"injected restore {seam} fsync failure")
            original_fsync(descriptor)

        monkeypatch.setattr(migration.os, "fsync", fail_file_fsync)
    else:
        backup_main = (backup / "storage.sqlite3").read_bytes()

        def fail_branch_parent(path: Path) -> None:
            nonlocal injected
            if path != destination.parent or injected:
                original_fsync_directory(path)
                return
            wal_absent = not Path(f"{destination}-wal").exists()
            shm_present = Path(f"{destination}-shm").exists()
            main_restored = destination.read_bytes() == backup_main
            should_fail = (
                seam == "absent-parent" and wal_absent and shm_present
            ) or (seam == "present-main-parent" and not shm_present and main_restored)
            if should_fail:
                injected = True
                raise OSError(errno.EIO, f"injected restore {seam} fsync failure")
            original_fsync_directory(path)

        monkeypatch.setattr(migration, "_fsync_directory", fail_branch_parent)

    with pytest.raises(OSError, match=f"restore {seam} fsync failure"):
        cold_restore_database(
            backup,
            destination,
            receipt,
            manifest,
            services_stopped=True,
            target_kind="rehearsal-copy",
            fault_injection="corrupt-main-and-create-sidecars",
        )
    assert injected is True
    assert not receipt.exists()
    assert not list(tmp_path.glob(".restore.json.cold-restore-*.unconfirmed"))
    staging = list(tmp_path.glob(".target.sqlite3.cold-restore-*"))
    if seam == "staged-file":
        assert staging == []
    else:
        assert len(staging) == 1
        marker = json.loads(
            (staging[0] / COLD_RESTORE_MARKER_NAME).read_text(encoding="utf-8")
        )
        expected_phase = (
            "TARGET_VALIDATED_AWAITING_RECEIPT_PUBLICATION"
            if seam == "prepared-receipt"
            else "TARGET_MUTATION_MAY_HAVE_STARTED"
        )
        assert marker["phase"] == expected_phase


def test_cold_restore_final_receipt_fsync_failure_quarantines_success_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, connection = _source(tmp_path)
    connection.close()
    backup = tmp_path / "sealed-backup"
    cold_backup_database(source.database, backup, services_stopped=True)
    manifest = _cold_restore_manifest(source, tmp_path)
    destination = tmp_path / "target.sqlite3"
    destination.write_bytes(b"corrupt")
    receipt = tmp_path / "restore.json"
    original_fsync_directory = migration._fsync_directory
    injected = False

    def fail_after_terminal_publish(path: Path) -> None:
        nonlocal injected
        if path == receipt.parent and receipt.exists() and not injected:
            injected = True
            raise OSError(errno.EIO, "injected terminal parent fsync failure")
        original_fsync_directory(path)

    monkeypatch.setattr(migration, "_fsync_directory", fail_after_terminal_publish)
    with pytest.raises(OSError, match="terminal parent fsync failure"):
        cold_restore_database(
            backup,
            destination,
            receipt,
            manifest,
            services_stopped=True,
            target_kind="rehearsal-copy",
            fault_injection="truncate-main",
        )
    assert injected is True
    assert not receipt.exists()
    unconfirmed = list(tmp_path.glob(".restore.json.cold-restore-*.unconfirmed"))
    assert len(unconfirmed) == 1
    assert json.loads(unconfirmed[0].read_text(encoding="utf-8"))[
        "restore_result"
    ]["status"] == "restored"
