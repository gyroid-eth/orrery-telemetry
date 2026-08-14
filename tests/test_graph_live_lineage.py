"""Parent/child lineage survives a spawn that sends no delegation mail."""

from __future__ import annotations

import pathlib
import sqlite3

import pytest

from dashboard import graph_data


PROJECT = "/lineage-fixture"
BASE = 1_786_000_000


def _make_db(path: pathlib.Path) -> pathlib.Path:
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
                inception_ts INTEGER,
                retired_at TEXT
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
        # Quiet child: registered, never sent or received anything. This is what
        # an --embed-task spawn looks like until it reports back.
        for agent_id, name, inception in (
            (1, "Parent", BASE),
            (2, "QuietChild", BASE + 60),
        ):
            connection.execute(
                "INSERT INTO agents VALUES (?, 1, ?, 'model', 'codex', 'task',"
                " ?, ?, NULL)",
                (agent_id, name, (inception + 600) * 1_000_000,
                 inception * 1_000_000),
            )
        connection.commit()
    finally:
        connection.close()
    return path


def _fake_tmux(directory: pathlib.Path, show_environment_case: str) -> pathlib.Path:
    fake = directory / "tmux"
    fake.write_text(
        "#!/bin/sh\n"
        'case "$1" in\n'
        '  list-sessions) printf "child\\n" ;;\n'
        f"{show_environment_case}"
        "esac\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    return fake


def _build(monkeypatch, database: pathlib.Path, live: dict[str, str]) -> dict:
    monkeypatch.setattr(graph_data, "DB_PATH", str(database))
    monkeypatch.setattr(graph_data, "PROJECT_HUMAN_KEY", PROJECT)
    monkeypatch.setattr(graph_data, "PROJECT_ID", 1)
    monkeypatch.setattr(graph_data, "_live_parents", lambda: dict(live))
    return graph_data.build_graph()


def test_spawn_edge_without_any_message(tmp_path, monkeypatch):
    database = _make_db(tmp_path / "mail.sqlite3")

    graph = _build(monkeypatch, database, {"QuietChild": "Parent"})

    assert graph["spawn"] == [
        {"source": "Parent", "target": "QuietChild", "type": "spawn"}
    ]


def test_message_derived_lineage_still_applies_without_a_live_session(
    tmp_path, monkeypatch
):
    database = _make_db(tmp_path / "mail.sqlite3")
    connection = sqlite3.connect(database)
    try:
        connection.execute(
            "INSERT INTO messages VALUES (1, 1, 1, ?, 'high')",
            ((BASE + 120) * 1_000_000,),
        )
        connection.execute("INSERT INTO message_recipients VALUES (1, 2, 'to')")
        connection.commit()
    finally:
        connection.close()

    graph = _build(monkeypatch, database, {})

    assert graph["spawn"] == [
        {"source": "Parent", "target": "QuietChild", "type": "spawn"}
    ]


def test_unknown_and_self_referential_sessions_are_ignored(tmp_path, monkeypatch):
    database = _make_db(tmp_path / "mail.sqlite3")

    graph = _build(
        monkeypatch,
        database,
        {
            "QuietChild": "SomeoneNotRegistered",  # parent outside the project
            "Stranger": "Parent",                  # session that is not an agent
            "Parent": "Parent",                    # a session cannot spawn itself
        },
    )

    assert graph["spawn"] == []


@pytest.mark.parametrize(
    "stdout, expected",
    [
        ("PARENT_AGENT=Parent\n", {"child": "Parent"}),
        ("-PARENT_AGENT\n", {}),          # tmux marks a removed variable this way
        ("PARENT_AGENT=\n", {}),
        ("", {}),
    ],
)
def test_live_parents_parses_tmux_environment(tmp_path, monkeypatch, stdout, expected):
    env_output = tmp_path / "env-output"
    env_output.write_text(stdout, encoding="utf-8")
    _fake_tmux(
        tmp_path,
        f'  show-environment) cat "{env_output}" ;;\n',
    )
    monkeypatch.setenv("PATH", f"{tmp_path}:/usr/bin:/bin")
    monkeypatch.setattr(
        graph_data, "_LIVE_PARENT_CACHE", {"ts": 0.0, "value": None}
    )

    assert graph_data._live_parents() == expected


def test_live_parents_does_not_cache_a_missing_tmux_server(tmp_path, monkeypatch):
    """A failed listing must not be remembered as "no lineage exists".

    A tmux server that is briefly unreachable would otherwise pin an empty
    lineage for the whole TTL, which looks exactly like the bug being fixed.
    """
    marker = tmp_path / "first-call-done"
    fake = tmp_path / "tmux"
    fake.write_text(
        "#!/bin/sh\n"
        'case "$1" in\n'
        "  list-sessions)\n"
        f'    if [ -e "{marker}" ]; then printf "child\\n"\n'
        f'    else : > "{marker}"; exit 1; fi ;;\n'
        '  show-environment) printf "PARENT_AGENT=Parent\\n" ;;\n'
        "esac\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:/usr/bin:/bin")
    monkeypatch.setattr(
        graph_data, "_LIVE_PARENT_CACHE", {"ts": 0.0, "value": None}
    )

    assert graph_data._live_parents() == {}
    assert graph_data._live_parents() == {"child": "Parent"}
