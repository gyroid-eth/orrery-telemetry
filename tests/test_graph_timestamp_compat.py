"""Graph compatibility across Python TEXT and Rust INTEGER timestamps."""

from __future__ import annotations

import json
import pathlib
import sqlite3
import threading
import urllib.request
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer

import pytest

from dashboard import graph_data
from dashboard import server


ROOT = pathlib.Path(__file__).resolve().parents[1]
PROJECT = "/timestamp-fixture"
BASE = int(datetime(2026, 8, 5, tzinfo=timezone.utc).timestamp())


def _iso(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%S.%f"
    )


def _stored(epoch: int, storage: str, index: int) -> object:
    if storage == "text":
        return _iso(epoch)
    if storage == "integer":
        return epoch * 1_000_000 + index
    if storage == "mixed":
        return _iso(epoch) if index % 2 == 0 else epoch * 1_000_000 + index
    raise AssertionError(storage)


def _make_db(
    path: pathlib.Path,
    storage: str,
    *,
    retired_at: bool,
) -> pathlib.Path:
    retired_column = ", retired_at TEXT" if retired_at else ""
    connection = sqlite3.connect(path)
    try:
        connection.executescript(f"""
            CREATE TABLE projects (id INTEGER PRIMARY KEY, human_key TEXT);
            CREATE TABLE agents (
                id INTEGER PRIMARY KEY,
                project_id INTEGER,
                name TEXT,
                model TEXT,
                program TEXT,
                task_description TEXT,
                last_active_ts INTEGER,
                inception_ts INTEGER
                {retired_column}
            );
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY,
                project_id INTEGER,
                sender_id INTEGER,
                created_ts INTEGER,
                importance TEXT
            );
            CREATE TABLE message_recipients (
                message_id INTEGER,
                agent_id INTEGER,
                kind TEXT
            );
            INSERT INTO projects VALUES (1, '{PROJECT}');
        """)
        agents = (
            (1, "Parent", BASE, BASE + 20 * 60),
            (2, "Child", BASE + 60, BASE + 21 * 60),
            # Duplicate display names pin the two-stage identity semantics:
            # aggregate by ids first, then re-aggregate by names.
            (3, "Parent", BASE + 2 * 60, BASE + 22 * 60),
            (4, "Child", BASE + 3 * 60, BASE + 23 * 60),
        )
        for index, (agent_id, name, inception, active) in enumerate(agents):
            columns = (
                "id, project_id, name, model, program, task_description, "
                "last_active_ts, inception_ts"
            )
            values = [
                agent_id, 1, name, "model", "codex", "task",
                _stored(active, storage, index),
                _stored(inception, storage, index + 1),
            ]
            if retired_at:
                columns += ", retired_at"
                values.append(None)
            placeholders = ", ".join("?" for _ in values)
            connection.execute(
                f"INSERT INTO agents ({columns}) VALUES ({placeholders})", values
            )

        messages = (
            (1, 1, 2, BASE + 10 * 60, "high"),
            # In the mixed fixture this newer INTEGER loses to the older TEXT
            # under raw SQLite MAX. The normalized result must still be newer.
            (2, 3, 4, BASE + 20 * 60, "normal"),
        )
        for index, (message_id, sender, recipient, created, importance) in enumerate(messages):
            connection.execute(
                "INSERT INTO messages VALUES (?, 1, ?, ?, ?)",
                (message_id, sender, _stored(created, storage, index), importance),
            )
            connection.execute(
                "INSERT INTO message_recipients VALUES (?, ?, 'to')",
                (message_id, recipient),
            )
        connection.commit()
    finally:
        connection.close()
    return path


def _build(monkeypatch, database: pathlib.Path) -> dict:
    monkeypatch.setattr(graph_data, "DB_PATH", str(database))
    monkeypatch.setattr(graph_data, "PROJECT_HUMAN_KEY", PROJECT)
    monkeypatch.setattr(graph_data, "PROJECT_ID", 1)
    return graph_data.build_graph()


@pytest.mark.parametrize(
    ("storage", "retired_at"),
    (("text", True), ("integer", False), ("mixed", False)),
)
def test_logical_graph_is_identical_across_timestamp_storage(
    monkeypatch, tmp_path, storage, retired_at
):
    data = _build(
        monkeypatch,
        _make_db(tmp_path / f"{storage}.sqlite3", storage, retired_at=retired_at),
    )

    assert data["degraded"] is False
    assert data["timestamp_diagnostics"] == {"invalid_count": 0, "fields": {}}
    assert data["edges"] == [{
        "source": "Parent",
        "target": "Child",
        "count": 2,
        "last_ts": BASE + 20 * 60,
        "kind": "to",
    }]
    assert data["spawn"] == [
        {"source": "Parent", "target": "Child", "type": "spawn"}
    ]
    assert {node["act"] for node in data["nodes"]} == {2}
    assert all(node["retired"] is False for node in data["nodes"])


def test_invalid_and_null_timestamps_degrade_without_looking_valid(
    monkeypatch, tmp_path
):
    database = _make_db(
        tmp_path / "invalid.sqlite3", "mixed", retired_at=False
    )
    connection = sqlite3.connect(database)
    try:
        connection.execute(
            "INSERT INTO messages VALUES (3, 1, 1, 'not-a-time', 'normal')"
        )
        connection.execute(
            "INSERT INTO message_recipients VALUES (3, 2, 'to')"
        )
        connection.execute(
            "INSERT INTO messages VALUES (4, 1, 1, NULL, 'normal')"
        )
        connection.execute(
            "INSERT INTO message_recipients VALUES (4, 2, 'to')"
        )
        connection.execute(
            "UPDATE agents SET last_active_ts='bad-active' WHERE id=4"
        )
        connection.execute(
            "UPDATE agents SET inception_ts='bad-inception' WHERE id=3"
        )
        connection.commit()
    finally:
        connection.close()

    data = _build(monkeypatch, database)

    assert data["degraded"] is True
    assert data["timestamp_diagnostics"] == {
        "invalid_count": 3,
        "fields": {
            "agents.inception_ts": 1,
            "agents.last_active_ts": 1,
            "messages.created_ts": 1,
        },
    }
    edge = data["edges"][0]
    assert edge["count"] == 4
    assert edge["last_ts"] == BASE + 20 * 60
    assert any(node["last_active"] is None for node in data["nodes"])
    assert data["spawn"] == [
        {"source": "Parent", "target": "Child", "type": "spawn"}
    ]


def test_python_timestamp_adapter_accepts_offsets_and_numeric_microseconds():
    diagnostics = graph_data._TimestampDiagnostics()
    assert graph_data._to_epoch((BASE + 1) * 1_000_000) == BASE + 1
    assert graph_data._to_epoch(float((BASE + 2) * 1_000_000)) == BASE + 2
    assert graph_data._to_epoch("2026-08-05T09:00:00+09:00") == BASE
    assert graph_data._to_epoch("2026-08-05 00:00:00.123456") == BASE
    assert graph_data._to_epoch(
        "broken", field="messages.created_ts", diagnostics=diagnostics
    ) is None
    assert diagnostics.payload()["invalid_count"] == 1


def test_graph_payload_and_spawn_only_preserve_timestamp_health(monkeypatch):
    unhealthy = {
        "nodes": [],
        "edges": [],
        "spawn": [],
        "timestamp_diagnostics": {
            "invalid_count": 1,
            "fields": {"messages.created_ts": 1},
        },
        "degraded": True,
    }
    monkeypatch.setattr(server, "_raw_graph", lambda: unhealthy)
    payload = server.graph_payload(4, True)
    assert payload["degraded"] is True
    assert payload["timestamp_diagnostics"]["invalid_count"] == 1

    monkeypatch.setattr(server, "graph_payload", lambda _days, _all: {
        **unhealthy,
        "error": "timestamp fixture failed",
    })
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{httpd.server_port}/api/graph?spawn_only=1"
        ) as response:
            spawn_only = json.load(response)
    finally:
        httpd.shutdown()
        thread.join()
    assert spawn_only["error"] == "timestamp fixture failed"
    assert spawn_only["degraded"] is True
    assert spawn_only["timestamp_diagnostics"]["invalid_count"] == 1


def test_dashboard_startup_never_analyzes_the_foreign_mail_database():
    source = (ROOT / "dashboard" / "server.py").read_text(encoding="utf-8")
    assert "_analyze_mail_db" not in source
    assert 'con.execute("ANALYZE")' not in source
