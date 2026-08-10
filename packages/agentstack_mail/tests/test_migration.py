from __future__ import annotations

import errno
import json
import sqlite3
import subprocess
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
    operation_id = "abandoned"
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


def test_source_mutation_during_copy_fails_before_publish(tmp_path: Path) -> None:
    source, connection = _source(tmp_path)
    destination = tmp_path / "new"

    def fault(phase: str) -> None:
        if phase == "before_verification":
            connection.execute("UPDATE messages SET body_md='changed' WHERE id=20")
            connection.commit()

    try:
        with pytest.raises(VerificationError, match="source changed"):
            copy_state(source, destination, fault_hook=fault)
        assert not destination.exists()
    finally:
        connection.close()


def test_source_mutation_after_fsync_still_fails_before_publish(tmp_path: Path) -> None:
    source, connection = _source(tmp_path)
    destination = tmp_path / "new"

    def fault(phase: str) -> None:
        if phase == "after_fsync":
            connection.execute("UPDATE messages SET body_md='late-change' WHERE id=20")
            connection.commit()

    try:
        with pytest.raises(VerificationError, match="before migration publication"):
            copy_state(source, destination, fault_hook=fault)
        assert not destination.exists()
    finally:
        connection.close()


def test_source_mutation_at_final_pre_publish_seam_is_detected(tmp_path: Path) -> None:
    source, connection = _source(tmp_path)
    destination = tmp_path / "new"

    def fault(phase: str) -> None:
        if phase == "before_publish":
            connection.execute("UPDATE messages SET body_md='last-seam' WHERE id=20")
            connection.commit()

    try:
        with pytest.raises(VerificationError, match="before migration publication"):
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


def test_source_mutation_at_post_publish_seam_keeps_generation_unconfirmed(
    tmp_path: Path,
) -> None:
    source, connection = _source(tmp_path)
    destination = tmp_path / "new"

    def fault(phase: str) -> None:
        if phase == "after_publish":
            connection.execute("UPDATE messages SET body_md='post-publish' WHERE id=20")
            connection.commit()

    try:
        with pytest.raises(VerificationError, match="no longer matches its source baseline"):
            copy_state(source, destination, fault_hook=fault)
        assert destination.is_dir()
        assert (destination / ".agentstack-mail-migration-staging.json").is_file()
        with pytest.raises(VerificationError, match="does not match"):
            verify_copy(source, destination)
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
            destination_root: Path, _source_paths: StatePaths
        ) -> tuple[str, str] | None:
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
