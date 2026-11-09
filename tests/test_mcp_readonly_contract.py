"""MCP projections against the real Dashboard builder and HTTP handler.

Registration data, runtime paths and process observations are isolated fixtures.
No production Mail, tmux, credentials or provider transcripts are consulted.
"""
from __future__ import annotations

import contextlib
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import sqlite3
import sys
import threading

import pytest
from dashboard import server

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "integrations/chatgpt_mcp/src"))
from orrery_chatgpt_mcp.model import Dashboard, ToolError, invoke, snapshot

PROJECT = "/fixture/selected"


@pytest.fixture
def isolated_roster(monkeypatch, tmp_path):
    database = tmp_path / "mail.sqlite3"
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    recent = now.strftime("%Y-%m-%d %H:%M:%S")
    old = (now - timedelta(days=40)).strftime("%Y-%m-%d %H:%M:%S")
    records = [
        (1, 1, "ScopedLive", recent, None), (2, 2, "ForeignLive", recent, None),
        (3, 1, "Finished", recent, None), (4, 1, "Gone", recent, None),
        (5, 1, "Retired", recent, recent), (6, 1, "TooOld", old, None),
        (7, 1, "Ambiguous", recent, None), (8, 2, "Ambiguous", recent, None),
        (9, 1, "RetiredButLive", recent, recent),
    ]
    with sqlite3.connect(database) as db:
        db.executescript("""
            CREATE TABLE projects (id INTEGER PRIMARY KEY, human_key TEXT);
            CREATE TABLE agents (
                id INTEGER PRIMARY KEY, project_id INTEGER, name TEXT, model TEXT,
                program TEXT, task_description TEXT, inception_ts TEXT,
                last_active_ts TEXT, retired_at TEXT
            );
        """)
        db.executemany("INSERT INTO projects VALUES (?, ?)", [(1, PROJECT), (2, "/fixture/foreign")])
        db.executemany("INSERT INTO agents VALUES (?, ?, ?, 'fixture-model', 'codex-cli', "
                       "'private task', '2020-01-01 00:00:00', ?, ?)", records)
    sessions = {name: dict(cmd="codex", title="", activity=1, created=1, attached=False,
                          pane_pid=1, session_id="$1")
                for name in ("ScopedLive", "ForeignLive", "Finished", "Ambiguous", "Unregistered", "RetiredButLive")}
    monkeypatch.setattr(server, "DB_PATH", str(database))
    monkeypatch.setattr(server, "RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setattr(server, "SESSION_INDEX_DIR", str(tmp_path / "session-index"))
    monkeypatch.setattr(server, "_project_key", lambda: PROJECT)
    monkeypatch.setattr(server, "tmux_state", lambda: sessions)
    for attr in ("_codex_app_runtimes", "_deliverables_index", "_name_substitutions", "_persistent_profiles"):
        monkeypatch.setattr(server, attr, lambda: {})
    monkeypatch.setattr(server, "_process_tree_snapshot", lambda: None)
    monkeypatch.setattr(server, "_tmux_row_state", lambda name, s, m, tree:
                        ("codex-cli", "finished" if name == "Finished" else "agent", name != "Finished", None))
    monkeypatch.setattr(server, "_agent_runtime", lambda *a, **k: {})
    monkeypatch.setattr(server, "_history_binding_fields", lambda *a, **k: {})
    monkeypatch.setattr(server, "_resume_capability_for_row", lambda *a, **k: "unavailable")
    monkeypatch.setattr(server.harness_runtime, "metadata", lambda *a, **k: {})
    server._RETIRED_AT_CACHE.clear()
    server._HTTP_SINGLE_FLIGHT.clear()
    return database


@contextlib.contextmanager
def real_dashboard():
    with ThreadingHTTPServer(("127.0.0.1", 0), server.Handler) as http:
        thread = threading.Thread(target=http.serve_forever, daemon=True)
        thread.start()
        try:
            yield Dashboard(f"http://127.0.0.1:{http.server_port}")
        finally:
            http.shutdown()
            thread.join(5)


def test_mixed_projects_and_stopped_agents_from_real_dashboard(isolated_roster):
    with real_dashboard() as dashboard:
        raw = dashboard.read("/api/agents?days=30")
        assert raw["project_key"] == PROJECT
        rows = {row["name"]: row for row in raw["agents"]}
        assert rows["ForeignLive"]["project_key"] == "/fixture/foreign"
        assert rows["Ambiguous"]["project_key"] is None
        assert rows["Unregistered"]["project_key"] is None
        assert "TooOld" not in rows
        projected = snapshot(dashboard, "legacy", project_key=PROJECT)
        expected = {"ScopedLive", "Finished", "Gone", "Retired", "RetiredButLive"}
        assert {row["name"] for row in projected.agents} == expected
        assert {row["category"] for row in projected.agents} == {"agent", "finished", "gone", "retired"}
        status = invoke("orrery_status", {}, dashboard, "legacy", project_key=PROJECT)
        assert status["observations"]["registered"] == len(expected)
        assert "ForeignLive" not in json.dumps(status)
        for name in expected:
            result = invoke("orrery_get_agent", {"name": name}, dashboard, "legacy", project_key=PROJECT)
            assert result["agent"]["name"] == name
        for name in ("ForeignLive", "Ambiguous", "Unregistered", "TooOld"):
            with pytest.raises(ToolError):
                invoke("orrery_get_agent", {"name": name}, dashboard, "legacy", project_key=PROJECT)
        with pytest.raises(ToolError):
            invoke("orrery_list_agents", {}, dashboard, "legacy", project_key="/fixture/foreign")


def test_unavailable_registration_index_never_assigns_live_project(isolated_roster, monkeypatch):
    monkeypatch.setattr(server, "_batched_registrations", lambda: None)
    rows = {row["name"]: row for row in server.build_agents()}
    assert rows["ScopedLive"]["project_key"] is None
    assert rows["ForeignLive"]["project_key"] is None
    assert rows["Gone"]["project_key"] == PROJECT
    assert rows["Retired"]["project_key"] == PROJECT


def test_annotations_stay_private_through_real_dashboard(isolated_roster, monkeypatch, tmp_path):
    monkeypatch.setattr(server, "ANNOT_PATH", str(tmp_path / "annotations.json"))
    monkeypatch.setattr(server, "LEGACY_ANNOT_PATH", str(tmp_path / "legacy-annotations.json"))
    monkeypatch.setattr(server, "_ANNOT_CACHE", {"path": "", "mtime": -1, "data": {}})
    server._write_annotation("ScopedLive", role="private-marker-role", task="private-marker-task")
    with real_dashboard() as dashboard:
        assert dashboard.read("/api/version")["api"] == server.API_GENERATION
        raw = dashboard.read("/api/agents?days=30")
        row = next(row for row in raw["agents"] if row["name"] == "ScopedLive")
        assert row["annot"]["task"] == "private-marker-task"
        for tool, args in (("orrery_status", {}), ("orrery_list_agents", {}),
                           ("orrery_get_agent", {"name": "ScopedLive"})):
            result = invoke(tool, args, dashboard, "legacy", project_key=PROJECT)
            assert "private-marker" not in json.dumps(result)
            assert '"annot"' not in json.dumps(result)
            assert '"task"' not in json.dumps(result)


def test_registration_scope_is_not_a_per_agent_query(isolated_roster, monkeypatch):
    calls = []
    original = server._db

    def counted():
        calls.append(1)
        return original()

    monkeypatch.setattr(server, "_db", counted)
    server.build_agents()
    assert len(calls) <= 6
