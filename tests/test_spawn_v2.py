"""Unit coverage for the lightweight dashboard spawn API."""
from __future__ import annotations

import json
import pathlib
import re
import shutil
import sqlite3
import stat
import subprocess
import threading
import time
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
        "efforts": ["low", "medium", "high", "xhigh", "max", "ultra"], "effort_default": "xhigh",
    }


def test_spawn_names_uses_current_codex_defaults(monkeypatch):
    monkeypatch.delenv("AGENTSTACK_CODEX_MODELS", raising=False)
    assert server._codex_models() == [
        "gpt-5.6-sol", "gpt-6-astra", "gpt-5.6-terra", "gpt-5.6-luna",
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
    """Omitting name must not let stock ORRERY Mail generate a new identity."""
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


# --------------------------------------------------------------------------- #
# Readiness timeout: launcher termination grace and cleanup reporting
# --------------------------------------------------------------------------- #
_CLEANUP_LAUNCHER = """#!/bin/bash
# --pre-registered NAME --child-token-file FILE ...
name="$2"
token_file="$4"
cat "$token_file" > "$TEST_RUNTIME/agent_token_$name"
rm -f "$token_file"
cleanup() {
  : > "$TEST_MARK.cleanup-started"
  # Stands in for a mail release/retire or worktree removal still in progress.
  sleep "$TEST_CLEANUP_SECONDS"
  rm -f "$TEST_RUNTIME/agent_token_$name"
  : > "$TEST_MARK.cleanup-done"
}
trap cleanup EXIT
trap 'exit 143' TERM
printf '%s\\n' "$$" > "$TEST_MARK.pid.tmp"
mv "$TEST_MARK.pid.tmp" "$TEST_MARK.pid"
while :; do sleep 0.05; done
"""


def _prepare_real_spawn(monkeypatch, tmp_path, script=_CLEANUP_LAUNCHER):
    launcher = tmp_path / "launcher.sh"
    launcher.write_text(script, encoding="utf-8")
    launcher.chmod(0o755)
    runtime = tmp_path / "runtime"

    def mcp(method, args, timeout=15):
        data = {"name": "QuietCurie", "registration_token": "child-owner-token"} \
            if method == "register_agent" else {}
        return {"ok": True, "data": data}

    monkeypatch.setattr(server, "RUNTIME_DIR", str(runtime))
    monkeypatch.setattr(server, "HERE", str(tmp_path))
    monkeypatch.setattr(server, "_project_key", lambda: "/project")
    monkeypatch.setattr(server, "_spawn_name_status", lambda _: "available")
    monkeypatch.setattr(server, "_mcp_call", mcp)
    monkeypatch.setattr(server.subprocess, "run", lambda *a, **k: type("R", (), {"returncode": 1})())
    real_popen = server.subprocess.Popen
    pid_mark = tmp_path / "mark.pid"

    def popen(*args, **kwargs):
        # Start the readiness clock only once the launcher's traps are in
        # place; a first exec of a new script can take longer than the timeout.
        import time as _time
        proc = real_popen(*args, **kwargs)
        deadline = _time.monotonic() + 30
        while not pid_mark.exists() and proc.poll() is None and _time.monotonic() < deadline:
            _time.sleep(0.02)
        assert pid_mark.exists(), "test launcher never became ready"
        return proc

    monkeypatch.setattr(server.subprocess, "Popen", popen)
    # Reach the timeout path at once; the grace under test stays real.
    monkeypatch.setattr(server, "_SPAWN_READINESS_TIMEOUT_SECONDS", 0.5)
    return launcher, runtime


def _cleanup_spec(launcher, tmp_path, cleanup_seconds, **overrides):
    return server.SpawnLaunchSpec(
        provider="gemini", program="antigravity", model="gemini-test",
        script=str(launcher), signal_process_group=True,
        launcher_env=(
            ("TEST_RUNTIME", str(tmp_path / "runtime")),
            ("TEST_MARK", str(tmp_path / "mark")),
            ("TEST_CLEANUP_SECONDS", str(cleanup_seconds)),
        ),
        **overrides,
    )


def _group_gone(pgid: int, deadline_seconds: float = 5.0) -> bool:
    import os as _os
    import time as _time
    deadline = _time.monotonic() + deadline_seconds
    while _time.monotonic() < deadline:
        try:
            _os.killpg(pgid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            pass
        _time.sleep(0.05)
    return False


def test_process_group_launcher_cleanup_outlives_the_canonical_5s_grace(monkeypatch, tmp_path):
    import time as _time
    launcher, runtime = _prepare_real_spawn(monkeypatch, tmp_path)
    spec = _cleanup_spec(launcher, tmp_path, cleanup_seconds=6)
    assert server._spawn_termination_grace(spec) > 6

    started = _time.monotonic()
    result = server.spawn_with_launch_spec(
        {"standalone": True, "name": "QuietCurie", "task": "work", "dir": str(tmp_path)}, spec)
    elapsed = _time.monotonic() - started

    assert result["ok"] is False
    assert "did not finish readiness checks" in result["error"]
    assert "cleanup_incomplete" not in result
    assert "child registration 'QuietCurie' may remain because" in result["error"]
    assert "child registration 'QuietCurie' remains because" not in result["error"]
    # The cleanup was still running past the old 5s KILL and was allowed to finish.
    assert elapsed >= 5.5
    assert (tmp_path / "mark.cleanup-done").exists()
    assert not (runtime / "agent_token_QuietCurie").exists()
    assert list((runtime / "spawn-tokens").iterdir()) == []
    assert _group_gone(int((tmp_path / "mark.pid").read_text()))


def test_cleanup_killed_past_its_grace_is_reported_and_keeps_owner_credential(monkeypatch, tmp_path):
    import time as _time
    launcher, runtime = _prepare_real_spawn(monkeypatch, tmp_path)
    spec = _cleanup_spec(launcher, tmp_path, cleanup_seconds=60, termination_grace=1.0)

    started = _time.monotonic()
    result = server.spawn_with_launch_spec(
        {"standalone": True, "name": "QuietCurie", "task": "work", "dir": str(tmp_path)}, spec)
    elapsed = _time.monotonic() - started

    assert elapsed < 20, "a cleanup past its grace must be killed, not awaited"
    assert (tmp_path / "mark.cleanup-started").exists()
    assert not (tmp_path / "mark.cleanup-done").exists()
    assert _group_gone(int((tmp_path / "mark.pid").read_text()))

    assert result["ok"] is False
    assert result["registration_retained"] is True
    assert result["cleanup_incomplete"] is True
    assert "did not finish within 1s of SIGTERM and was killed" in result["error"]
    assert "reservations, registration, worktree and branch may remain" in result["error"]
    # The killed cleanup never released/retired: its owner credential is kept
    # for cleanup-child-agent.sh, while the one-shot handoff is still removed.
    owner = runtime / "agent_token_QuietCurie"
    assert owner.read_text() == "child-owner-token"
    assert f"owner credential kept at {owner}" in result["error"]
    assert "cleanup-child-agent.sh QuietCurie can retire" in result["error"]
    assert "does not remove the worktree or branch" in result["error"]
    assert result["recovery_credential"] == str(owner)
    assert result["cleanup_child_agent_can_recover"] is True
    assert list((runtime / "spawn-tokens").iterdir()) == []


def test_canonical_launcher_timeout_keeps_5s_grace_and_removes_credentials(monkeypatch, tmp_path):
    import subprocess as _subprocess
    launcher = tmp_path / "spawn_child.sh"
    launcher.write_text("#!/bin/bash\n")
    launcher.chmod(0o755)
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "agent_token_QuietCurie").write_text("child-owner-token")
    events = []

    class StuckProc:
        pid = 4242

        def wait(self, timeout=None):
            events.append(("wait", timeout))
            raise _subprocess.TimeoutExpired("launcher", timeout)

        def terminate(self):
            events.append(("terminate", None))

        def kill(self):
            events.append(("kill", None))

    monkeypatch.setattr(server, "SPAWN_SCRIPT", str(launcher))
    monkeypatch.setattr(server, "RUNTIME_DIR", str(runtime))
    monkeypatch.setattr(server, "HERE", str(tmp_path))
    monkeypatch.setattr(server, "_project_key", lambda: "/project")
    monkeypatch.setattr(server, "_spawn_name_status", lambda _: "available")
    monkeypatch.setattr(server, "_mcp_call", lambda m, a, timeout=15: {"ok": True, "data": {"name": "QuietCurie", "registration_token": "tok"} if m == "register_agent" else {}})
    monkeypatch.setattr(server.subprocess, "Popen", lambda *a, **k: StuckProc())
    monkeypatch.setattr(server.subprocess, "run", lambda *a, **k: type("R", (), {"returncode": 0})())
    monkeypatch.setattr(server.os, "killpg", lambda *a: events.append(("killpg", a)))

    for payload in ({"provider": "claude"}, {"provider": "codex", "model": server._codex_models()[0]}):
        events.clear()
        (runtime / "agent_token_QuietCurie").write_text("child-owner-token")
        result = server.do_spawn({"standalone": True, "name": "QuietCurie", "task": "work",
                                  "dir": str(tmp_path), **payload})
        assert result["ok"] is False
        assert "did not finish readiness checks within 120s" in result["error"]
        assert "cleanup_incomplete" not in result
        assert "may remain" not in result["error"]
        assert events[:3] == [("wait", 120), ("terminate", None),
                              ("wait", server._SPAWN_TERMINATE_GRACE_SECONDS)]
        assert events[3] == ("kill", None)
        assert not any(name == "killpg" for name, _ in events)
        assert not (runtime / "agent_token_QuietCurie").exists()
    assert server._SPAWN_TERMINATE_GRACE_SECONDS == 5.0


def test_termination_grace_resolution_and_validation():
    base = dict(provider="p", program="x", model="m", script="/bin/true")
    assert server._spawn_termination_grace(server.SpawnLaunchSpec(**base)) == 5.0
    group = server.SpawnLaunchSpec(**base, signal_process_group=True)
    assert server._spawn_termination_grace(group) == server._SPAWN_CLEANUP_GRACE_SECONDS > 5.0
    tuned = server.SpawnLaunchSpec(**base, signal_process_group=True, termination_grace=30)
    assert server._spawn_termination_grace(tuned) == 30.0
    for bad in (0, -1, float("inf"), float("nan"), True, "10"):
        with pytest.raises(ValueError):
            server.SpawnLaunchSpec(**base, termination_grace=bad)


# --------------------------------------------------------------------------- #
# Killed cleanup: which owner credential survives, and where it is reported
# --------------------------------------------------------------------------- #
_REAL_RUN = subprocess.run
_OWNER_TOKEN = "child-owner-token"

_KILLED_CLEANUP_LAUNCHER = """#!/bin/bash
# --pre-registered NAME --child-token-file FILE ...
name="$2"
token_file="$4"
owner="$TEST_RUNTIME/agent_token_$name"
# A cleanup that never finishes: only the SIGKILL after the grace stops it.
trap '' TERM
case "$TEST_MODE" in
  before-copy) ;;
  empty-owner) : > "$owner" ;;
  partial-owner) head -c 5 "$token_file" > "$owner" ;;
  foreign-owner) printf 'someone-elses-token' > "$owner" ;;
  symlink-owner) ln -s "$TEST_DECOY" "$owner" ;;
  consumed-partial) head -c 5 "$token_file" > "$owner"; rm -f "$token_file" ;;
  copied) cat "$token_file" > "$owner" ;;
esac
printf '%s\\n' "$$" > "$TEST_MARK.pid.tmp"
mv "$TEST_MARK.pid.tmp" "$TEST_MARK.pid"
while :; do sleep 0.05; done
"""


def _killed_cleanup_spec(launcher, tmp_path, mode):
    return server.SpawnLaunchSpec(
        provider="gemini", program="antigravity", model="gemini-test",
        script=str(launcher), signal_process_group=True, termination_grace=1.0,
        launcher_env=(
            ("TEST_RUNTIME", str(tmp_path / "runtime")),
            ("TEST_MARK", str(tmp_path / "mark")),
            ("TEST_MODE", mode),
            ("TEST_DECOY", str(tmp_path / "decoy")),
        ),
    )


def _run_killed_cleanup(monkeypatch, tmp_path, mode):
    launcher, runtime = _prepare_real_spawn(monkeypatch, tmp_path, _KILLED_CLEANUP_LAUNCHER)
    (tmp_path / "decoy").write_text("decoy-content")
    spec = _killed_cleanup_spec(launcher, tmp_path, mode)
    result = server.spawn_with_launch_spec(
        {"standalone": True, "name": "QuietCurie", "task": "work", "dir": str(tmp_path)}, spec)
    assert _group_gone(int((tmp_path / "mark.pid").read_text()))
    assert result["ok"] is False
    assert result["cleanup_incomplete"] is True
    assert result["registration_retained"] is True
    assert "did not finish within 1s of SIGTERM and was killed" in result["error"]
    return result, runtime


def _assert_owner_installed(owner):
    info = owner.lstat()
    assert stat.S_ISREG(info.st_mode)
    assert stat.S_IMODE(info.st_mode) == 0o600
    assert owner.read_text() == _OWNER_TOKEN


@pytest.mark.parametrize("mode", ["before-copy", "empty-owner", "partial-owner"])
def test_killed_cleanup_installs_unconsumed_handoff_as_owner_credential(monkeypatch, tmp_path, mode):
    result, runtime = _run_killed_cleanup(monkeypatch, tmp_path, mode)
    owner = runtime / "agent_token_QuietCurie"

    _assert_owner_installed(owner)
    assert result["recovery_credential"] == str(owner)
    assert result["cleanup_child_agent_can_recover"] is True
    assert f"installed from the unconsumed one-shot handoff at {owner}" in result["error"]
    assert "cleanup-child-agent.sh QuietCurie can retire" in result["error"]
    assert "does not remove the worktree or branch" in result["error"]
    # One usable copy is enough: the handoff it came from is gone, and no
    # staging file is left beside the owner credential.
    assert list((runtime / "spawn-tokens").iterdir()) == []
    assert sorted(p.name for p in runtime.iterdir()) == ["agent_token_QuietCurie", "spawn-tokens"]


@pytest.mark.parametrize("mode", ["foreign-owner", "symlink-owner"])
def test_killed_cleanup_keeps_handoff_when_owner_path_is_not_ours_to_replace(monkeypatch, tmp_path, mode):
    result, runtime = _run_killed_cleanup(monkeypatch, tmp_path, mode)
    owner = runtime / "agent_token_QuietCurie"

    if mode == "symlink-owner":
        # Neither followed nor replaced.
        assert owner.is_symlink()
        assert (tmp_path / "decoy").read_text() == "decoy-content"
        assert f"{owner} is not a regular file" in result["error"]
    else:
        assert owner.read_text() == "someone-elses-token"
        assert f"{owner} is holding a different value" in result["error"]
    handoffs = list((runtime / "spawn-tokens").iterdir())
    assert len(handoffs) == 1
    assert handoffs[0].read_text() == _OWNER_TOKEN
    assert result["recovery_credential"] == str(handoffs[0])
    assert result["cleanup_child_agent_can_recover"] is False
    assert f"unconsumed one-shot handoff kept at {handoffs[0]}" in result["error"]
    assert "cannot retire this child" in result["error"]
    assert "cleanup-child-agent.sh QuietCurie can" not in result["error"]


def test_killed_cleanup_without_a_usable_copy_says_so(monkeypatch, tmp_path):
    result, runtime = _run_killed_cleanup(monkeypatch, tmp_path, "consumed-partial")
    owner = runtime / "agent_token_QuietCurie"

    assert owner.read_text() == _OWNER_TOKEN[:5]
    assert result["recovery_credential"] is None
    assert result["cleanup_child_agent_can_recover"] is False
    assert (f"no usable owner credential remains ({owner} is a partial copy; "
            "the one-shot handoff is missing)") in result["error"]
    assert "cleanup-child-agent.sh cannot retire this child" in result["error"]
    assert "cleanup-child-agent.sh QuietCurie can" not in result["error"]
    assert list((runtime / "spawn-tokens").iterdir()) == []


def test_async_killed_cleanup_reports_recovery_credential_via_spawn_status(monkeypatch, tmp_path):
    launcher, runtime = _prepare_real_spawn(monkeypatch, tmp_path, _KILLED_CLEANUP_LAUNCHER)
    spec = _killed_cleanup_spec(launcher, tmp_path, "before-copy")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{httpd.server_port}/api/spawn-status?name=QuietCurie"
    try:
        pending = server.spawn_with_launch_spec(
            {"standalone": True, "name": "QuietCurie", "task": "work",
             "dir": str(tmp_path), "async": True}, spec)
        assert pending["ok"] is True and pending["pending"] is True
        assert pending["verdict_deadline_seconds"] == server._spawn_verdict_deadline_seconds(spec)

        deadline = time.monotonic() + 30
        while True:
            with urllib.request.urlopen(url) as response:
                status = json.loads(response.read())
            if status["state"] != "launching" or time.monotonic() > deadline:
                break
            time.sleep(0.1)
    finally:
        httpd.shutdown()
        thread.join()

    assert status["state"] == "failed", status
    owner = runtime / "agent_token_QuietCurie"
    _assert_owner_installed(owner)
    assert status["result"]["cleanup_incomplete"] is True
    assert status["result"]["recovery_credential"] == str(owner)
    assert status["result"]["cleanup_child_agent_can_recover"] is True
    assert f"installed from the unconsumed one-shot handoff at {owner}" in status["error"]
    assert list((runtime / "spawn-tokens").iterdir()) == []
    assert _group_gone(int((tmp_path / "mark.pid").read_text()))


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_canonical_real_launcher_timeout_still_deletes_credentials_after_5s(monkeypatch, tmp_path, provider):
    launcher, runtime = _prepare_real_spawn(monkeypatch, tmp_path, _KILLED_CLEANUP_LAUNCHER)
    monkeypatch.setattr(server, "SPAWN_SCRIPT", str(launcher))
    monkeypatch.setenv("TEST_RUNTIME", str(runtime))
    monkeypatch.setenv("TEST_MARK", str(tmp_path / "mark"))
    monkeypatch.setenv("TEST_MODE", "copied")
    payload = {"provider": provider}
    if provider == "codex":
        payload["model"] = server._codex_models()[0]

    started = time.monotonic()
    result = server.do_spawn({"standalone": True, "name": "QuietCurie", "task": "work",
                              "dir": str(tmp_path), **payload})
    elapsed = time.monotonic() - started

    # TERM is ignored, so the canonical 5s grace runs out before the KILL.
    assert server._SPAWN_TERMINATE_GRACE_SECONDS <= elapsed < 15
    assert result["ok"] is False
    assert "did not finish readiness checks" in result["error"]
    for key in ("cleanup_incomplete", "recovery_credential", "cleanup_child_agent_can_recover"):
        assert key not in result
    assert "may remain" not in result["error"]
    assert not (runtime / "agent_token_QuietCurie").exists()
    assert list((runtime / "spawn-tokens").iterdir()) == []


def test_credential_state_never_follows_links_and_rejects_oversized_tokens(tmp_path):
    token = "t" * server._AGENT_TOKEN_MAX_CHARS
    exact = tmp_path / "exact"
    exact.write_text(token + "\n")
    assert server._spawn_credential_state(str(exact), token) == "complete"
    big = tmp_path / "big"
    big.write_text(token + "t")
    assert server._spawn_credential_state(str(big), token + "t") == "oversized"
    link = tmp_path / "link"
    link.symlink_to(exact)
    assert server._spawn_credential_state(str(link), token) == "not-regular"
    assert server._promote_spawn_credential(str(link), token, "not-regular") is False
    assert link.is_symlink()
    # A credential that became complete after inspection is not replaced.
    assert server._promote_spawn_credential(str(exact), token, "empty") is False
    assert server._promote_spawn_credential(str(tmp_path / "gone"), token, "missing") is True
    assert (tmp_path / "gone").read_text() == token


# --------------------------------------------------------------------------- #
# Async verdict deadline: server-derived and consumed by the page watcher
# --------------------------------------------------------------------------- #
def test_verdict_deadline_covers_each_launchers_worst_case():
    base = dict(provider="p", program="x", model="m", script="/bin/true")
    canonical = server.SpawnLaunchSpec(**base)
    group = server.SpawnLaunchSpec(**base, signal_process_group=True)
    tuned = server.SpawnLaunchSpec(**base, signal_process_group=True, termination_grace=300)
    for spec in (canonical, group, tuned):
        worst = (server._SPAWN_READINESS_TIMEOUT_SECONDS + server._spawn_termination_grace(spec)
                 + server._SPAWN_KILL_REAP_SECONDS)
        assert server._spawn_verdict_deadline_seconds(spec) >= worst + 2
    assert server._spawn_verdict_deadline_seconds(canonical) == 140
    assert server._spawn_verdict_deadline_seconds(group) == 285
    assert server._spawn_verdict_deadline_seconds(tuned) == 435


def test_async_pending_deadline_comes_from_the_spec_not_the_payload(monkeypatch, tmp_path):
    launcher = tmp_path / "spawn_child.sh"
    launcher.write_text("#!/bin/bash\n")
    launcher.chmod(0o755)
    release = threading.Event()

    class SlowProc:
        def wait(self, timeout=None):
            assert release.wait(timeout=10), "launcher wait was never released"
            return 0

    monkeypatch.setattr(server, "SPAWN_SCRIPT", str(launcher))
    monkeypatch.setattr(server, "RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setattr(server, "HERE", str(tmp_path))
    monkeypatch.setattr(server, "_project_key", lambda: "/project")
    monkeypatch.setattr(server, "_spawn_name_status", lambda _: "available")
    monkeypatch.setattr(server, "_mcp_call", lambda m, a, timeout=15: {"ok": True, "data": {"name": "QuietCurie", "registration_token": "tok"} if m == "register_agent" else {}})
    monkeypatch.setattr(server.subprocess, "Popen", lambda *a, **k: SlowProc())
    monkeypatch.setattr(server.subprocess, "run", lambda *a, **k: type("R", (), {"returncode": 0})())

    spoofed = {"standalone": True, "name": "QuietCurie", "task": "work", "dir": str(tmp_path),
               "async": True, "verdict_deadline_seconds": 99999}
    try:
        for extra in ({"provider": "claude"},
                      {"provider": "codex", "model": server._codex_models()[0]}):
            pending = server.do_spawn({**spoofed, **extra})
            assert pending["pending"] is True
            assert pending["verdict_deadline_seconds"] == 140
        group = server.SpawnLaunchSpec(provider="gemini", program="antigravity", model="m",
                                       script=str(launcher), signal_process_group=True)
        pending = server.spawn_with_launch_spec({**spoofed, "verdict_deadline_seconds": 1}, group)
        assert pending["pending"] is True
        assert pending["verdict_deadline_seconds"] == server._spawn_verdict_deadline_seconds(group)
        assert pending["verdict_deadline_seconds"] >= (
            server._SPAWN_READINESS_TIMEOUT_SECONDS + server._SPAWN_CLEANUP_GRACE_SECONDS
            + server._SPAWN_KILL_REAP_SECONDS)
    finally:
        release.set()
    deadline = time.monotonic() + 5
    while server.spawn_launch_status("QuietCurie")["state"] == "launching" and time.monotonic() < deadline:
        time.sleep(0.05)
    assert server.spawn_launch_status("QuietCurie")["state"] == "ready"


_WATCH_HARNESS = r"""
let now=0;
Date.now=()=>now;
globalThis.setTimeout=(fn,ms)=>{now+=ms||0;Promise.resolve().then(fn);return 0;};
let verdictAt=Infinity,fetches=0;
const toasts=[];
globalThis.fetch=async()=>{fetches++;
  return {json:async()=>({ok:true,state:now>=verdictAt?'ready':'launching'})};};
function toast(title,msg,err){toasts.push([title,msg,!!err]);}
function clearSpawnTaskDraft(){}
function tick(){}
function netTick(){}
let view='deck';
async function run(pending,verdictSeconds){
  now=0;fetches=0;toasts.length=0;verdictAt=verdictSeconds*1000;
  await watchSpawnLaunch('QuietCurie','/repo',spawnWatchLimitMs(pending));
  return {toasts:toasts.slice(),elapsed:now};
}
(async()=>{
  const out={};
  out.groupWorst=await run({verdict_deadline_seconds:GROUP_DEADLINE},GROUP_WORST);
  out.groupWithoutDeadline=await run({},GROUP_WORST);
  out.canonicalWorst=await run({verdict_deadline_seconds:140},CANONICAL_WORST);
  out.canonicalLate=await run({verdict_deadline_seconds:140},150);
  out.legacyLate=await run({},150);
  now=0;toasts.length=0;verdictAt=150000;
  await watchSpawnLaunch('QuietCurie','/repo');
  out.defaultArgument={toasts:toasts.slice(),elapsed:now};
  out.limits={
    group:spawnWatchLimitMs({verdict_deadline_seconds:GROUP_DEADLINE}),
    string:spawnWatchLimitMs({verdict_deadline_seconds:'9999'}),
    boolean:spawnWatchLimitMs({verdict_deadline_seconds:true}),
    negative:spawnWatchLimitMs({verdict_deadline_seconds:-5}),
    nan:spawnWatchLimitMs({verdict_deadline_seconds:NaN}),
    infinite:spawnWatchLimitMs({verdict_deadline_seconds:Infinity}),
    shorter:spawnWatchLimitMs({verdict_deadline_seconds:30}),
    huge:spawnWatchLimitMs({verdict_deadline_seconds:1e9}),
    none:spawnWatchLimitMs(null),
  };
  process.stdout.write(JSON.stringify(out));
})().catch(e=>{console.error(e);process.exit(1);});
"""


def test_rendered_watcher_waits_for_the_server_verdict_deadline():
    if shutil.which("node") is None:
        pytest.skip("node is required to run the dashboard watcher")
    source = (pathlib.Path(server.__file__).parent / "index.html").read_bytes()
    rendered = server._render_dashboard_index(source).decode("utf-8")
    assert "watchSpawnLaunch(j.child_name,payload.dir,spawnWatchLimitMs(j));" in rendered
    parts = []
    for constant in ("SPAWN_WATCH_DEFAULT_MS", "SPAWN_WATCH_MAX_MS"):
        match = re.search(rf"\nconst {constant}=[^;]+;", rendered)
        assert match, constant
        parts.append(match.group(0))
    for header in ("function spawnWatchLimitMs", "async function watchSpawnLaunch"):
        match = re.search(rf"\n{header}\(.*?\)\{{.*?\n\}}", rendered, re.DOTALL)
        assert match, header
        parts.append(match.group(0))

    base = dict(provider="p", program="x", model="m", script="/bin/true")
    group = server.SpawnLaunchSpec(**base, signal_process_group=True)
    group_worst = (server._SPAWN_READINESS_TIMEOUT_SECONDS + server._SPAWN_CLEANUP_GRACE_SECONDS
                   + server._SPAWN_KILL_REAP_SECONDS)
    canonical_worst = (server._SPAWN_READINESS_TIMEOUT_SECONDS + server._SPAWN_TERMINATE_GRACE_SECONDS
                       + server._SPAWN_KILL_REAP_SECONDS)
    harness = (_WATCH_HARNESS
               .replace("GROUP_DEADLINE", str(server._spawn_verdict_deadline_seconds(group)))
               .replace("GROUP_WORST", repr(group_worst))
               .replace("CANONICAL_WORST", repr(canonical_worst)))
    result = _REAL_RUN(["node", "-e", "\n".join(parts) + "\n" + harness],
                       capture_output=True, text=True, timeout=20, check=False)
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout)

    spawned = [["▸ SPAWNED", "> QuietCurie :: tmux session ready", False]]
    timed_out = [["✕ SPAWN", "> QuietCurie :: no verdict after 140s", True]]
    # A process-group launcher's worst-case verdict lands inside its deadline,
    # where the old fixed 140s watch had already given up.
    assert out["groupWorst"]["toasts"] == spawned
    assert out["groupWithoutDeadline"]["toasts"] == timed_out
    # Claude/Codex keep exactly the 140s watch, with or without the field.
    assert out["canonicalWorst"]["toasts"] == spawned
    for key in ("canonicalLate", "legacyLate", "defaultArgument"):
        assert out[key]["toasts"] == timed_out, key
        assert 140000 <= out[key]["elapsed"] < 142100, key
    limits = out["limits"]
    assert limits["group"] == server._spawn_verdict_deadline_seconds(group) * 1000
    for key in ("string", "boolean", "negative", "nan", "infinite", "shorter", "none"):
        assert limits[key] == 140000, key
    assert limits["huge"] == 3600000
