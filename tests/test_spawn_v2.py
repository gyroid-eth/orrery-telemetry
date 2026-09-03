"""Unit coverage for the lightweight dashboard spawn API."""
from __future__ import annotations

import json
import pathlib
import sqlite3
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

import dashboard.server as server


def _set_annotation_paths(monkeypatch, tmp_path):
    path = tmp_path / "runtime" / "annotations.json"
    legacy = tmp_path / "dashboard" / "annotations.json"
    monkeypatch.setattr(server, "ANNOT_PATH", str(path))
    monkeypatch.setattr(server, "LEGACY_ANNOT_PATH", str(legacy))
    monkeypatch.setattr(
        server, "_ANNOT_CACHE", {"path": "", "mtime": -1.0, "data": {}}
    )
    return path, legacy


def test_group_only_annotation_is_persisted(monkeypatch, tmp_path):
    path, _legacy = _set_annotation_paths(monkeypatch, tmp_path)

    result = server._write_annotation("WiseFaraday", "", "", "runtime-audit")

    assert result["ok"] is True
    assert json.loads(path.read_text(encoding="utf-8"))["WiseFaraday"] == {
        "role": "", "emoji": "", "group": "runtime-audit",
    }
    assert server._annotations()["WiseFaraday"]["group"] == "runtime-audit"


def test_annotation_is_removed_only_when_all_fields_are_empty(monkeypatch, tmp_path):
    path, _legacy = _set_annotation_paths(monkeypatch, tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps({"WiseFaraday": {"role": "", "emoji": "", "group": "audit"}}),
        encoding="utf-8",
    )

    result = server._write_annotation("WiseFaraday", "", "", "")

    assert result == {"ok": True, "removed": "WiseFaraday"}
    assert json.loads(path.read_text(encoding="utf-8")) == {}


def test_legacy_annotation_is_read_then_migrated_on_write(monkeypatch, tmp_path):
    path, legacy = _set_annotation_paths(monkeypatch, tmp_path)
    legacy.parent.mkdir(parents=True)
    legacy_data = {
        "WiseFaraday": {"role": "auditor", "emoji": "", "group": "runtime"},
        "ProOpus": {"role": "parent", "emoji": "", "group": "runtime"},
    }
    legacy.write_text(json.dumps(legacy_data), encoding="utf-8")

    assert server._annotations() == legacy_data

    result = server._write_annotation(
        "WiseFaraday", "runtime maintainer", "", "runtime"
    )

    assert result["ok"] is True
    migrated = json.loads(path.read_text(encoding="utf-8"))
    assert migrated["WiseFaraday"]["role"] == "runtime maintainer"
    assert migrated["ProOpus"] == legacy_data["ProOpus"]
    assert json.loads(legacy.read_text(encoding="utf-8")) == legacy_data
    assert server._annotations() == migrated


def test_new_annotation_path_wins_over_legacy(monkeypatch, tmp_path):
    path, legacy = _set_annotation_paths(monkeypatch, tmp_path)
    path.parent.mkdir(parents=True)
    legacy.parent.mkdir(parents=True)
    path.write_text(
        json.dumps({"WiseFaraday": {"role": "new", "emoji": "", "group": ""}}),
        encoding="utf-8",
    )
    legacy.write_text(
        json.dumps({"WiseFaraday": {"role": "old", "emoji": "", "group": ""}}),
        encoding="utf-8",
    )

    assert server._annotations()["WiseFaraday"]["role"] == "new"
    server._write_annotation("WiseFaraday", "updated", "", "")
    assert json.loads(path.read_text(encoding="utf-8"))["WiseFaraday"]["role"] == "updated"


def test_annotation_null_case_creates_runtime_store(monkeypatch, tmp_path):
    path, legacy = _set_annotation_paths(monkeypatch, tmp_path)

    assert server._annotations() == {}
    assert not path.exists()
    assert not legacy.exists()

    result = server._write_annotation("WiseFaraday", "maintainer", "", "")

    assert result["ok"] is True
    assert json.loads(path.read_text(encoding="utf-8"))["WiseFaraday"]["role"] == "maintainer"


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


def test_mcp_call_shapes_credentials_to_the_live_server_schema(monkeypatch):
    calls = []

    monkeypatch.setattr(
        server,
        "_mcp_tool_parameters",
        lambda _method: {"project_key", "sender_name", "to", "subject", "body_md"},
    )
    monkeypatch.setattr(
        server,
        "_mcp_jsonrpc",
        lambda method, params, timeout=15: (
            calls.append((method, params, timeout))
            or {
                "ok": True,
                "result": {"structuredContent": {"count": 1}},
            }
        ),
    )

    result = server._mcp_call("send_message", {
        "project_key": "/project",
        "sender_name": "Parent",
        "to": ["Child"],
        "subject": "task",
        "body_md": "work",
        "sender_token": "strict-only-owner-token",
    })

    assert result == {"ok": True, "data": {"count": 1}}
    assert calls[0][0] == "tools/call"
    assert calls[0][1]["arguments"] == {
        "project_key": "/project",
        "sender_name": "Parent",
        "to": ["Child"],
        "subject": "task",
        "body_md": "work",
    }


def test_codex_spawn_passes_model_effort_and_readback_name(monkeypatch, tmp_path):
    launcher = tmp_path / "spawn_child.sh"
    launcher.write_text("#!/bin/bash\n")
    launcher.chmod(0o755)
    calls, launched = [], []

    def mcp(method, args, timeout=15):
        calls.append((method, args))
        return {
            "ok": True,
            "data": {
                "name": "SunnyCurie",
                "registration_token": "server-child-token",
            } if method == "register_agent" else {},
        }

    monkeypatch.setattr(server, "SPAWN_SCRIPT", str(launcher))
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "agent_token_Parent").write_text("parent-owner-token")
    monkeypatch.setattr(server, "RUNTIME_DIR", str(runtime))
    monkeypatch.setattr(server, "HERE", str(tmp_path))
    monkeypatch.setattr(server, "_project_key", lambda: "/project")
    monkeypatch.setattr(server, "_spawn_name_status", lambda _: "available")
    monkeypatch.setattr(server, "_mcp_call", mcp)
    monkeypatch.setattr(server.time, "sleep", lambda _: None)
    monkeypatch.setattr(server.subprocess, "Popen", lambda args, **kwargs: launched.append(args))
    monkeypatch.setattr(server.subprocess, "run", lambda *a, **k: type("R", (), {"returncode": 0})())
    result = server.do_spawn({"parent": "Parent", "name": "Sunny-Curie", "task": "work", "dir": str(tmp_path), "provider": "codex", "model": "gpt-5.6-sol", "effort": "high"})

    assert result["ok"] is True
    assert result["requested_name"] == "Sunny-Curie"
    assert result["child_name"] == "SunnyCurie"
    assert result["name_substituted"] is True
    # The request stays in stock-safe hyphen spelling; only the registration
    # response's actual name is used by the launcher/token path below.
    assert calls[0][1]["name"] == "Sunny-Curie"
    assert [method for method, _ in calls] == [
        "register_agent", "set_contact_policy", "send_message",
    ]
    assert calls[1][1]["registration_token"] == "server-child-token"
    assert calls[2][1]["sender_token"] == "parent-owner-token"
    assert pathlib.Path(launched[0][4]).read_text() == "server-child-token"
    assert launched[0][1:] == ["--pre-registered", "SunnyCurie", "--child-token-file", launched[0][4], "--codex", "--model", "gpt-5.6-sol", "--effort", "high", "work", str(tmp_path)]


def test_auto_spawn_registers_an_explicit_hyphenated_name(monkeypatch, tmp_path):
    """Omitting name must not let stock agent-mail generate a new identity."""
    launcher = tmp_path / "spawn_child.sh"
    launcher.write_text("#!/bin/bash\n")
    launcher.chmod(0o755)
    calls, launched = [], []

    def mcp(method, args, timeout=15):
        calls.append((method, args))
        return {
            "ok": True,
            "data": {
                "name": "Zesty-Curie",
                "registration_token": "server-child-token",
            } if method == "register_agent" else {},
        }

    monkeypatch.setattr(server, "SPAWN_SCRIPT", str(launcher))
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "agent_token_Parent").write_text("parent-owner-token")
    monkeypatch.setattr(server, "RUNTIME_DIR", str(runtime))
    monkeypatch.setattr(server, "HERE", str(tmp_path))
    monkeypatch.setattr(server, "_project_key", lambda: "/project")
    monkeypatch.setattr(server, "_suggest_any_spawn_name", lambda: "Zesty-Curie")
    monkeypatch.setattr(server, "_mcp_call", mcp)
    monkeypatch.setattr(server.time, "sleep", lambda _: None)
    monkeypatch.setattr(server.subprocess, "Popen", lambda args, **kwargs: launched.append(args))
    monkeypatch.setattr(server.subprocess, "run", lambda *a, **k: type("R", (), {"returncode": 0})())

    result = server.do_spawn({"parent": "Parent", "task": "work", "dir": str(tmp_path)})

    assert result["ok"] is True
    assert result["requested_name"] == "Zesty-Curie"
    assert result["name_substituted"] is False
    assert calls[0] == ("register_agent", {
        "project_key": "/project", "program": "claude-code", "model": "claude-sonnet-5",
        "task_description": "work", "registration_token": calls[0][1]["registration_token"],
        "name": "Zesty-Curie",
    })
    assert calls[1] == ("set_contact_policy", {
        "project_key": "/project", "agent_name": "Zesty-Curie",
        "policy": "open", "registration_token": "server-child-token",
    })
    assert calls[2][0] == "send_message"
    assert calls[2][1]["sender_token"] == "parent-owner-token"
    assert pathlib.Path(launched[0][4]).read_text() == "server-child-token"
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
            "data": {
                "name": "QuietCurie",
                "registration_token": "server-child-token",
            } if method == "register_agent" else {},
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
    assert [method for method, _ in calls] == [
        "register_agent", "set_contact_policy",
    ]
    assert calls[1][1]["registration_token"] == "server-child-token"
    args, kwargs = launched[0]
    assert pathlib.Path(args[4]).read_text() == "server-child-token"
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


def test_async_spawn_returns_pending_and_settles_in_the_background(monkeypatch, tmp_path):
    """The page closes its modal at once; the verdict arrives via spawn-status."""
    import threading as _threading
    import time as _time
    launcher = tmp_path / "spawn_child.sh"
    launcher.write_text("#!/bin/bash\n")
    launcher.chmod(0o755)
    release = _threading.Event()

    class SlowProc:
        def wait(self, timeout=None):
            assert release.wait(timeout=5), "launcher wait was never released"
            return 0

    def mcp(method, args, timeout=15):
        return {"ok": True, "data": {"name": "QuietCurie", "registration_token": "tok"} if method == "register_agent" else {}}

    monkeypatch.setattr(server, "SPAWN_SCRIPT", str(launcher))
    monkeypatch.setattr(server, "RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setattr(server, "HERE", str(tmp_path))
    monkeypatch.setattr(server, "_project_key", lambda: "/project")
    monkeypatch.setattr(server, "_spawn_name_status", lambda _: "available")
    monkeypatch.setattr(server, "_mcp_call", mcp)
    monkeypatch.setattr(server.subprocess, "Popen", lambda *a, **k: SlowProc())
    monkeypatch.setattr(server.subprocess, "run", lambda *a, **k: type("R", (), {"returncode": 0})())

    started = _time.monotonic()
    result = server.do_spawn({"standalone": True, "name": "QuietCurie", "task": "work", "dir": str(tmp_path), "async": True})
    assert _time.monotonic() - started < 1.0, "an async spawn must not wait for the launcher"
    assert result["ok"] is True and result["pending"] is True
    assert result["child_name"] == "QuietCurie"
    assert server.spawn_launch_status("QuietCurie")["state"] == "launching"

    release.set()
    deadline = _time.monotonic() + 5
    while server.spawn_launch_status("QuietCurie")["state"] == "launching" and _time.monotonic() < deadline:
        _time.sleep(0.05)
    status = server.spawn_launch_status("QuietCurie")
    assert status["state"] == "ready", status
    assert status["result"]["tmux_session"] == "QuietCurie"
    assert server.spawn_launch_status("Nobody")["ok"] is False


def test_sync_spawn_is_unchanged_without_the_async_flag(monkeypatch, tmp_path):
    launcher = tmp_path / "spawn_child.sh"
    launcher.write_text("#!/bin/bash\n")
    launcher.chmod(0o755)

    class Proc:
        def wait(self, timeout=None):
            return 3

    monkeypatch.setattr(server, "SPAWN_SCRIPT", str(launcher))
    monkeypatch.setattr(server, "RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setattr(server, "HERE", str(tmp_path))
    monkeypatch.setattr(server, "_project_key", lambda: "/project")
    monkeypatch.setattr(server, "_spawn_name_status", lambda _: "available")
    monkeypatch.setattr(server, "_mcp_call", lambda m, a, timeout=15: {"ok": True, "data": {"name": "QuietCurie", "registration_token": "tok"} if m == "register_agent" else {}})
    monkeypatch.setattr(server.subprocess, "Popen", lambda *a, **k: Proc())
    monkeypatch.setattr(server.subprocess, "run", lambda *a, **k: type("R", (), {"returncode": 0})())
    result = server.do_spawn({"standalone": True, "name": "QuietCurie", "task": "work", "dir": str(tmp_path)})
    assert result["ok"] is False
    assert "exited with status 3" in result["error"]
