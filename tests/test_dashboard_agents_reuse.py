"""/api/agents is reused for a few seconds and dropped by any state change.

A cockpit and a phone relay together asked for a fresh roster every couple of
seconds, and each answer cost about half a second of CPU. The answer of one
computation is now reused for AGENTS_REUSE_SECONDS, counted from when that
computation started; every POST (spawn, kill, exit, annotate, reactivate, ...)
drops it at once, so an operation's result shows on the next read.
"""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from dashboard import server


@pytest.fixture
def clock(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(server, "_AGENTS_REUSE", server._TimedReuse(4.0, lambda: now[0]))
    return now


@pytest.fixture
def roster(monkeypatch, clock):
    calls = []

    def fake_build_agents(history_days=server.HISTORY_DAYS_DEFAULT):
        calls.append(history_days)
        return [{"name": "Worker", "category": "agent", "running": True, "n": len(calls)}]

    monkeypatch.setattr(server, "build_agents", fake_build_agents)
    server._HTTP_SINGLE_FLIGHT.clear()
    return calls


@pytest.fixture
def http():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_port}"

    def get(path):
        return json.loads(urllib.request.urlopen(base + path, timeout=30).read())

    def post(path, payload):
        request = urllib.request.Request(
            base + path, data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            return urllib.request.urlopen(request, timeout=30).status
        except urllib.error.HTTPError as error:
            return error.code

    try:
        yield get, post
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_both_scopes_share_one_computation_within_the_window(roster, clock, http):
    get, _ = http
    full = get("/api/agents")
    clock[0] += 3.9
    live = get("/api/agents?scope=live")
    assert roster == [server.HISTORY_DAYS_DEFAULT]
    assert live["agents"] == full["agents"] and live["ts"] == full["ts"]


def test_an_answer_older_than_the_window_is_recomputed(roster, clock, http):
    get, _ = http
    get("/api/agents")
    clock[0] += 4.0
    assert get("/api/agents")["agents"][0]["n"] == 2
    assert len(roster) == 2


def test_each_history_window_has_its_own_answer(roster, clock, http):
    get, _ = http
    get("/api/agents")
    get("/api/agents?days=7")
    get("/api/agents?days=7")
    assert roster == [server.HISTORY_DAYS_DEFAULT, 7.0]


@pytest.mark.parametrize("path", ["/api/kill", "/api/exit", "/api/annotate", "/api/spawn",
                                  "/api/reactivate", "/api/jump"])
def test_a_post_drops_the_answer_even_when_it_is_refused(roster, clock, http, path):
    get, post = http
    get("/api/agents")
    post(path, {})  # an empty body is refused; the drop does not depend on success
    get("/api/agents")
    assert len(roster) == 2


def test_a_post_unrelated_to_state_still_leaves_nothing_stale(roster, clock, http):
    get, post = http
    get("/api/agents")
    assert post("/api/not-a-route", {}) == 404
    get("/api/agents")
    assert len(roster) == 2


def test_a_computation_overlapping_a_state_change_is_not_kept(roster, clock):
    reuse = server._AGENTS_REUSE
    mark = reuse.begin()
    server.invalidate_display_reuse()  # the operation finished while it was computing
    reuse.put("k", "stale", mark)
    assert reuse.get("k") is None


def test_stopped_row_bindings_are_reused_until_invalidated(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(server, "_STOPPED_BINDING_REUSE", server._TimedReuse(30.0, lambda: clock[0]))
    calls = []
    # The provider lookup the real function falls through to, counted.
    monkeypatch.setattr(server, "_codex_history_binding",
                        lambda name, now=None: calls.append(name) or {"history_binding": "bound"})
    stopped = {"name": "Old", "program": "codex", "category": "gone", "running": False, "created": 0}
    running = dict(stopped, name="Live", running=True)
    assert server._history_binding_fields(stopped, now=0, reuse_stopped=True) == server._history_binding_fields(stopped, now=0, reuse_stopped=True)
    assert calls == ["Old"]
    server._history_binding_fields(running, now=0, reuse_stopped=True)
    server._history_binding_fields(running, now=0, reuse_stopped=True)
    assert calls == ["Old", "Live", "Live"]  # a running row is judged every time
    server._history_binding_fields(stopped, now=0)  # the light route never reuses
    assert calls[-1] == "Old" and len(calls) == 4
    server.invalidate_display_reuse()
    server._history_binding_fields(stopped, now=0, reuse_stopped=True)
    assert len(calls) == 5
    clock[0] += 31
    server._history_binding_fields(stopped, now=0, reuse_stopped=True)
    assert len(calls) == 6


def test_mail_database_signature_is_withheld_while_the_database_is_fresh(monkeypatch, tmp_path):
    import os
    database = tmp_path / "mail.sqlite3"
    database.write_bytes(b"x")
    monkeypatch.setattr(server, "DB_PATH", str(database))
    assert server._mail_db_signature() is None  # just written: always recompute
    os.utime(database, (1, 1))
    quiet = server._mail_db_signature()
    assert quiet is not None
    assert server._mail_db_signature() == quiet
    database.write_bytes(b"xy")  # a commit changes the identity of the content
    os.utime(database, (2, 2))
    assert server._mail_db_signature() not in (None, quiet)
    wal = tmp_path / "mail.sqlite3-wal"
    wal.write_bytes(b"w")
    os.utime(wal, (3, 3))
    assert server._mail_db_signature()[2] is not None  # the WAL is part of it


def test_last_instructions_are_reused_only_for_an_unchanged_quiet_database(monkeypatch, tmp_path):
    import os
    import sqlite3
    database = tmp_path / "mail.sqlite3"
    con = sqlite3.connect(database)
    con.executescript("""
        CREATE TABLE agents(id INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE messages(id INTEGER PRIMARY KEY, sender_id INT, subject TEXT, created_ts TEXT, importance TEXT);
        CREATE TABLE message_recipients(message_id INT, agent_id INT);
        INSERT INTO agents VALUES (1, 'Boss'), (2, 'Worker');
        INSERT INTO messages VALUES (1, 1, 'first', '2026-01-01 00:00:00', 'normal');
        INSERT INTO message_recipients VALUES (1, 2);
    """)
    con.commit()
    con.close()
    os.utime(database, (1, 1))
    monkeypatch.setattr(server, "DB_PATH", str(database))
    monkeypatch.setattr(server, "_db", lambda: sqlite3.connect(database))
    monkeypatch.setattr(server, "_mail_registrations", lambda con: {})
    server._INSTRUCTION_MEMO.clear()
    assert server.agentmail_state()[1]["Worker"]["subject"] == "first"
    assert server._INSTRUCTION_MEMO  # quiet and unchanged: kept
    other = sqlite3.connect(database)
    other.execute("UPDATE messages SET subject='second'")
    other.commit()
    other.close()
    # The database was just written, so its signature is withheld and it recomputes.
    assert server.agentmail_state()[1]["Worker"]["subject"] == "second"


def test_the_answer_is_dropped_before_a_post_reply_is_written(roster, clock, http, monkeypatch):
    get, post = http
    order = []
    real_invalidate = server.invalidate_display_reuse
    monkeypatch.setattr(server, "invalidate_display_reuse",
                        lambda: order.append("invalidate") or real_invalidate())
    real_end = server.Handler.end_headers
    monkeypatch.setattr(server.Handler, "end_headers",
                        lambda self: order.append("reply") or real_end(self))
    get("/api/agents")
    order.clear()
    post("/api/kill", {})
    assert order.index("invalidate") < order.index("reply")
    deadline = time.monotonic() + 5
    while order[-1] != "invalidate" and time.monotonic() < deadline:
        time.sleep(0.01)  # the handler thread finishes just after the reply
    assert order[-1] == "invalidate"  # and once more after the operation
