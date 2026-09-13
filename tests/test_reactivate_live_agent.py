#!/usr/bin/env python3
"""A live agent that ORRERY Mail has retired.

ORRERY Mail retires an agent after 24 hours without activity. For a session
that ended, that is housekeeping. For a long-lived one that was simply idle —
a commander, a monitor — it is not: the agent is still running, still holding
a conversation, and now **inbound mail to it is silently refused** while its
own sends and inbox reads keep working. Nobody finds out until somebody else's
message bounces.

Resume does not fix it. Resume restores a session that ended; pointed at a
live one it just attaches, and no re-registration happens. The only recovery
was to kill the conversation and resume from the transcript.

The dashboard is the one component that can see both sides: ORRERY Mail does
not know tmux is alive, and this does. So it reports the contradiction and
offers a one-step repair — and does not repair anything on its own, because
quietly correcting state is how a broken thing goes on looking fine.

Runnable two ways:
    python3 tests/test_reactivate_live_agent.py
    pytest tests/test_reactivate_live_agent.py
"""
from __future__ import annotations

import importlib.util
import os
import pathlib
import sqlite3
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
SERVER = ROOT / "dashboard" / "server.py"

PROJECT = "/proj"
LIVE = "Sturdy-Koch"


def _db_with(directory: pathlib.Path, *, retired: bool, column: bool = True) -> pathlib.Path:
    db = directory / "storage.sqlite3"
    con = sqlite3.connect(db)
    retired_col = ", retired_at TEXT" if column else ""
    con.executescript(f"""
    CREATE TABLE projects (id INTEGER PRIMARY KEY, human_key TEXT);
    CREATE TABLE agents (id INTEGER PRIMARY KEY, project_id INTEGER, name TEXT,
                         model TEXT, program TEXT, task_description TEXT,
                         last_active_ts TEXT, inception_ts TEXT{retired_col});
    CREATE TABLE messages (id INTEGER PRIMARY KEY, project_id INTEGER, sender_id INTEGER,
                           subject TEXT, body_md TEXT, created_ts TEXT, importance TEXT);
    CREATE TABLE message_recipients (message_id INTEGER, agent_id INTEGER, kind TEXT);
    INSERT INTO projects VALUES (1, '{PROJECT}');
    """)
    if column:
        con.execute(
            "INSERT INTO agents (id, project_id, name, model, program,"
            " task_description, last_active_ts, inception_ts, retired_at)"
            " VALUES (1, 1, ?, 'sonnet', 'claude-code', 'commander',"
            " datetime('now'), datetime('now'), ?)",
            (LIVE, "2026-08-01T00:00:00" if retired else None),
        )
    else:
        con.execute(
            "INSERT INTO agents (id, project_id, name, model, program,"
            " task_description, last_active_ts, inception_ts)"
            " VALUES (1, 1, ?, 'sonnet', 'claude-code', 'commander',"
            " datetime('now'), datetime('now'))",
            (LIVE,),
        )
    con.commit()
    con.close()
    return db


def _load(db: pathlib.Path):
    saved = {k: os.environ.get(k) for k in ("AGENTSTACK_MAIL_DB", "AGENTSTACK_PROJECT_KEY")}
    os.environ["AGENTSTACK_MAIL_DB"] = str(db)
    os.environ["AGENTSTACK_PROJECT_KEY"] = PROJECT
    sys.path.insert(0, str(ROOT / "dashboard"))
    try:
        spec = importlib.util.spec_from_file_location(
            f"srv_react_{id(db)}", SERVER
        )
        module = importlib.util.module_from_spec(spec)
        module_name = spec.name
        previous = sys.modules.get(module_name)
        sys.modules[module_name] = module
        try:
            spec.loader.exec_module(module)
            return module
        finally:
            if previous is None:
                sys.modules.pop(module_name, None)
            else:
                sys.modules[module_name] = previous
    finally:
        sys.path.remove(str(ROOT / "dashboard"))
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def test_a_retired_name_is_reported():
    with tempfile.TemporaryDirectory() as directory:
        server = _load(_db_with(pathlib.Path(directory), retired=True))
        assert LIVE in server._retired_names(PROJECT)


def test_an_active_name_is_not_reported():
    """The null case: this must not flag everybody."""
    with tempfile.TemporaryDirectory() as directory:
        server = _load(_db_with(pathlib.Path(directory), retired=False))
        assert LIVE not in server._retired_names(PROJECT)


def test_a_schema_without_the_column_reports_nobody():
    with tempfile.TemporaryDirectory() as directory:
        server = _load(_db_with(pathlib.Path(directory), retired=False, column=False))
        assert server._retired_names(PROJECT) == set()


def test_reactivate_refuses_an_agent_with_no_live_session():
    """Resume is for sessions that ended. This is only for ones still running."""
    with tempfile.TemporaryDirectory() as directory:
        server = _load(_db_with(pathlib.Path(directory), retired=True))
        result = server.do_reactivate(LIVE)   # no tmux session by that name
        assert result["ok"] is False
        assert "no live tmux session" in result["error"]


def test_reactivate_refuses_an_agent_that_is_not_retired(monkeypatch=None):
    with tempfile.TemporaryDirectory() as directory:
        server = _load(_db_with(pathlib.Path(directory), retired=False))
        server._has_session = lambda _name: True
        result = server.do_reactivate(LIVE)
        assert result["ok"] is False
        assert "not retired" in result["error"]


def test_reactivate_refuses_on_a_schema_that_cannot_retire():
    with tempfile.TemporaryDirectory() as directory:
        server = _load(_db_with(pathlib.Path(directory), retired=False, column=False))
        server._has_session = lambda _name: True
        result = server.do_reactivate(LIVE)
        assert result["ok"] is False
        assert "no retired_at column" in result["error"]


def test_reactivate_rejects_an_invalid_name():
    with tempfile.TemporaryDirectory() as directory:
        server = _load(_db_with(pathlib.Path(directory), retired=True))
        assert server.do_reactivate("../../etc/passwd")["ok"] is False


def test_reactivate_unretires_through_the_mcp_tool():
    """ORRERY Mail has no `/mail/api/unretire-agent` route (that was the old
    third-party mail UI); the published path is the `unretire_agent` tool."""
    with tempfile.TemporaryDirectory() as directory:
        server = _load(_db_with(pathlib.Path(directory), retired=True))
        server._has_session = lambda _name: True
        calls = []

        def fake_mcp_call(method, args, timeout=15):
            calls.append((method, args))
            return {"ok": True, "result": {"status": "active"}}

        server._mcp_call = fake_mcp_call
        result = server.do_reactivate(LIVE)
        assert result == {"ok": True, "session": LIVE, "action": "reactivated"}
        assert calls == [("unretire_agent", {"project_key": PROJECT, "agent_name": LIVE})]


def test_reactivate_reports_a_refused_unretire():
    with tempfile.TemporaryDirectory() as directory:
        server = _load(_db_with(pathlib.Path(directory), retired=True))
        server._has_session = lambda _name: True
        server._mcp_call = lambda *_a, **_k: {"ok": False, "error": "HTTP Error 404"}
        result = server.do_reactivate(LIVE)
        assert result["ok"] is False
        assert "404" in result["error"]


def test_kill_retires_through_the_mcp_tool():
    """DECK's RETIRE went through the same missing web route: tmux was killed,
    the roster never changed. It must use the `retire_agent` tool."""
    with tempfile.TemporaryDirectory() as directory:
        server = _load(_db_with(pathlib.Path(directory), retired=False))
        server.build_agents = lambda history_days=None: [{
            "name": LIVE, "running": False, "attached": False, "category": "gone",
        }]
        server._has_session = lambda _name: False
        calls = []

        def fake_mcp_call(method, args, timeout=15):
            calls.append((method, args))
            return {"ok": True, "result": {"status": "retired"}}

        server._mcp_call = fake_mcp_call
        result = server.do_kill(LIVE, mode="retire")
        assert result["ok"] is True
        assert result["actions"] == ["retired"]
        assert calls == [("retire_agent", {"project_key": PROJECT, "agent_name": LIVE})]


def test_kill_reports_a_refused_retire_in_actions():
    with tempfile.TemporaryDirectory() as directory:
        server = _load(_db_with(pathlib.Path(directory), retired=False))
        server.build_agents = lambda history_days=None: [{
            "name": LIVE, "running": False, "attached": False, "category": "gone",
        }]
        server._has_session = lambda _name: False
        server._mcp_call = lambda *_a, **_k: {"ok": False, "error": "HTTP Error 404"}
        result = server.do_kill(LIVE, mode="retire")
        assert result["ok"] is True
        assert result["actions"] == ["retire-err:HTTP Error 404"]


def test_the_mail_web_api_follows_the_configured_endpoint():
    """It used to hardcode 127.0.0.1:8765, so retire failed on any other port."""
    with tempfile.TemporaryDirectory() as directory:
        server = _load(_db_with(pathlib.Path(directory), retired=True))
        server.MCP_HTTP_URL = "http://127.0.0.1:18765/mcp"
        assert server._mail_web_url("/mail/api/unretire-agent") == (
            "http://127.0.0.1:18765/mail/api/unretire-agent"
        )


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as exc:
                failures += 1
                print(f"FAIL {name}: {exc}")
    raise SystemExit(1 if failures else 0)
