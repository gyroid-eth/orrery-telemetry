"""exit / kill / jump look up one agent instead of building the whole history.

They used to call build_agents(None): every agent ever registered, each with
its resume capability, Mail fields and history binding, outside the roster's
single-flight. Under load (load average 40-50, 2026-10-02) that took over ten
seconds, and a bulk exit from the cockpit reported "failed" after its relay gave
up at six while the agent had in fact exited. lookup_agent() gives the same
verdict for one name, through the same running/category code.
"""
from __future__ import annotations

import sqlite3
import time

import pytest

from dashboard import server

PROJECT = "/fixture/project"


def _db(path, rows):
    con = sqlite3.connect(path)
    con.executescript("""
    CREATE TABLE projects (id INTEGER PRIMARY KEY, human_key TEXT);
    CREATE TABLE agents (id INTEGER PRIMARY KEY, project_id INTEGER, name TEXT,
                         model TEXT, program TEXT, task_description TEXT,
                         last_active_ts TEXT, inception_ts TEXT, retired_at TEXT);
    CREATE TABLE messages (id INTEGER PRIMARY KEY, project_id INTEGER, sender_id INTEGER,
                           subject TEXT, body_md TEXT, created_ts TEXT, importance TEXT);
    CREATE TABLE message_recipients (message_id INTEGER, agent_id INTEGER, kind TEXT);
    """)
    con.execute("INSERT INTO projects VALUES (1, ?)", (PROJECT,))
    con.executemany(
        "INSERT INTO agents (project_id, name, model, program, task_description, last_active_ts,"
        " inception_ts, retired_at) VALUES (1, ?, 'sonnet', ?, 't', ?, ?, ?)",
        [(name, program, ts, ts, retired) for name, program, ts, retired in rows],
    )
    con.commit()
    con.close()


def _session(cmd, title=""):
    return {"name": "", "created": 100, "session_id": "$1", "activity": 200, "attached": False,
            "client_tty": None, "cmd": cmd, "pane_pid": 0, "title": title,
            "mail_disabled": "", "mail_disabled_reason": ""}


@pytest.fixture
def world(monkeypatch, tmp_path):
    """Live sessions in tmux, and a Mail history behind them."""
    db = tmp_path / "storage.sqlite3"
    history = [(f"OldAgent{i:04d}", "claude-code", "2026-01-01T00:00:00", None) for i in range(300)]
    _db(db, history + [
        ("LiveClaude", "claude-code", "2026-10-02T00:00:00", None),
        ("HuskClaude", "claude-code", "2026-10-02T00:00:00", None),
        ("GoneClaude", "claude-code", "2026-09-01T00:00:00", None),
        ("RetiredClaude", "claude-code", "2026-09-01T00:00:00", "2026-09-02T00:00:00"),
        ("AppAgent", "codex-app", "2026-10-02T00:00:00", None),
    ])
    sessions = {"LiveClaude": {**_session("claude", "✳ LiveClaude"), "attached": True},
                "HuskClaude": _session("zsh"),
                "plain-shell": _session("zsh")}
    monkeypatch.setattr(server, "DB_PATH", str(db))
    monkeypatch.setattr(server, "_project_key", lambda: PROJECT)
    monkeypatch.setattr(server, "tmux_state", lambda: sessions)
    monkeypatch.setattr(server, "_process_tree_snapshot", lambda: None)
    monkeypatch.setattr(server, "_codex_app_runtimes",
                        lambda: {"AppAgent": {"model": "gpt-5.6-sol"}})
    monkeypatch.setattr(server, "_codex_app_live",
                        lambda _rt: {"live": "", "running": True, "act_state": "work"})
    monkeypatch.setattr(server, "_agent_runtime", lambda *_a: {})
    monkeypatch.setattr(server, "_deliverables_index", lambda: {})
    monkeypatch.setattr(server, "_persistent_profiles", lambda: {})
    monkeypatch.setattr(server, "_observe_activity", lambda *_a: 0.0)
    monkeypatch.setattr(server, "_resume_capability_for_row", lambda *_a, **_k: "ready")
    monkeypatch.setattr(server, "_claude_row_mail_fields", lambda *_a, **_k: {})
    return sessions


NAMES = ["LiveClaude", "HuskClaude", "plain-shell", "GoneClaude", "RetiredClaude", "AppAgent", "OldAgent0007"]


@pytest.mark.parametrize("name", NAMES)
def test_lookup_gives_the_roster_verdict_for_one_name(world, name):
    roster = {row["name"]: row for row in server.build_agents(None)}
    found = server.lookup_agent(name)
    assert found is not None
    for key in ("category", "running", "attached"):
        assert found[key] == roster[name][key], (name, key)


def test_an_unknown_name_is_not_found(world):
    assert server.lookup_agent("NobodyCurie") is None


def test_exit_kill_and_jump_never_build_the_whole_history(world, monkeypatch):
    def forbidden(*_a, **_k):
        raise AssertionError("build_agents called for one agent")
    monkeypatch.setattr(server, "build_agents", forbidden)
    monkeypatch.setattr(server, "_has_session", lambda name: name in world)
    monkeypatch.setattr(server, "_tmux", lambda *_a, **_k: "")
    monkeypatch.setattr(server.subprocess, "run", lambda *_a, **_k: server.subprocess.CompletedProcess([], 0, "", ""))
    monkeypatch.setattr(server.time, "sleep", lambda _s: None)
    # Refusals and acceptances both come from the lookup, never an enumeration error.
    assert "running" in server.do_kill("LiveClaude")["error"]
    assert "only running/finished" in server.do_exit("GoneClaude")["error"]
    assert server.do_exit("NobodyCurie")["error"] == "agent 'NobodyCurie' not found"
    exited = server.do_exit("HuskClaude")
    assert "failed to enumerate" not in str(exited)


def test_one_lookup_does_not_grow_with_the_history(world, monkeypatch):
    """Before/after under a per-row cost standing in for a loaded machine."""
    calls = 0

    def slow_row(*_a, **_k):
        nonlocal calls
        calls += 1
        time.sleep(0.002)
        return "ready"

    monkeypatch.setattr(server, "_resume_capability_for_row", slow_row)
    started = time.monotonic()
    rows = len(server.build_agents(None))
    before = time.monotonic() - started
    calls_before, calls = calls, 0
    started = time.monotonic()
    server.lookup_agent("HuskClaude")
    after = time.monotonic() - started
    print(f"\nwhole history: {rows} rows, {before:.3f}s; one lookup: {after:.3f}s")
    assert calls_before == rows and calls == 0
    assert after < before / 10


def test_lookup_and_roster_choose_the_same_registration_on_a_tie(world, tmp_path, monkeypatch):
    """Two registrations of one name with the same last activity (another
    project): both paths must pick the same one, or a live Codex could look
    finished to kill (#179 review)."""
    con = sqlite3.connect(server.DB_PATH)
    con.execute("INSERT INTO projects VALUES (2, '/other/project')")
    con.executemany(
        "INSERT INTO agents (project_id, name, model, program, task_description, last_active_ts,"
        " inception_ts, retired_at) VALUES (?, 'LiveClaude', 'm', ?, 't', '2026-10-02T00:00:00',"
        " '2026-10-02T00:00:00', NULL)",
        [(2, "codex-cli"), (1, "claude-code"), (2, "codex-cli")],
    )
    con.commit()
    con.close()
    roster = {row["name"]: row for row in server.build_agents(None)}["LiveClaude"]
    found = server.lookup_agent("LiveClaude")
    assert found["program"] == roster["program"]
    for key in ("category", "running", "attached"):
        assert found[key] == roster[key], key
    assert server._mail_agent_for("LiveClaude") == server.agentmail_state()[0]["LiveClaude"]


def test_a_retired_registration_is_never_the_chosen_one(world):
    con = sqlite3.connect(server.DB_PATH)
    con.execute(
        "INSERT INTO agents (project_id, name, model, program, task_description, last_active_ts,"
        " inception_ts, retired_at) VALUES (1, 'LiveClaude', 'm', 'codex-cli', 't',"
        " '2026-10-02T00:00:00', '2026-10-02T00:00:00', '2026-10-02T00:00:01')")
    con.commit()
    con.close()
    assert server.agentmail_state()[0]["LiveClaude"]["program"] == "claude-code"
    assert server._mail_agent_for("LiveClaude")["program"] == "claude-code"


def test_jump_resolves_one_agent_without_the_whole_history(world, monkeypatch):
    def forbidden(*_a, **_k):
        raise AssertionError("build_agents called for one agent")
    monkeypatch.setattr(server, "build_agents", forbidden)
    monkeypatch.setattr(server, "_has_session", lambda name: name in world)
    monkeypatch.setattr(server, "_agent_program", lambda _name: "claude-code")
    seen = []
    monkeypatch.setattr(server, "_resume_capability",
                        lambda name, program, category, **_k: seen.append((name, category)) or "no_history")
    # A husk (finished) and a gone agent both reach the resume check by the lookup.
    assert server.do_jump("HuskClaude")["ok"] is False
    assert server.do_jump("GoneClaude")["ok"] is False
    assert seen == [("HuskClaude", "finished"), ("GoneClaude", "gone")]
