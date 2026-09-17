from __future__ import annotations

import json
from pathlib import Path
import sqlite3

import dashboard.server as server


def _write_profile(
    path: Path,
    *,
    name: str,
    project: str,
    interaction: object,
    agent_id: int = 1,
    provider: object = "claude",
    server_instance_id: str = "mail-instance-fixture",
) -> None:
    connection = path.with_name(f"{path.stem}-connection.json")
    connection.write_text(
        json.dumps(
            {
                "kind": "orrery-mail-connection-v1",
                "expected_server_instance_id": server_instance_id,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    connection.chmod(0o600)
    path.write_text(
        json.dumps(
            {
                "kind": "orrery-persistent-agent-v1",
                "name": name,
                "agent_id": agent_id,
                "project_key": project,
                "connection": connection.name,
                "provider": provider,
                "parentless": True,
                "lifecycle": "persistent",
                "interaction": interaction,
                "state_dir": "/state",
                "working_directory": "/project",
                "command": ["claude", "--channels", "telegram"],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    path.chmod(0o600)


def test_profile_metadata_is_separate_and_invalid_or_legacy_rows_are_unknown(
    tmp_path: Path, monkeypatch
):
    database = tmp_path / "mail.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE mail_instances (
                id INTEGER PRIMARY KEY, instance_id TEXT NOT NULL
            );
            CREATE TABLE projects (id INTEGER PRIMARY KEY, human_key TEXT);
            CREATE TABLE agents (id INTEGER PRIMARY KEY, project_id INTEGER, name TEXT);
            INSERT INTO mail_instances VALUES (1, 'mail-instance-fixture');
            INSERT INTO projects VALUES (1, '/project');
            INSERT INTO projects VALUES (2, '/other');
            INSERT INTO agents VALUES (1, 1, 'PersistentBot');
            INSERT INTO agents VALUES (2, 2, 'OtherBot');
            INSERT INTO agents VALUES (3, 1, 'BadProvider');
            INSERT INTO agents VALUES (4, 1, 'ForeignAuthority');
            INSERT INTO agents VALUES (5, 1, 'BadInteraction');
            INSERT INTO agents VALUES (6, 1, 'NullProvider');
            """
        )
    profiles = tmp_path / "profiles"
    profiles.mkdir()
    _write_profile(
        profiles / "interactive.json",
        name="PersistentBot",
        project="/project",
        interaction="interactive",
    )
    _write_profile(
        profiles / "other-project.json",
        name="OtherBot",
        project="/other",
        interaction="headless",
    )
    unsafe = profiles / "unsafe.json"
    _write_profile(unsafe, name="UnsafeBot", project="/project", interaction="headless")
    unsafe.chmod(0o644)
    _write_profile(
        profiles / "bad-provider.json",
        name="BadProvider",
        project="/project",
        interaction="headless",
        agent_id=3,
        provider=[],
    )
    _write_profile(
        profiles / "wrong-id.json",
        name="PersistentBot",
        project="/project",
        interaction="headless",
        agent_id=999,
    )
    _write_profile(
        profiles / "foreign-authority.json",
        name="ForeignAuthority",
        project="/project",
        interaction="headless",
        agent_id=4,
        server_instance_id="another-mail-instance",
    )
    _write_profile(
        profiles / "bad-interaction.json",
        name="BadInteraction",
        project="/project",
        interaction={},
        agent_id=5,
    )
    _write_profile(
        profiles / "null-provider.json",
        name="NullProvider",
        project="/project",
        interaction="interactive",
        agent_id=6,
        provider=None,
    )
    monkeypatch.setattr(server, "DB_PATH", str(database))
    monkeypatch.setattr(server, "PERSISTENT_PROFILES_DIR", str(profiles))
    monkeypatch.setattr(server, "PROJECT_KEY", "/project")
    monkeypatch.setattr(server, "VAULT", "")

    assert server._persistent_profiles() == {
        "PersistentBot": {
            "profile_provider": "claude",
            "parentless": True,
            "lifecycle": "persistent",
            "interaction": "interactive",
        }
    }


def test_dashboard_row_keeps_tmux_surface_and_adds_profile_interaction(
    tmp_path: Path, monkeypatch
):
    database = tmp_path / "mail.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE projects (id INTEGER PRIMARY KEY, human_key TEXT);
            CREATE TABLE mail_instances (
                id INTEGER PRIMARY KEY, instance_id TEXT NOT NULL
            );
            CREATE TABLE agents (
                id INTEGER PRIMARY KEY, project_id INTEGER, name TEXT, model TEXT,
                program TEXT, task_description TEXT, last_active_ts TEXT,
                inception_ts TEXT, retired_at TEXT
            );
            INSERT INTO projects VALUES (1, '/project');
            INSERT INTO mail_instances VALUES (1, 'mail-instance-fixture');
            INSERT INTO agents VALUES (
                1, 1, 'PersistentBot', 'claude-opus-5', 'claude-code', 'bot',
                datetime('now'), datetime('now'), NULL
            );
            INSERT INTO agents VALUES (
                2, 1, 'LegacyBot', 'claude-sonnet-5', 'claude-code', 'legacy',
                datetime('now'), datetime('now'), NULL
            );
            """
        )
    profiles = tmp_path / "profiles"
    profiles.mkdir()
    _write_profile(
        profiles / "persistent.json",
        name="PersistentBot",
        project="/project",
        interaction="headless",
    )
    monkeypatch.setattr(server, "DB_PATH", str(database))
    monkeypatch.setattr(server, "PROJECT_KEY", "/project")
    monkeypatch.setattr(server, "VAULT", "")
    monkeypatch.setattr(server, "PERSISTENT_PROFILES_DIR", str(profiles))
    monkeypatch.setattr(server, "tmux_state", lambda: {})
    monkeypatch.setattr(server, "agentmail_state", lambda: ({}, {}))
    monkeypatch.setattr(server, "_codex_app_runtimes", lambda: {})
    monkeypatch.setattr(server, "_deliverables_index", lambda: {})
    monkeypatch.setattr(server, "_retired_names", lambda _key: set())
    monkeypatch.setattr(server, "_name_substitutions", lambda: {})
    monkeypatch.setattr(server, "_observe_activity", lambda *_args, **_kwargs: 0)
    monkeypatch.setattr(server, "_prune_observed", lambda _names: None)

    rows = {row["name"]: row for row in server.build_agents()}
    persistent = rows["PersistentBot"]
    assert persistent["surface"] == "tmux"
    assert persistent["provider"] == "anthropic"
    assert persistent["profile_provider"] == "claude"
    assert persistent["parentless"] is True
    assert persistent["lifecycle"] == "persistent"
    assert persistent["interaction"] == "headless"
    legacy = rows["LegacyBot"]
    assert legacy["surface"] == "tmux"
    assert legacy["profile_provider"] == ""
    assert legacy["parentless"] is None
    assert legacy["lifecycle"] == "unknown"
    assert legacy["interaction"] == "unknown"


def test_deck_marks_only_the_headless_interaction_exception() -> None:
    html = (Path(__file__).resolve().parents[1] / "dashboard" / "index.html").read_text(
        encoding="utf-8"
    )
    assert "a.interaction==='headless'" in html
    assert "BRIDGE · HEADLESS" in html
    assert "INTERACTIVE" not in html
