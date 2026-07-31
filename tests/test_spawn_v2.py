"""Unit coverage for the lightweight dashboard spawn API."""
from __future__ import annotations

import dashboard.server as server


def test_spawn_names_uses_launcher_scientist_source(monkeypatch, tmp_path):
    script = tmp_path / "scientists.sh"
    script.write_text('ags_adjective_list() { printf "Sunny\\n"; }\nags_scientist_list() { printf "Curie\\n"; }\n')
    monkeypatch.setattr(server, "SPAWN_SCIENTISTS_SCRIPT", str(script))
    monkeypatch.setattr(server, "_spawn_name_status", lambda _: "unknown")
    data = server.spawn_names_payload()
    assert data["names"] == [{"name": "Curie", "portrait": True, "status": "unknown"}]
    assert data["adjectives"] == ["Sunny"]
    assert data["default_model"] == "claude-sonnet-5"
    assert "emoji" not in data


def test_spawn_name_status_fails_closed_when_db_missing(monkeypatch):
    monkeypatch.setattr(server, "DB_PATH", "/definitely/missing.sqlite3")
    assert server._spawn_name_status("Curie") == "unknown"


def test_spawn_names_keeps_home_preset_symbolic(monkeypatch):
    monkeypatch.delenv("AGENTSTACK_SPAWN_DIRS", raising=False)
    monkeypatch.setattr(server.subprocess, "run", lambda *a, **k: type("R", (), {"stdout": "Curie\\n"})())
    monkeypatch.setattr(server, "_spawn_name_status", lambda _: "available")
    assert server.spawn_names_payload()["dirs"] == ["~"]


def test_spawn_dry_validation_expands_dir_and_uses_current_default(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "SPAWN_SCRIPT", str(tmp_path / "missing"))
    result = server.do_spawn({"parent": "Parent", "task": "work", "dir": str(tmp_path)})
    assert result["error"].startswith("spawn script missing")
