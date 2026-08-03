#!/usr/bin/env python3
"""The dashboard reads a database it does not own, at whatever version is installed.

A tester running an agent-mail from forty days earlier has no `retired_at`
column, and every query naming it raised

    OperationalError: no such column: a.retired_at

Those queries sit inside `except: pass`, so the card did not show an error —
it showed *nothing*, which is the worse of the two. (It also leaked the
connection on the way out until the descriptor fix, matching the 124/124 pair
count the same tester reported.)

The fix asks the schema instead of assuming it. These tests pin both the
recovery and the null case: a schema with the column must keep reporting who
is retired, or "degrade gracefully" would just mean "never retire anyone".

Runnable two ways:
    python3 tests/test_dashboard_old_schema.py
    pytest tests/test_dashboard_old_schema.py
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

BASE_SCHEMA = """
CREATE TABLE projects (id INTEGER PRIMARY KEY, human_key TEXT);
CREATE TABLE messages (id INTEGER PRIMARY KEY, project_id INTEGER, sender_id INTEGER,
                       subject TEXT, body_md TEXT, created_ts TEXT, importance TEXT);
CREATE TABLE message_recipients (message_id INTEGER, agent_id INTEGER, kind TEXT);
INSERT INTO projects VALUES (1, '/proj');
"""

OLD_AGENTS = """
CREATE TABLE agents (id INTEGER PRIMARY KEY, project_id INTEGER, name TEXT,
                     model TEXT, program TEXT, task_description TEXT,
                     last_active_ts TEXT, inception_ts TEXT);
"""

NEW_AGENTS = """
CREATE TABLE agents (id INTEGER PRIMARY KEY, project_id INTEGER, name TEXT,
                     model TEXT, program TEXT, task_description TEXT,
                     last_active_ts TEXT, inception_ts TEXT, retired_at TEXT);
"""


def _make_db(directory: pathlib.Path, agents_ddl: str, *, retired: bool) -> pathlib.Path:
    db = directory / "storage.sqlite3"
    con = sqlite3.connect(db)
    con.executescript(BASE_SCHEMA + agents_ddl)
    if "retired_at" in agents_ddl:
        con.execute(
            "INSERT INTO agents (id, project_id, name, model, program,"
            " task_description, last_active_ts, inception_ts, retired_at)"
            " VALUES (1, 1, 'Zesty-Einstein', 'sonnet', 'claude-code', 'parent',"
            " datetime('now'), datetime('now'), ?)",
            ("2026-08-01T00:00:00" if retired else None,),
        )
    else:
        con.execute(
            "INSERT INTO agents (id, project_id, name, model, program,"
            " task_description, last_active_ts, inception_ts)"
            " VALUES (1, 1, 'Zesty-Einstein', 'sonnet', 'claude-code', 'parent',"
            " datetime('now'), datetime('now'))"
        )
    con.commit()
    con.close()
    return db


def _load_server(db: pathlib.Path):
    """Import a fresh copy of server.py bound to this database.

    server.py reads its configuration from the environment at import time, so
    the only way to point it somewhere is to set those variables — and the only
    safe way to do that inside a shared pytest process is to put them back.
    Leaving `AGENTSTACK_MAIL_DB` pointing at a deleted temp directory made five
    installer tests fail elsewhere in the run: they resolve the mail database
    from the same variable and dutifully found this one.
    """
    saved = {
        key: os.environ.get(key)
        for key in ("AGENTSTACK_MAIL_DB", "AGENTSTACK_PROJECT_KEY")
    }
    os.environ["AGENTSTACK_MAIL_DB"] = str(db)
    os.environ["AGENTSTACK_PROJECT_KEY"] = "/proj"
    sys.path.insert(0, str(ROOT / "dashboard"))
    try:
        spec = importlib.util.spec_from_file_location(f"srv_{db.parent.name}", SERVER)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(ROOT / "dashboard"))
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def test_an_agent_mail_without_retired_at_still_shows_its_agents():
    with tempfile.TemporaryDirectory() as directory:
        db = _make_db(pathlib.Path(directory), OLD_AGENTS, retired=False)
        server = _load_server(db)
        assert server._has_retired_at() is False
        agents, _ = server.agentmail_state()
        assert "Zesty-Einstein" in agents, (
            "the old schema swallowed the OperationalError and returned nothing"
        )


def test_a_schema_with_the_column_still_reports_retirement():
    """The null case: degrading gracefully must not mean ignoring the column."""
    with tempfile.TemporaryDirectory() as directory:
        db = _make_db(pathlib.Path(directory), NEW_AGENTS, retired=True)
        server = _load_server(db)
        assert server._has_retired_at() is True
        # agentmail_state excludes retired agents, so a retired one drops out.
        agents, _ = server.agentmail_state()
        assert "Zesty-Einstein" not in agents


def test_a_live_agent_on_the_new_schema_is_still_listed():
    with tempfile.TemporaryDirectory() as directory:
        db = _make_db(pathlib.Path(directory), NEW_AGENTS, retired=False)
        server = _load_server(db)
        assert server._has_retired_at() is True
        agents, _ = server.agentmail_state()
        assert "Zesty-Einstein" in agents


def test_building_the_deck_survives_the_old_schema():
    with tempfile.TemporaryDirectory() as directory:
        db = _make_db(pathlib.Path(directory), OLD_AGENTS, retired=False)
        server = _load_server(db)
        # build_agents runs the 30-day retired-flag query; it must not raise.
        assert isinstance(server.build_agents(), list)


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
