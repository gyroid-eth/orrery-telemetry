"""Regression coverage for dashboard SQLite descriptor lifetime."""

from __future__ import annotations

import gc
import json
import os
import pathlib
import shutil
import sqlite3
import subprocess
import threading
import time
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

import dashboard.server as server


def _create_mail_db(path: pathlib.Path, project_key: str) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            """
            PRAGMA journal_mode=WAL;
            CREATE TABLE projects (
                id INTEGER PRIMARY KEY,
                human_key TEXT NOT NULL
            );
            CREATE TABLE agents (
                id INTEGER PRIMARY KEY,
                project_id INTEGER NOT NULL,
                name TEXT NOT NULL
            );
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY,
                project_id INTEGER NOT NULL,
                sender_id INTEGER NOT NULL,
                subject TEXT,
                body_md TEXT,
                importance TEXT,
                thread_id TEXT,
                created_ts TEXT NOT NULL
            );
            CREATE TABLE message_recipients (
                message_id INTEGER NOT NULL,
                agent_id INTEGER NOT NULL,
                kind TEXT
            );
            """
        )
        connection.execute(
            "INSERT INTO projects (id, human_key) VALUES (1, ?)",
            (project_key,),
        )
        connection.executemany(
            "INSERT INTO agents (id, project_id, name) VALUES (?, 1, ?)",
            ((1, "Sender"), (2, "Recipient")),
        )
        connection.execute(
            """
            INSERT INTO messages
                (id, project_id, sender_id, subject, body_md, importance,
                 thread_id, created_ts)
            VALUES (1, 1, 1, 'fd test', 'body', 'normal', NULL,
                    datetime('now'))
            """
        )
        connection.execute(
            """
            INSERT INTO message_recipients (message_id, agent_id, kind)
            VALUES (1, 2, 'to')
            """
        )
        connection.commit()
    finally:
        connection.close()


def _database_fd_count(path: pathlib.Path) -> int:
    """Count this process's descriptors for DB, WAL, and SHM files."""
    proc_fds = pathlib.Path("/proc/self/fd")
    prefix = str(path.resolve())
    if proc_fds.is_dir():
        count = 0
        for fd in proc_fds.iterdir():
            try:
                target = os.readlink(fd)
            except OSError:
                continue
            count += target.startswith(prefix)
        return count

    lsof = shutil.which("lsof")
    if not lsof:
        pytest.skip("fd inspection needs /proc/self/fd or lsof")
    result = subprocess.run(
        [lsof, "-p", str(os.getpid()), "-Fn"],
        text=True,
        capture_output=True,
        check=True,
    )
    return sum(
        line[1:].startswith(prefix)
        for line in result.stdout.splitlines()
        if line.startswith("n")
    )


def test_messages_since_requests_do_not_leak_sqlite_descriptors(
    monkeypatch,
    tmp_path,
):
    project_key = str(tmp_path / "project")
    database = tmp_path / "storage.sqlite3"
    _create_mail_db(database, project_key)
    monkeypatch.setattr(server, "DB_PATH", str(database))
    monkeypatch.setattr(server, "PROJECT_KEY", project_key)
    monkeypatch.setattr(server, "VAULT", "")

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    gc_was_enabled = gc.isenabled()
    gc.disable()
    try:
        url = (
            f"http://127.0.0.1:{httpd.server_port}"
            "/api/messages-since?since=0"
        )
        with urllib.request.urlopen(url) as response:
            payload = json.load(response)
        assert payload["ok"] is True
        assert len(payload["messages"]) == 1
        before = _database_fd_count(database)

        for _ in range(40):
            with urllib.request.urlopen(url) as response:
                assert json.load(response)["ok"] is True

        # The response is written after messages_since_payload has left its DB
        # context, so every request's connection must already be closed here.
        time.sleep(0.05)
        after = _database_fd_count(database)
    finally:
        httpd.shutdown()
        thread.join()
        if gc_was_enabled:
            gc.enable()
        gc.collect()

    assert after == before



def test_failing_queries_do_not_leak_sqlite_descriptors(monkeypatch, tmp_path):
    """A query that raises must still release the file.

    The first fix moved every ``with _db()`` onto a connection that closes
    itself, and the remaining callers closed explicitly — but inside the
    ``try``, so any failing query skipped the close. On a large, busy database
    that is not hypothetical: a two-second lock timeout is enough, and a tester
    watched the descriptor table fill until the dashboard stopped answering.
    """
    database = tmp_path / "storage.sqlite3"
    # A real SQLite file with none of the tables the dashboard asks for, so
    # every query raises where the connection is already open.
    connection = sqlite3.connect(database)
    try:
        connection.execute("CREATE TABLE unrelated (id INTEGER)")
        connection.commit()
    finally:
        connection.close()
    monkeypatch.setattr(server, "DB_PATH", str(database))
    monkeypatch.setattr(server, "PROJECT_KEY", str(tmp_path / "project"))
    monkeypatch.setattr(server, "VAULT", "")

    gc_was_enabled = gc.isenabled()
    gc.disable()
    try:
        server.agentmail_state()
        server._agent_window("nobody")
        server._agent_id_for_name("nobody")
        before = _database_fd_count(database)

        for _ in range(20):
            server.agentmail_state()
            server._agent_window("nobody")
            server._agent_id_for_name("nobody")

        after = _database_fd_count(database)
    finally:
        if gc_was_enabled:
            gc.enable()
        gc.collect()

    assert after == before
