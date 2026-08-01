"""Unit coverage for the lightweight dashboard spawn API."""
from __future__ import annotations

import json
import sqlite3
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

import dashboard.server as server


def test_group_only_annotation_is_persisted(monkeypatch, tmp_path):
    path = tmp_path / "annotations.json"
    monkeypatch.setattr(server, "ANNOT_PATH", str(path))
    monkeypatch.setattr(server, "_ANNOT_CACHE", {"mtime": -1.0, "data": {}})

    result = server._write_annotation("WiseFaraday", "", "", "runtime-audit")

    assert result["ok"] is True
    assert json.loads(path.read_text(encoding="utf-8"))["WiseFaraday"] == {
        "role": "", "emoji": "", "group": "runtime-audit",
    }
    assert server._annotations()["WiseFaraday"]["group"] == "runtime-audit"


def test_annotation_is_removed_only_when_all_fields_are_empty(monkeypatch, tmp_path):
    path = tmp_path / "annotations.json"
    path.write_text(
        json.dumps({"WiseFaraday": {"role": "", "emoji": "", "group": "audit"}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(server, "ANNOT_PATH", str(path))
    monkeypatch.setattr(server, "_ANNOT_CACHE", {"mtime": -1.0, "data": {}})

    result = server._write_annotation("WiseFaraday", "", "", "")

    assert result == {"ok": True, "removed": "WiseFaraday"}
    assert json.loads(path.read_text(encoding="utf-8")) == {}


def test_spawn_names_uses_launcher_scientist_source(monkeypatch, tmp_path):
    script = tmp_path / "scientists.sh"
    script.write_text('ags_adjective_list() { printf "Sunny\\n"; }\nags_scientist_list() { printf "Curie\\n"; }\n')
    monkeypatch.setattr(server, "SPAWN_SCIENTISTS_SCRIPT", str(script))
    monkeypatch.setattr(
        server, "_spawn_scientist_statuses",
        lambda _adjectives, scientists: {name: "unknown" for name in scientists},
    )
    data = server.spawn_names_payload()
    assert data["names"] == [{"name": "Curie", "portrait": True, "status": "unknown"}]
    assert data["adjectives"] == ["Sunny"]
    assert data["default_model"] == "claude-sonnet-5"
    assert "emoji" not in data


def test_spawn_names_status_means_any_adjective_pair_is_free(monkeypatch, tmp_path):
    script = tmp_path / "scientists.sh"
    script.write_text(
        'ags_adjective_list() { printf "Sunny\\nZesty\\n"; }\n'
        'ags_scientist_list() { printf "Boltzmann\\nCurie\\n"; }\n'
    )
    db = tmp_path / "mail.sqlite3"
    with sqlite3.connect(db) as con:
        con.execute("CREATE TABLE agents (name TEXT)")
        con.executemany(
            "INSERT INTO agents(name) VALUES (?)",
            [
                ("SunnyBoltzmann",),
                ("ZestyBoltzmann",),
                ("Curie",),  # bare surname must not determine rail status
                ("SunnyCurie",),
            ],
        )
    monkeypatch.setattr(server, "SPAWN_SCIENTISTS_SCRIPT", str(script))
    monkeypatch.setattr(server, "DB_PATH", str(db))
    server._SPAWN_STATUS_CACHE.update(ts=0.0, key=None, data={})

    names = {
        item["name"]: item["status"]
        for item in server.spawn_names_payload()["names"]
    }
    assert names == {"Boltzmann": "occupied", "Curie": "available"}
    # Local DB rows may have the separator stripped while stock requests keep
    # it.  Comparison normalizes only this occupancy check, never API names.
    assert server._spawn_name_status("Sunny-Boltzmann") == "occupied"
    assert server._spawn_name_status("SunnyBoltzmann") == "occupied"


def test_spawn_name_status_fails_closed_when_db_missing(monkeypatch):
    monkeypatch.setattr(server, "DB_PATH", "/definitely/missing.sqlite3")
    assert server._spawn_name_status("Curie") == "unknown"


def test_spawn_names_keeps_home_preset_symbolic(monkeypatch):
    monkeypatch.delenv("AGENTSTACK_SPAWN_DIRS", raising=False)
    monkeypatch.setattr(server.subprocess, "run", lambda *a, **k: type("R", (), {"stdout": "Curie\\n"})())
    monkeypatch.setattr(server, "_spawn_name_status", lambda _: "available")
    assert server.spawn_names_payload()["dirs"] == ["~"]


def test_spawn_names_advertises_codex_provider(monkeypatch):
    monkeypatch.setenv("AGENTSTACK_CODEX_MODELS", "gpt-test-a, gpt-test-b")
    monkeypatch.setattr(server.subprocess, "run", lambda *a, **k: type("R", (), {"stdout": "Sunny\n\036Curie\n"})())
    providers = server.spawn_names_payload()["providers"]
    assert next(provider for provider in providers if provider["id"] == "codex") == {
        "id": "codex", "label": "Codex", "program": "codex-cli",
        "models": ["gpt-test-a", "gpt-test-b"], "default_model": "gpt-test-a",
        "efforts": ["low", "medium", "high", "xhigh"], "effort_default": "xhigh",
    }


def test_spawn_names_uses_current_codex_defaults(monkeypatch):
    monkeypatch.delenv("AGENTSTACK_CODEX_MODELS", raising=False)
    assert server._codex_models() == [
        "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna",
    ]


def test_codex_spawn_passes_model_effort_and_readback_name(monkeypatch, tmp_path):
    launcher = tmp_path / "spawn_child.sh"
    launcher.write_text("#!/bin/bash\n")
    launcher.chmod(0o755)
    calls, launched = [], []

    def mcp(method, args, timeout=15):
        calls.append((method, args))
        return {"ok": True, "data": {"name": "SunnyCurie"} if method == "register_agent" else {}}

    monkeypatch.setattr(server, "SPAWN_SCRIPT", str(launcher))
    monkeypatch.setattr(server, "RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setattr(server, "HERE", str(tmp_path))
    monkeypatch.setattr(server, "_project_key", lambda: "/project")
    monkeypatch.setattr(server, "_spawn_name_status", lambda _: "available")
    monkeypatch.setattr(server, "_mcp_call", mcp)
    monkeypatch.setattr(server.time, "sleep", lambda _: None)
    monkeypatch.setattr(server.subprocess, "Popen", lambda args, **kwargs: launched.append(args))
    monkeypatch.setattr(server.subprocess, "run", lambda *a, **k: type("R", (), {"returncode": 0})())
    result = server.do_spawn({"parent": "Parent", "name": "Sunny-Curie", "task": "work", "dir": str(tmp_path), "provider": "codex", "model": "gpt-5.6-sol", "effort": "high"})

    assert result["ok"] is True
    # The request stays in stock-safe hyphen spelling; only the registration
    # response's actual name is used by the launcher/token path below.
    assert calls[0][1]["name"] == "Sunny-Curie"
    assert launched[0][1:] == ["--pre-registered", "SunnyCurie", "--child-token-file", launched[0][4], "--codex", "--model", "gpt-5.6-sol", "--effort", "high", "work", str(tmp_path)]


def test_auto_spawn_registers_an_explicit_hyphenated_name(monkeypatch, tmp_path):
    """Omitting name must not let stock agent-mail generate a new identity."""
    launcher = tmp_path / "spawn_child.sh"
    launcher.write_text("#!/bin/bash\n")
    launcher.chmod(0o755)
    calls, launched = [], []

    def mcp(method, args, timeout=15):
        calls.append((method, args))
        return {"ok": True, "data": {"name": "Zesty-Curie"} if method == "register_agent" else {}}

    monkeypatch.setattr(server, "SPAWN_SCRIPT", str(launcher))
    monkeypatch.setattr(server, "RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setattr(server, "HERE", str(tmp_path))
    monkeypatch.setattr(server, "_project_key", lambda: "/project")
    monkeypatch.setattr(server, "_suggest_any_spawn_name", lambda: "Zesty-Curie")
    monkeypatch.setattr(server, "_mcp_call", mcp)
    monkeypatch.setattr(server.time, "sleep", lambda _: None)
    monkeypatch.setattr(server.subprocess, "Popen", lambda args, **kwargs: launched.append(args))
    monkeypatch.setattr(server.subprocess, "run", lambda *a, **k: type("R", (), {"returncode": 0})())

    result = server.do_spawn({"parent": "Parent", "task": "work", "dir": str(tmp_path)})

    assert result["ok"] is True
    assert calls[0] == ("register_agent", {
        "project_key": "/project", "program": "claude-code", "model": "claude-sonnet-5",
        "task_description": "work", "registration_token": calls[0][1]["registration_token"],
        "name": "Zesty-Curie",
    })
    assert launched[0][2] == "Zesty-Curie"


def test_standalone_spawn_skips_mail_injects_full_task_and_drops_parent_env(
        monkeypatch, tmp_path):
    launcher = tmp_path / "spawn_child.sh"
    launcher.write_text("#!/bin/bash\n")
    launcher.chmod(0o755)
    calls, launched = [], []

    def mcp(method, args, timeout=15):
        calls.append((method, args))
        return {
            "ok": True,
            "data": {"name": "QuietCurie"} if method == "register_agent" else {},
        }

    def popen(args, **kwargs):
        launched.append((args, kwargs))

    monkeypatch.setattr(server, "SPAWN_SCRIPT", str(launcher))
    monkeypatch.setattr(server, "RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setattr(server, "HERE", str(tmp_path))
    monkeypatch.setattr(server, "_project_key", lambda: "/project")
    monkeypatch.setattr(server, "_spawn_name_status", lambda _: "available")
    monkeypatch.setattr(server, "_mcp_call", mcp)
    monkeypatch.setattr(server.subprocess, "Popen", popen)
    monkeypatch.setattr(
        server.subprocess, "run",
        lambda *a, **k: type("R", (), {"returncode": 0})(),
    )
    monkeypatch.setenv("PARENT_AGENT", "InheritedParent")
    task = "first line\n" + ("full standalone task " * 20)

    result = server.do_spawn({
        "standalone": True,
        "name": "QuietCurie",
        "task": task,
        "dir": str(tmp_path),
    })

    assert result["ok"] is True
    assert result["standalone"] is True
    assert [method for method, _ in calls] == ["register_agent"]
    args, kwargs = launched[0]
    assert "--standalone" in args
    assert args[-2:] == [task.strip(), str(tmp_path)]
    assert "PARENT_AGENT" not in kwargs["env"]


def test_standalone_flag_requires_a_boolean():
    assert server.do_spawn({"standalone": "true"}) == {
        "ok": False, "error": "standalone must be boolean",
    }


def test_spawn_dry_validation_expands_dir_and_uses_current_default(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "SPAWN_SCRIPT", str(tmp_path / "missing"))
    result = server.do_spawn({"parent": "Parent", "task": "work", "dir": str(tmp_path)})
    assert result["error"].startswith("spawn script missing")


def test_suggest_name_refuses_exhausted_candidates(monkeypatch, tmp_path):
    script = tmp_path / "scientists.sh"
    script.write_text(
        'ags_adjective_list() { printf "Sunny\\nZesty\\n"; }\n'
        'ags_scientist_list() { printf "Boltzmann\\n"; }\n'
    )
    monkeypatch.setattr(server, "SPAWN_SCIENTISTS_SCRIPT", str(script))
    monkeypatch.setattr(server, "_spawn_name_status", lambda _: "occupied")
    assert server.suggest_spawn_name("Boltzmann") is None


def test_suggest_name_rejects_scientist_outside_roster(monkeypatch, tmp_path):
    script = tmp_path / "scientists.sh"
    script.write_text(
        'ags_adjective_list() { printf "Stormy\\n"; }\n'
        'ags_scientist_list() { printf "Boltzmann\\n"; }\n'
    )
    monkeypatch.setattr(server, "SPAWN_SCIENTISTS_SCRIPT", str(script))
    monkeypatch.setattr(server, "_spawn_name_status", lambda _: "available")
    assert server.suggest_spawn_name("NotAScientist") is None
    assert server.suggest_spawn_name("Boltzmann") == "Stormy-Boltzmann"


def test_suggest_name_endpoint_returns_409_when_exhausted(monkeypatch):
    monkeypatch.setattr(server, "suggest_spawn_name", lambda _: None)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        with pytest.raises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(f"http://127.0.0.1:{httpd.server_port}/api/suggest-name?scientist=Boltzmann")
        assert error.value.code == 409
    finally:
        httpd.shutdown()
        thread.join()


def test_spawn_dirs_rejects_traversal_external_and_symlink_escape(monkeypatch, tmp_path):
    root = tmp_path / "root"
    inside = root / "alpha"
    outside = tmp_path / "outside"
    inside.mkdir(parents=True)
    outside.mkdir()
    (root / "zulu").mkdir()
    (root / ".hidden").mkdir()
    (root / "escape").symlink_to(outside, target_is_directory=True)
    monkeypatch.setenv("AGENTSTACK_SPAWN_ROOTS", str(root))

    assert server.spawn_directory_suggestions(str(root / ".."))["dirs"] == []
    assert server.spawn_directory_suggestions(str(outside))["dirs"] == []
    result = server.spawn_directory_suggestions(str(root))
    assert [item["name"] for item in result["dirs"]] == ["alpha", "zulu"]
