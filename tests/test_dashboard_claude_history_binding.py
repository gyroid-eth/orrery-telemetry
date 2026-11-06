"""Claude history is exported only from a verified self-registration receipt."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime

import pytest

from dashboard import server

AGENT = "ExactClaude"
SID = "125687ab-1111-4222-8333-123456789abc"
OTHER_SID = "125687ab-1111-4222-8333-123456789def"
RECEIPT_TS = "2026-11-04T15:00:00"


@pytest.fixture
def binding(tmp_path, monkeypatch):
    project = str(tmp_path / "workspace")
    db = tmp_path / "fixture.sqlite3"
    with sqlite3.connect(db) as con:
        con.executescript("""
            CREATE TABLE projects (id INTEGER PRIMARY KEY, human_key TEXT);
            CREATE TABLE agents (id INTEGER PRIMARY KEY, project_id INTEGER,
                name TEXT, program TEXT, model TEXT, task_description TEXT,
                inception_ts TEXT, last_active_ts TEXT, retired_at TEXT);
        """)
        con.executemany("INSERT INTO projects VALUES (?, ?)", [(1, project), (2, project + "-other")])
        con.executemany("INSERT INTO agents VALUES (?, ?, ?, ?, '', '', ?, ?, NULL)", [
            (42, 1, AGENT, "claude-code", "2026-11-04 00:00:00", "2026-11-04 00:00:00"),
            # An unscoped name lookup would select the other project's row.
            (99, 2, AGENT, "claude-code", "2026-11-04 01:00:00", "2026-11-04 01:00:00"),
        ])
    indices = tmp_path / "runtime" / "session_index"
    indices.mkdir(parents=True)
    projects = tmp_path / "claude" / "projects"
    directory = projects / "-workspace"
    directory.mkdir(parents=True)
    transcript = directory / f"{SID}.jsonl"
    transcript.write_text(json.dumps({"type": "user", "sessionId": SID,
                                      "message": {"role": "user", "content": "fixture"}}) + "\n")
    record = {"schema_version": 2, "binding_kind": "self", "agent_id": 42,
              "agent_name": AGENT, "registered_by": AGENT, "project_key": project,
              "session_id": SID, "transcript_path": str(transcript), "cwd": project,
              "ts": RECEIPT_TS}
    path = indices / "42.json"
    path.write_text(json.dumps(record))
    monkeypatch.setattr(server, "DB_PATH", str(db))
    monkeypatch.setattr(server, "PROJECT_KEY", project)
    monkeypatch.setattr(server, "VAULT", "")
    monkeypatch.setattr(server, "SESSION_INDEX_DIR", str(indices))
    monkeypatch.setattr(server, "CLAUDE_PROJECTS", str(projects))

    def forbidden(*args, **kwargs):
        raise AssertionError("verified binding entered a heuristic or another provider")

    for name in ("_transcript_path", "_indexed_transcript", "_scan_selfref", "_codex_transcript_path"):
        monkeypatch.setattr(server, name, forbidden)
    return {"record": record, "path": path, "transcript": transcript, "db": db,
            "indices": indices, "directory": directory}


def rewrite(binding, **changes):
    binding["record"].update(changes)
    binding["path"].write_text(json.dumps(binding["record"]))


def assert_unbound(reason):
    result = server._claude_history_binding(AGENT)
    assert result["history_binding"] == "unbound"
    assert result["history_binding_reason_code"] == reason
    assert result["history_binding_reason"]
    assert result["transcript_path"] is None


def test_bound_exact_receipt_ignores_other_session_in_same_cwd(binding):
    other = binding["directory"] / f"{OTHER_SID}.jsonl"
    other.write_text(json.dumps({"type": "user", "sessionId": OTHER_SID}) + "\n")
    result = server._claude_history_binding(AGENT)
    assert result == {"history_binding": "bound", "history_binding_reason": "",
                      "history_binding_reason_code": "bound",
                      "transcript_path": str(binding["transcript"]),
                      "history_session_id": SID, "history_receipt_ts": RECEIPT_TS}


def test_missing_receipt_does_not_guess(binding):
    binding["path"].unlink()
    assert_unbound("binding_missing")


@pytest.mark.parametrize("changes", [
    {"schema_version": 1}, {"binding_kind": "delegated"}, {"agent_id": 99},
    {"agent_id": True}, {"agent_name": "ParentClaude"}, {"registered_by": "ParentClaude"},
    {"project_key": "/another-project"}, {"provider": "codex"}, {"program": "codex-cli"},
    {"session_id": ""}, {"session_id": "../escape"},
])
def test_unproven_receipt_is_unbound(binding, changes):
    rewrite(binding, **changes)
    assert_unbound("invalid_binding")


@pytest.mark.parametrize("raw", ["[]", "null", "{", "0"])
def test_malformed_receipt_is_unbound(binding, raw):
    binding["path"].write_text(raw)
    assert_unbound("invalid_binding")


def test_filename_session_id_mismatch(binding):
    rewrite(binding, session_id=OTHER_SID)
    assert_unbound("id_mismatch")


def test_transcript_metadata_session_id_mismatch(binding):
    binding["transcript"].write_text(json.dumps({"type": "user", "sessionId": OTHER_SID}) + "\n")
    assert_unbound("id_mismatch")


@pytest.mark.parametrize("raw", ["{}\n", "", "{bad}\n", json.dumps({"type": "user", "sessionId": SID})])
def test_absent_or_incomplete_native_metadata_is_unbound(binding, raw):
    binding["transcript"].write_text(raw)
    assert_unbound("id_mismatch")


def test_metadata_without_session_id_may_precede_native_record(binding):
    original = binding["transcript"].read_text()
    binding["transcript"].write_text('{"type":"file-history-snapshot"}\n' + original)
    assert server._claude_history_binding(AGENT)["history_binding"] == "bound"


def test_missing_file_is_unbound(binding):
    binding["transcript"].unlink()
    assert_unbound("no_transcript")


def test_transcript_outside_claude_projects_is_unbound(binding, tmp_path):
    other = tmp_path / f"{SID}.jsonl"
    other.write_text(binding["transcript"].read_text())
    rewrite(binding, transcript_path=str(other))
    assert_unbound("no_transcript")


@pytest.mark.parametrize("path", ["relative.jsonl", "/invalid\x00/file.jsonl", "/invalid\ud800/file.jsonl"])
def test_invalid_transcript_path_is_unbound(binding, path):
    rewrite(binding, transcript_path=path)
    assert_unbound("no_transcript")


def test_symlinked_transcript_is_unbound(binding):
    other = binding["directory"] / "target.jsonl"
    binding["transcript"].rename(other)
    binding["transcript"].symlink_to(other)
    assert_unbound("no_transcript")


def test_symlinked_receipt_is_unbound(binding):
    other = binding["indices"] / "copy.json"
    binding["path"].rename(other)
    binding["path"].symlink_to(other)
    assert_unbound("invalid_binding")


def test_two_self_receipts_cannot_claim_one_conversation(binding):
    other = {**binding["record"], "agent_id": 77, "agent_name": "OtherClaude",
             "registered_by": "OtherClaude"}
    (binding["indices"] / "77.json").write_text(json.dumps(other))
    assert_unbound("ambiguous_binding")


def test_resume_with_changed_session_id_replaces_old_binding_without_cache(binding):
    assert server._claude_history_binding(AGENT)["transcript_path"] == str(binding["transcript"])
    new = binding["directory"] / f"{OTHER_SID}.jsonl"
    new.write_text(json.dumps({"type": "user", "sessionId": OTHER_SID}) + "\n")
    rewrite(binding, session_id=OTHER_SID, transcript_path=str(new))
    assert server._claude_history_binding(AGENT)["transcript_path"] == str(new)
    # A failed new receipt must not expose a cached old transcript.
    rewrite(binding, transcript_path="")
    assert_unbound("no_transcript")


def test_retirement_keeps_the_exact_archival_binding(binding):
    with sqlite3.connect(binding["db"]) as con:
        con.execute("UPDATE agents SET retired_at='2026-11-04 01:30:00' WHERE id=42")
    assert server._claude_history_binding(AGENT)["history_binding"] == "bound"


def test_registration_provider_change_revokes_binding(binding):
    with sqlite3.connect(binding["db"]) as con:
        con.execute("UPDATE agents SET program='codex-cli' WHERE id=42")
    assert_unbound("invalid_binding")


@pytest.mark.parametrize("created_delta,expected", [(-1, "bound"), (0, "bound"), (1, "stale")])
def test_receipt_older_than_current_tmux_session_is_stale(binding, created_delta, expected):
    created = datetime.fromisoformat(RECEIPT_TS).timestamp() + created_delta
    result = server._claude_history_binding(AGENT, session_created=created)
    assert result["history_binding"] == expected
    assert result["history_session_id"] == SID
    assert result["history_receipt_ts"] == RECEIPT_TS
    if expected == "stale":
        assert result["transcript_path"] is None
        assert result["history_binding_reason_code"] == "receipt_predates_session"


@pytest.mark.parametrize("timestamp", [None, "", "not-a-date", {}, 123])
def test_unavailable_receipt_time_is_null_and_not_invented(binding, timestamp):
    rewrite(binding, ts=timestamp)
    result = server._claude_history_binding(AGENT, session_created=1234567890)
    assert result["history_binding"] == "bound"
    assert result["history_receipt_ts"] is None


def test_receipt_replaced_during_verification_does_not_publish_old_path(binding, monkeypatch):
    def replace_receipt(*args):
        rewrite(binding, session_id=OTHER_SID)
        return True
    monkeypatch.setattr(server.claude_history, "_claude_transcript_matches", replace_receipt)
    assert_unbound("invalid_binding")


@pytest.mark.parametrize("raw", [
    json.dumps({"type": "user", "message": {"content": SID}}) + "\n",
    json.dumps({"type": "summary", "sessionId": SID}) + "\n",
    json.dumps({"type": [], "sessionId": SID}) + "\n",
    '{"type":"user","sessionId":"' + SID + '","text":"' + 'x' * 262144 + '"}\n',
], ids=["text-only", "summary", "invalid-type", "oversized"])
def test_text_or_non_native_records_and_oversized_prefix_do_not_prove_identity(binding, raw):
    binding["transcript"].write_text(raw)
    assert_unbound("id_mismatch")


@pytest.mark.parametrize("surface", ["retired", "live", "stale"])
def test_build_agents_exports_binding_for_claude(binding, monkeypatch, surface):
    with sqlite3.connect(binding["db"]) as con:
        con.execute("UPDATE agents SET retired_at='2026-11-04 01:30:00' WHERE id=42")
    sessions = {} if surface == "retired" else {AGENT: {
        "title": "", "activity": 123, "cmd": "claude", "attached": False,
        "created": datetime.fromisoformat(RECEIPT_TS).timestamp() + (1 if surface == "stale" else -1),
    }}
    for name, value in (("tmux_state", sessions), ("agentmail_state", ({}, {})),
                        ("_codex_app_runtimes", {}), ("_deliverables_index", {}),
                        ("_persistent_profiles", {}), ("_name_substitutions", {})):
        monkeypatch.setattr(server, name, lambda value=value: value)
    monkeypatch.setattr(server, "_tmux_row_state", lambda *a: ("claude-code", "agent", True, None))
    monkeypatch.setattr(server, "_agent_runtime", lambda *a: {})
    monkeypatch.setattr(server, "_provider_stall_view", lambda *a: None)
    monkeypatch.setattr(server, "_resume_capability_for_row", lambda *a, **kw: "not_required")
    monkeypatch.setattr(server, "_retired_names", lambda *a: {AGENT})
    monkeypatch.setattr(server, "_has_retired_at", lambda: True)
    monkeypatch.setattr(server, "_claude_row_mail_fields", lambda *a, **kw: {})
    rows = server.build_agents(history_days=None)
    assert len(rows) == 1
    assert rows[0]["category"] == ("retired" if surface == "retired" else "agent")
    assert rows[0]["history_binding"] == ("stale" if surface == "stale" else "bound")
    assert rows[0]["history_session_id"] == SID
    assert rows[0]["history_receipt_ts"] == RECEIPT_TS
    assert rows[0]["transcript_path"] == (None if surface == "stale" else str(binding["transcript"]))
