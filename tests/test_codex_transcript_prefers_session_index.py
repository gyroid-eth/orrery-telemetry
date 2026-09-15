"""Codex History resolves only a project-scoped, verified session index.

Codex rollout files do not contain an agent name. A timestamp/cwd scan can
therefore select another concurrently started agent even when it finds only
one candidate. The reader must treat the runtime-written session index as the
only authority, verify it against both ORRERY Mail and the rollout header, and
fail closed when any part is unavailable.
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import pytest

from dashboard import server


AGENT = "IndexedCodex"
AGENT_ID = 42
OTHER_AGENT_ID = 99
SESSION_ID = "01a0a3cb-fc0b-7890-8819-1f9d6b764073"
LAUNCH_ID = "launch-current"
RECEIPT_ID = "receipt-current"


def _write_rollout(path: Path, session_id: str = SESSION_ID) -> None:
    path.write_text(
        json.dumps(
            {
                "type": "session_meta",
                "payload": {"id": session_id, "cwd": str(path.parent)},
            }
        )
        + "\n",
        encoding="utf-8",
    )


def _index_record(project_key: str, transcript: Path, **overrides: object) -> dict:
    record = {
        "schema_version": 2,
        "binding_kind": "self",
        "provider": "codex",
        "program": "codex-cli",
        "agent_id": AGENT_ID,
        "agent_name": AGENT,
        "project_key": project_key,
        "registered_by": AGENT,
        "launch_id": LAUNCH_ID,
        "receipt_id": RECEIPT_ID,
        "session_id": SESSION_ID,
        "transcript_path": str(transcript),
    }
    record.update(overrides)
    return record


def _write_index(index_dir: Path, agent_id: int, record: dict) -> None:
    (index_dir / f"{agent_id}.json").write_text(
        json.dumps(record), encoding="utf-8"
    )


@pytest.fixture()
def binding(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    project = str(tmp_path / "project-a")
    other_project = str(tmp_path / "project-b")
    Path(project).mkdir()
    Path(other_project).mkdir()

    db = tmp_path / "mail.sqlite3"
    with sqlite3.connect(db) as con:
        con.executescript(
            """
            CREATE TABLE projects (id INTEGER PRIMARY KEY, human_key TEXT);
            CREATE TABLE agents (
                id INTEGER PRIMARY KEY,
                project_id INTEGER,
                name TEXT,
                program TEXT,
                last_active_ts TEXT,
                inception_ts TEXT
            );
            """
        )
        con.executemany(
            "INSERT INTO projects (id, human_key) VALUES (?, ?)",
            [(1, project), (2, other_project)],
        )
        con.executemany(
            "INSERT INTO agents "
            "(id, project_id, name, program, last_active_ts, inception_ts) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [
                (AGENT_ID, 1, AGENT, "codex-cli", "2026-09-15 06:00:00", "2026-09-15 06:00:00"),
                # Deliberately newer: an unscoped name lookup chooses this row.
                (OTHER_AGENT_ID, 2, AGENT, "codex-cli", "2026-09-15 07:00:00", "2026-09-15 07:00:00"),
            ],
        )

    index_dir = tmp_path / "session_index"
    index_dir.mkdir()
    launch_dir = tmp_path / "codex_launches"
    launch_dir.mkdir()
    (launch_dir / f"{AGENT_ID}.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "binding_expected": True,
                "provider": "codex",
                "program": "codex-cli",
                "agent_id": AGENT_ID,
                "agent_name": AGENT,
                "project_key": project,
                "launch_id": LAUNCH_ID,
                "launch_kind": "startup",
                "history_mode": "enabled",
                "expected_at": time.time(),
                "claimed_session_id": SESSION_ID,
                "binding_conflicted": False,
                "receipt_id": RECEIPT_ID,
                "last_reason": None,
            }
        ),
        encoding="utf-8",
    )
    transcript = tmp_path / "rollout-exact.jsonl"
    other_transcript = tmp_path / "rollout-other-project.jsonl"
    wrong_transcript = tmp_path / "rollout-stale-cache.jsonl"
    for path in (transcript, other_transcript, wrong_transcript):
        _write_rollout(path)

    monkeypatch.setattr(server, "DB_PATH", str(db))
    monkeypatch.setattr(server, "PROJECT_KEY", project)
    monkeypatch.setattr(server, "VAULT", "")
    monkeypatch.setattr(server, "SESSION_INDEX_DIR", str(index_dir))
    monkeypatch.setattr(server, "CODEX_LAUNCH_DIR", str(launch_dir))
    server._TPATH_CACHE.clear()
    server._TPATH_OWNER.clear()
    yield {
        "db": db,
        "project": project,
        "other_project": other_project,
        "index_dir": index_dir,
        "launch_dir": launch_dir,
        "transcript": transcript,
        "other_transcript": other_transcript,
        "wrong_transcript": wrong_transcript,
    }
    server._TPATH_CACHE.clear()
    server._TPATH_OWNER.clear()


def _forbid_guesses(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("Codex resolver entered a heuristic/provider fallback")

    # These are the old exact-but-unscoped helper and timestamp scan. Keeping
    # them as sentinels proves the resolver did not merely guess the right file.
    monkeypatch.setattr(server, "_indexed_transcript", forbidden)
    monkeypatch.setattr(server.os, "walk", forbidden)
    monkeypatch.setattr(server, "_agent_window", forbidden)


def test_valid_index_is_the_only_cold_resolution(binding, monkeypatch) -> None:
    _write_index(
        binding["index_dir"],
        AGENT_ID,
        _index_record(binding["project"], binding["transcript"]),
    )
    _forbid_guesses(monkeypatch)

    assert server._codex_transcript_path(AGENT) == str(binding["transcript"])


@pytest.mark.parametrize("cached", ["wrong_path", "none"])
def test_warm_wrong_or_none_cache_cannot_hide_a_valid_index(
    binding, monkeypatch, cached: str
) -> None:
    _write_index(
        binding["index_dir"],
        AGENT_ID,
        _index_record(binding["project"], binding["transcript"]),
    )
    now = time.time()
    cached_path = str(binding["wrong_transcript"]) if cached == "wrong_path" else None
    server._TPATH_CACHE[("codex", AGENT)] = (now, cached_path)
    _forbid_guesses(monkeypatch)

    assert server._codex_transcript_path(AGENT) == str(binding["transcript"])


def test_missing_index_ignores_a_stale_success_cache(binding, monkeypatch) -> None:
    server._TPATH_CACHE[("codex", AGENT)] = (
        time.time(),
        str(binding["wrong_transcript"]),
    )
    _forbid_guesses(monkeypatch)

    assert server._codex_transcript_path(AGENT) is None


def test_index_update_and_deletion_are_visible_without_cache_expiry(binding) -> None:
    first = binding["transcript"]
    second = binding["index_dir"].parent / "rollout-resumed.jsonl"
    _write_rollout(second)
    index_path = binding["index_dir"] / f"{AGENT_ID}.json"

    _write_index(
        binding["index_dir"], AGENT_ID, _index_record(binding["project"], first)
    )
    assert server._codex_transcript_path(AGENT) == str(first)

    _write_index(
        binding["index_dir"], AGENT_ID, _index_record(binding["project"], second)
    )
    assert server._codex_transcript_path(AGENT) == str(second)

    index_path.unlink()
    assert server._codex_transcript_path(AGENT) is None


@pytest.mark.parametrize(
    "defect",
    [
        "missing",
        "corrupt",
        "project",
        "agent_id",
        "provider",
        "provider_missing",
        "program",
        "header",
        "registered_by_parent",
        "registered_by_empty",
    ],
)
def test_invalid_or_missing_index_never_falls_back(
    binding, monkeypatch, defect: str
) -> None:
    index_path = binding["index_dir"] / f"{AGENT_ID}.json"
    transcript = binding["transcript"]
    record = _index_record(binding["project"], transcript)
    if defect == "corrupt":
        index_path.write_text("{", encoding="utf-8")
    elif defect != "missing":
        if defect == "project":
            record["project_key"] = binding["other_project"]
        elif defect == "agent_id":
            record["agent_id"] = OTHER_AGENT_ID
        elif defect == "provider":
            record["provider"] = "claude"
        elif defect == "provider_missing":
            record.pop("provider")
        elif defect == "program":
            with sqlite3.connect(binding["db"]) as con:
                con.execute(
                    "UPDATE agents SET program='claude-code' WHERE id=?",
                    (AGENT_ID,),
                )
        elif defect == "header":
            _write_rollout(transcript, "different-runtime-session")
        elif defect == "registered_by_parent":
            record["registered_by"] = "ParentAgent"
        elif defect == "registered_by_empty":
            record["registered_by"] = ""
        _write_index(binding["index_dir"], AGENT_ID, record)

    _forbid_guesses(monkeypatch)
    assert server._codex_transcript_path(AGENT) is None


def test_same_name_in_newer_other_project_cannot_take_the_binding(binding) -> None:
    _write_index(
        binding["index_dir"],
        AGENT_ID,
        _index_record(binding["project"], binding["transcript"]),
    )
    _write_index(
        binding["index_dir"],
        OTHER_AGENT_ID,
        _index_record(
            binding["other_project"],
            binding["other_transcript"],
            agent_id=OTHER_AGENT_ID,
        ),
    )

    assert server._codex_transcript_path(AGENT) == str(binding["transcript"])


def test_symlinked_sessions_path_is_verified_by_its_real_file(binding, tmp_path) -> None:
    sessions_link = tmp_path / "child-codex-home-sessions"
    sessions_link.symlink_to(binding["transcript"].parent, target_is_directory=True)
    linked_transcript = sessions_link / binding["transcript"].name
    _write_index(
        binding["index_dir"],
        AGENT_ID,
        _index_record(binding["project"], linked_transcript),
    )

    assert server._codex_transcript_path(AGENT) == str(linked_transcript)


@pytest.mark.parametrize(
    "header",
    [
        [],
        {"type": "session_meta", "payload": "invalid"},
    ],
    ids=["top-level-list", "payload-string"],
)
def test_structurally_invalid_rollout_header_is_unconfirmed(
    binding, monkeypatch, header: object
) -> None:
    binding["transcript"].write_text(json.dumps(header) + "\n", encoding="utf-8")
    _write_index(
        binding["index_dir"],
        AGENT_ID,
        _index_record(binding["project"], binding["transcript"]),
    )
    _forbid_guesses(monkeypatch)

    assert server._codex_transcript_path(AGENT) is None


def _make_provider_unknown(binding) -> None:
    with sqlite3.connect(binding["db"]) as con:
        con.execute("UPDATE agents SET program=NULL WHERE id=?", (AGENT_ID,))


def test_unknown_provider_history_does_not_enter_any_transcript_resolver(
    binding, monkeypatch
) -> None:
    _make_provider_unknown(binding)

    def forbidden(_name: str) -> None:
        raise AssertionError("unknown provider entered a transcript resolver")

    monkeypatch.setattr(server, "_transcript_path", forbidden)
    monkeypatch.setattr(server, "_codex_transcript_path", forbidden)
    result = server.history_payload(AGENT, 20)

    assert result["ok"] is False
    assert "provider" in result["error"].lower()
    assert "unconfirmed" in result["error"].lower()


def test_unknown_provider_resume_does_not_enter_any_transcript_resolver(
    binding, monkeypatch
) -> None:
    _make_provider_unknown(binding)

    def forbidden(_name: str) -> None:
        raise AssertionError("unknown provider entered a transcript resolver")

    monkeypatch.setattr(server, "_transcript_path", forbidden)
    monkeypatch.setattr(server, "_codex_transcript_path", forbidden)
    result = server.do_resume(AGENT)

    assert result["ok"] is False
    assert "provider" in result["error"].lower()
    assert "unconfirmed" in result["error"].lower()


def test_known_claude_resume_still_uses_the_legacy_resolver(monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(server, "_agent_program", lambda _name: "claude-code")

    def claude_lookup(name: str) -> None:
        calls.append(name)
        return None

    monkeypatch.setattr(server, "_transcript_path", claude_lookup)
    result = server.do_resume("ClaudeAgent")

    assert calls == ["ClaudeAgent"]
    assert result["ok"] is False


def test_codex_history_never_falls_back_to_claude(monkeypatch) -> None:
    monkeypatch.setattr(server, "_agent_program", lambda _name: "codex-cli")
    monkeypatch.setattr(server, "_codex_transcript_path", lambda _name: None)

    def forbidden(_name: str) -> None:
        raise AssertionError("Codex History searched a Claude transcript")

    monkeypatch.setattr(server, "_transcript_path", forbidden)
    result = server.history_payload(AGENT, 20)

    assert result["ok"] is False
    assert "unconfirmed" in result["error"].lower()


def test_existing_claude_history_path_is_unchanged(tmp_path, monkeypatch) -> None:
    transcript = tmp_path / "claude.jsonl"
    transcript.write_text(
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "still Claude"}],
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(server, "_agent_program", lambda _name: "claude-code")
    monkeypatch.setattr(server, "_transcript_path", lambda _name: str(transcript))

    def forbidden(_name: str) -> None:
        raise AssertionError("a valid Claude path must not consult Codex")

    monkeypatch.setattr(server, "_codex_transcript_path", forbidden)
    result = server.history_payload("ClaudeAgent", 20)

    assert result["ok"] is True
    assert result["source"] == "claude"
    assert result["events"][0]["text"] == "still Claude"
