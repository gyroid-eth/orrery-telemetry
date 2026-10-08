from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from dataclasses import asdict, replace
from pathlib import Path

import pytest

from agentstack_mail.namespace_migration import (
    NamespaceMigration,
    PHASES,
    unseal,
    bundle_at,
)
from agentstack_mail.namespace_plan import FenceEvidence
from agentstack_mail.namespace_state_io import (
    StateMigrationError,
    connection,
    rows,
    SourceBundle,
)
from agentstack_mail.namespace_transform import (
    resolve_mail,
    convert_bundle,
    validate_bundle,
    timestamp,
)
from agentstack_mail.namespace_replies import ReplyError

spec = importlib.util.spec_from_file_location(
    "state_fixture", Path(__file__).parent / "fixtures/namespace_state.py"
)
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)


def fence(generation, roster):
    return FenceEvidence(
        generation, roster, {key: "stopped" for key in roster}, True, True, True, 0
    )


@pytest.fixture
def state(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("TMUX", raising=False)
    source, choices = fixture.build(tmp_path / "source")
    return source, choices, NamespaceMigration(source, tmp_path / "migration", choices)


def test_full_mail_delivery_and_state_preservation(state):
    source, choices, run = state
    before = source.manifest()
    status = run.resume(fence=fence)
    assert status["phase"] == "committed" and status["rollback_allowed"]
    assert not status["activation"]
    assert source.manifest() == before
    target = run.workspace / "candidate"
    with connection(source.mail) as old, connection(target / "mail.sqlite3") as current:
        assert rows(old, "message_recipients") == rows(current, "message_recipients")
        assert rows(old, "enrollment_audits") == rows(current, "enrollment_audits")
        assert [
            tuple(row)
            for row in old.execute(
                "SELECT id,registration_token,credential_generation FROM agents ORDER BY id"
            )
        ] == [
            tuple(row)
            for row in current.execute(
                "SELECT id,registration_token,credential_generation FROM agents ORDER BY id"
            )
        ]
        assert (
            current.execute("SELECT reply_to FROM messages WHERE id=102").fetchone()[0]
            == 101
        )
        assert tuple(
            current.execute(
                "SELECT reply_to,legacy_thread_label FROM messages WHERE id=103"
            ).fetchone()
        ) == (None, "review")
        assert (
            current.execute(
                "SELECT path_unknown FROM file_reservations WHERE id=3"
            ).fetchone()[0]
            == 1
        )
        assert (
            current.execute("SELECT cache_valid FROM message_summaries").fetchone()[0]
            == 0
        )
        assert (
            current.execute(
                "SELECT name FROM sqlite_master WHERE name='projects'"
            ).fetchone()
            is None
        )
        assert all(
            not row["notnull"]
            for row in current.execute("PRAGMA table_info(messages)")
            if row["name"] == "legacy_project_id"
        )
        assert current.execute(
            "SELECT message_id FROM fts_messages WHERE fts_messages MATCH '検索'"
        ).fetchall()
    with connection(target / "delivery.sqlite3") as current:
        assert {row["status"] for row in rows(current, "codex_app_delivery_state")} == {
            "delivered",
            "dead_letter",
            "pending",
            "failed",
            "leased",
        }
        failed = next(
            row
            for row in rows(current, "codex_app_delivery_state")
            if row["status"] == "failed"
        )
        assert failed["attempt_count"] == 3
        assert timestamp(failed["next_attempt_at"]) > timestamp(failed["updated_at"])
    assert "fixture-owner-one" not in run.receipt_path.read_text()
    assert "fixture-owner-two" not in json.dumps(status)
    mapping = json.loads((target / "mapping.json").read_text())
    for old, new in mapping["files"].items():
        category, relative = old.split("/", 1)
        assert (getattr(source, category) / relative).read_bytes() == (
            target / new
        ).read_bytes()
    bindings = json.loads((target / "bindings.json").read_text())["bindings"]
    assert bindings[0]["agent_id"] == 1 and bindings[1]["agent_id"] == 2
    assert (
        bindings[1]["agent_name"] == "Beta"
        and bindings[1]["window_uuid"] != "shared-window"
    )


@pytest.mark.parametrize(
    "phase,point",
    [(phase, point) for phase in PHASES for point in ("before", "after")]
    + [("switch_pointer", "after")],
)
def test_every_phase_process_termination_and_idempotent_resume(
    state, phase, point, tmp_path
):
    source, choices, run = state
    before = source.manifest()
    payload = tmp_path / "task.json"
    payload.write_text(
        json.dumps(
            {
                "source": {key: str(value) for key, value in asdict(source).items()},
                "choices": choices,
                "workspace": str(run.workspace),
            }
        )
    )
    code = """import json,os,sys
from pathlib import Path
from agentstack_mail.namespace_state_io import SourceBundle
from agentstack_mail.namespace_migration import NamespaceMigration
from agentstack_mail.namespace_plan import FenceEvidence
payload=json.loads(Path(sys.argv[1]).read_text())
source=SourceBundle(**{key:Path(value) for key,value in payload['source'].items()})
run=NamespaceMigration(source,Path(payload['workspace']),payload['choices'])
def fence(generation,roster):
    return FenceEvidence(generation,roster,{key:'stopped' for key in roster},True,True,True,0)
def fault(phase,point):
    if (phase,point)==(sys.argv[2],sys.argv[3]): os._exit(86)
run.resume(fence=fence,fault=fault)
"""
    result = subprocess.run(
        [sys.executable, "-c", code, str(payload), phase, point],
        capture_output=True,
        text=True,
        timeout=45,
    )
    assert result.returncode == 86, result.stderr
    prior = unseal(run.receipt_path) if run.receipt_path.exists() else None
    status = NamespaceMigration(source, run.workspace, choices).resume(fence=fence)
    receipt = unseal(run.receipt_path)
    if prior:
        assert receipt["windows"] == prior["windows"]
        assert receipt["planner"] is not None
    assert status["phase"] == "committed"
    assert run.resume(fence=fence) == status
    assert source.manifest() == before


@pytest.mark.parametrize(
    "missing,diagnostic",
    [
        ("rename_map", "AGENT_NAME_COLLISION"),
        ("window_map", "WINDOW_MAPPING_REQUIRED"),
        ("lease_anchors", "LEASE_ANCHOR_OR_RULES_UNKNOWN"),
        ("wake_recovery", "WAKE_COMPLETION_UNKNOWN"),
    ],
)
def test_unresolved_input_blocks_without_source_mutation(
    state, missing, diagnostic, tmp_path
):
    source, choices, _ = state
    before = source.manifest()
    choices.pop(missing)
    run = NamespaceMigration(source, tmp_path / "blocked", choices)
    with pytest.raises(ValueError, match=diagnostic):
        run.resume(fence=fence)
    assert source.manifest() == before
    assert not run.pointer_path.exists()


@pytest.mark.parametrize("value", ("999", "102", "0"))
def test_reply_reference_repair_is_explicit(state, value, tmp_path):
    source, choices, _ = state
    with connection(source.mail, write=True) as database:
        database.execute("UPDATE messages SET thread_id=? WHERE id=102", (value,))
    with pytest.raises(ReplyError):
        resolve_mail(source, choices)
    choices["reply_map"] = {"102": 101}
    resolved = resolve_mail(source, choices)
    target = tmp_path / "repaired"
    convert_bundle(source, target, resolved, choices)
    validate_bundle(source, target, resolved, choices)
    with connection(target / "mail.sqlite3") as database:
        assert tuple(
            database.execute(
                "SELECT reply_to,legacy_thread_id_raw FROM messages WHERE id=102"
            ).fetchone()
        ) == (101, value)


def test_absolute_lease_collisions_are_not_resolved_by_dropping_a_holder(state):
    source, choices, run = state
    with connection(source.mail, write=True) as database:
        database.execute(
            "UPDATE file_reservations SET path_pattern='src/a.py' WHERE id=2"
        )
    with pytest.raises(StateMigrationError, match="ABSOLUTE_LEASE_COLLISION"):
        run.resume(fence=fence)
    with connection(source.mail) as database:
        assert len(rows(database, "file_reservations")) == 3


def test_final_rebuild_preserves_post_initial_read_ack_and_wake_completion(state):
    source, choices, run = state

    def update(phase, point):
        if (phase, point) == ("prevalidated", "after"):
            with connection(source.mail, write=True) as database:
                database.execute(
                    "UPDATE message_recipients SET ack_ts='2026-10-08T11:00:00+00:00' WHERE message_id=102"
                )
            with connection(source.delivery, write=True) as database:
                database.execute(
                    "UPDATE codex_app_delivery_state SET status='delivered',lease_owner=NULL,lease_expires_at=NULL,delivered_at='2026-10-08T11:00:00+00:00' WHERE message_id=105"
                )

    assert run.resume(fence=fence, fault=update)["phase"] == "committed"
    with connection(run.workspace / "candidate/mail.sqlite3") as database:
        assert (
            database.execute(
                "SELECT ack_ts FROM message_recipients WHERE message_id=102"
            ).fetchone()[0]
            == "2026-10-08T11:00:00+00:00"
        )
    with connection(run.workspace / "candidate/delivery.sqlite3") as database:
        assert (
            database.execute(
                "SELECT status FROM codex_app_delivery_state WHERE message_id=105"
            ).fetchone()[0]
            == "delivered"
        )


@pytest.mark.parametrize("change", ("source", "candidate", "roster", "block"))
def test_late_change_cannot_pass_final_gate(state, change):
    source, choices, run = state

    def update(phase, point):
        if (phase, point) != ("final_verified", "after"):
            return
        if change in ("source", "candidate"):
            target = (
                source.mail
                if change == "source"
                else run.workspace / "candidate/mail.sqlite3"
            )
            with connection(target, write=True) as database:
                database.execute(
                    "UPDATE message_recipients SET ack_ts='2026-10-08T12:00:00+00:00' WHERE message_id=102"
                )
        else:
            config = json.loads(source.config.read_text())
            (
                config["expected_writers"].pop()
                if change == "roster"
                else config["instruction_blocks"].append({"path": "unknown"})
            )
            source.config.write_text(json.dumps(config))

    with pytest.raises(
        StateMigrationError,
        match="FINAL_GATE_REFUSED|WRITER_ROSTER_CHANGED|BLOCK_COLLECTOR_REQUIRED",
    ):
        run.resume(fence=fence, fault=update)
    assert not run.pointer_path.exists()


def test_fence_must_cover_planned_writer_roster_and_inflight_wakes(state):
    source, choices, run = state

    def incomplete(generation, roster):
        return replace(
            fence(generation, roster),
            expected_writers=roster[:-1],
            writer_states={key: "stopped" for key in roster[:-1]},
        )

    with pytest.raises(StateMigrationError, match="WRITER_FENCE_UNCONFIRMED"):
        run.resume(fence=incomplete)
    assert run.status()["phase"] == "prevalidated"
    with pytest.raises(StateMigrationError, match="WRITER_FENCE_UNCONFIRMED"):
        run.resume(
            fence=lambda generation, roster: replace(
                fence(generation, roster), inflight_wakes=1
            )
        )
    assert run.resume(fence=fence)["phase"] == "committed"


def test_wal_backup_and_roundtrip_preserve_committed_wal_rows(state):
    source, choices, run = state
    with connection(source.mail, write=True) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute(
            "UPDATE message_recipients SET read_ts='2026-10-08T11:30:00+00:00' WHERE message_id=102"
        )
        writer.commit()
        assert source.mail.with_name(source.mail.name + "-wal").exists()
        run.resume(fence=fence)
        with connection(run.workspace / "candidate/mail.sqlite3") as current:
            assert (
                current.execute(
                    "SELECT read_ts FROM message_recipients WHERE message_id=102"
                ).fetchone()[0]
                == "2026-10-08T11:30:00+00:00"
            )


def test_rollback_restores_candidate_pointer_and_reuses_window_decision(state):
    source, choices, run = state
    run.resume(fence=fence)
    windows = unseal(run.receipt_path)["windows"]
    assert run.rollback()["phase"] == "prepared"
    assert json.loads(run.pointer_path.read_text())["authority"] == "legacy-rehearsal"
    assert run.resume(fence=fence)["phase"] == "committed"
    assert unseal(run.receipt_path)["windows"] == windows


@pytest.mark.parametrize("component", ("mail", "delivery", "archive", "bindings"))
def test_rollback_after_any_new_candidate_write_is_refused(state, component):
    source, choices, run = state
    run.resume(fence=fence)
    root = run.workspace / "candidate"
    if component in ("mail", "delivery"):
        with connection(root / (component + ".sqlite3"), write=True) as database:
            database.execute(
                "UPDATE namespace_metadata SET value='1' WHERE key='write_generation'"
            )
    elif component == "archive":
        (root / "archive/new-file").write_bytes(b"new candidate state")
    else:
        (root / "bindings.json").write_text("{}")
    assert not run.status()["rollback_allowed"]
    before = run.pointer_path.read_bytes()
    with pytest.raises(StateMigrationError, match="ROLLBACK_AFTER_NEW_WRITES_REFUSED"):
        run.rollback()
    assert run.pointer_path.read_bytes() == before


def test_receipt_resolution_and_workspace_lock_are_fail_closed(state):
    source, choices, run = state
    with run._locked():
        with pytest.raises(StateMigrationError, match="MIGRATION_BUSY"):
            run.resume(fence=fence)
    run.resume(fence=fence)
    choices["rename_map"]["2"] = "Changed"
    with pytest.raises(StateMigrationError, match="PLAN_CHANGED"):
        NamespaceMigration(source, run.workspace, choices).resume(fence=fence)
    document = json.loads(run.receipt_path.read_text())
    document["payload"]["windows"]["22"] = "different"
    run.receipt_path.write_text(json.dumps(document))
    with pytest.raises(StateMigrationError, match="RECEIPT_INVALID"):
        run.status()


def test_missing_attachment_or_corrupt_candidate_data_blocks_validation(
    state, tmp_path
):
    source, choices, _ = state
    resolved = resolve_mail(source, choices)
    target = tmp_path / "candidate"
    convert_bundle(source, target, resolved, choices)
    with connection(target / "mail.sqlite3", write=True) as database:
        database.execute("UPDATE agents SET registration_token='wrong' WHERE id=1")
    with pytest.raises(StateMigrationError, match="MAIL_ROWS_NOT_PRESERVED"):
        validate_bundle(source, target, resolved, choices)
    (source.archive / "projects/one/attachments/proof.txt").unlink()
    with pytest.raises(StateMigrationError, match="ATTACHMENT_MISSING"):
        convert_bundle(source, tmp_path / "missing", resolved, choices)


def test_readonly_plan_reports_conflicts_without_secrets(state):
    from agentstack_mail.namespace_transform import plan_report

    source, choices, _ = state
    before = source.manifest()
    unresolved = {
        **choices,
        "rename_map": {},
        "window_map": {},
        "lease_anchors": {},
        "wake_recovery": [],
    }
    report = plan_report(source, unresolved)
    assert report["needs_action"] and not report["activation"]
    assert {
        row["agent_id"] for group in report["agent_conflicts"] for row in group
    } == {1, 2}
    assert report["window_unresolved"]
    assert {row["lease_id"] for row in report["lease_unresolved"]} == {1, 2}
    assert any(
        "WAKE_COMPLETION_UNKNOWN" in row["reasons"]
        for row in report["delivery_unresolved"]
    )
    assert "fixture-owner" not in json.dumps(report)
    assert not plan_report(source, choices)["needs_action"]
    assert source.manifest() == before


def test_workspace_and_snapshot_refuse_unowned_or_source_paths(state, tmp_path):
    source, choices, _ = state
    unknown = tmp_path / "unknown"
    unknown.mkdir()
    sentinel = unknown / "keep.txt"
    sentinel.write_text("keep")
    with pytest.raises(StateMigrationError, match="WORKSPACE_NOT_OWNED"):
        NamespaceMigration(source, unknown, choices)
    with pytest.raises(StateMigrationError, match="SNAPSHOT_TARGET_NOT_OWNED"):
        source.snapshot(unknown)
    with pytest.raises(StateMigrationError, match="SOURCE_SNAPSHOT_OVERLAP"):
        source.snapshot(source.archive)
    assert sentinel.read_text() == "keep"


def test_candidate_signal_uses_stable_ids_and_preserves_original_bytes(state):
    source, _, run = state
    run.resume(fence=fence)
    root = run.workspace / "candidate"
    envelopes = list((root / "signals").rglob("*.signal"))
    assert len(envelopes) == 1
    document = json.loads(envelopes[0].read_text())
    assert document["agent_id"] == 2
    assert document["mail_instance_id"] == "fixture-instance"
    assert document["message"]["id"] == 103
    original = root / document["legacy_signal_path"]
    assert (
        original.read_bytes()
        == (
            source.signals
            / document["legacy_signal_path"].removeprefix("legacy-signals/")
        ).read_bytes()
    )


def test_candidate_fts_index_corruption_blocks_validation(state):
    source, choices, run = state
    run.resume(fence=fence)
    root = run.workspace / "candidate"
    resolved = json.loads((root / "mapping.json").read_text())["resolved"]
    with connection(root / "mail.sqlite3", write=True) as database:
        database.execute("DELETE FROM fts_messages_data WHERE id > 10")
    with pytest.raises(StateMigrationError, match="FTS_INDEX_INVALID"):
        validate_bundle(source, root, resolved, choices)


def test_historical_delivery_name_requires_explicit_stable_owner(state):
    from agentstack_mail.namespace_transform import expected_delivery, delivery_key

    source, choices, _ = state
    config = json.loads(source.config.read_text())
    config["delivery_policies"][delivery_key("/fixture/two", "OldAlpha")] = config[
        "delivery_policies"
    ][delivery_key("/fixture/two", "Alpha")]
    source.config.write_text(json.dumps(config))
    with connection(source.delivery, write=True) as database:
        database.execute("UPDATE codex_app_delivery_state SET agent_name='OldAlpha'")
    resolved = resolve_mail(source, choices)
    with pytest.raises(StateMigrationError, match="DELIVERY_MAPPING_UNKNOWN"):
        expected_delivery(source, resolved, choices)
    ids = (101, 103, 104, 105, 106)
    repaired = {
        **choices,
        "delivery_map": {
            delivery_key("/fixture/two", "OldAlpha", mid): 2 for mid in ids
        },
        "wake_recovery": [delivery_key("/fixture/two", "OldAlpha", 105)],
    }
    converted = expected_delivery(source, resolved, repaired)
    assert len(converted) == 5 and {row["agent_id"] for row in converted} == {2}
    assert {row["legacy_agent_name"] for row in converted} == {"OldAlpha"}


def test_two_legacy_delivery_keys_cannot_merge_into_one_state(state):
    from agentstack_mail.namespace_transform import expected_delivery, delivery_key

    source, choices, _ = state
    config = json.loads(source.config.read_text())
    config["delivery_policies"][delivery_key("/fixture/one", "Alpha")] = config[
        "delivery_policies"
    ][delivery_key("/fixture/two", "Alpha")]
    source.config.write_text(json.dumps(config))
    with connection(source.delivery, write=True) as database:
        row = dict(
            database.execute(
                "SELECT * FROM codex_app_delivery_state WHERE message_id=103"
            ).fetchone()
        )
        row["project_key"] = "/fixture/one"
        database.execute(
            "INSERT INTO codex_app_delivery_state("
            + ",".join(row)
            + ") VALUES ("
            + ",".join("?" for _ in row)
            + ")",
            tuple(row.values()),
        )
    repaired = {
        **choices,
        "delivery_map": {delivery_key("/fixture/one", "Alpha", 103): 2},
    }
    with pytest.raises(StateMigrationError, match="DELIVERY_MAPPING_COLLISION"):
        expected_delivery(source, resolve_mail(source, choices), repaired)


def test_archive_git_history_survives_final_snapshot(state):
    source, _, run = state
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "gyroid-eth",
        "GIT_COMMITTER_NAME": "gyroid-eth",
        "GIT_AUTHOR_EMAIL": "117999063+gyroid-eth@users.noreply.github.com",
        "GIT_COMMITTER_EMAIL": "117999063+gyroid-eth@users.noreply.github.com",
    }

    def git(root, *args):
        return subprocess.check_output(
            ["git", "-c", "commit.gpgsign=false", "-C", str(root), *args],
            env=env,
            stderr=subprocess.DEVNULL,
        )

    git(source.archive, "init", "-b", "fixture")
    git(source.archive, "add", ".")
    git(source.archive, "commit", "-m", "添付の初期状態")
    original = (source.archive / "projects/one/attachments/proof.txt").read_bytes()
    (source.archive / "projects/one/attachments/proof.txt").write_bytes(
        original + b"changed"
    )
    git(source.archive, "add", ".")
    git(source.archive, "commit", "-m", "添付の履歴を追加")
    head = git(source.archive, "rev-parse", "HEAD")
    before = source.manifest()
    run.resume(fence=fence)
    final_archive = run.workspace / "final/archive"
    assert git(final_archive, "rev-parse", "HEAD") == head
    assert (
        git(final_archive, "show", "HEAD~1:projects/one/attachments/proof.txt")
        == original
    )
    assert source.manifest() == before


def test_delivery_extra_table_blocks_final_validation(state):
    source, choices, run = state
    run.resume(fence=fence)
    root = run.workspace / "candidate"
    resolved = json.loads((root / "mapping.json").read_text())["resolved"]
    with connection(root / "delivery.sqlite3", write=True) as database:
        database.execute("CREATE TABLE unexpected_state(id INTEGER)")
    with pytest.raises(StateMigrationError, match="DELIVERY_TABLES_CHANGED"):
        validate_bundle(source, root, resolved, choices)


@pytest.mark.parametrize(
    "field,value",
    [("authority", "other"), ("receipt_id", "other"), ("activation", True)],
)
def test_committed_resume_checks_complete_pointer_identity(state, field, value):
    _, _, run = state
    run.resume(fence=fence)
    pointer = json.loads(run.pointer_path.read_text())
    pointer[field] = value
    run.pointer_path.write_text(json.dumps(pointer))
    with pytest.raises(StateMigrationError, match="POINTER_MISMATCH"):
        run.resume(fence=fence)


def test_interrupted_rollback_can_be_repeated_without_losing_candidate(
    state, monkeypatch
):
    _, _, run = state
    run.resume(fence=fence)
    before = (run.workspace / "candidate/mail.sqlite3").read_bytes()
    original = run._save
    with monkeypatch.context() as patch:

        def fail(receipt):
            raise OSError("fixture interrupted rollback receipt")

        patch.setattr(run, "_save", fail)
        with pytest.raises(OSError):
            run.rollback()
    with pytest.raises(StateMigrationError, match="POINTER_MISMATCH"):
        run.resume(fence=fence)
    assert run.rollback()["phase"] == "prepared"
    assert (run.workspace / "candidate/mail.sqlite3").read_bytes() == before
    assert run.resume(fence=fence)["phase"] == "committed"
