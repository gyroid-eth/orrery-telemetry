"""HTTP and side-effect regressions for spawn previews (#131)."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

import dashboard.server as server
import dashboard.provider_server as provider_server


def forbidden(*args, **kwargs):
    pytest.fail(f"dry run attempted a side effect: {args!r}")


@pytest.fixture
def preview_env(monkeypatch, tmp_path):
    script = tmp_path / "spawn.sh"
    script.write_text("#!/bin/bash\n")
    for module in (server, provider_server):
        monkeypatch.setattr(module, "SPAWN_SCRIPT", str(script))
        monkeypatch.setattr(module, "RUNTIME_DIR", str(tmp_path / "runtime"))
        monkeypatch.setattr(module, "HERE", str(tmp_path / "dashboard"))
        monkeypatch.setattr(module, "_project_key", lambda: "/project")
        monkeypatch.setattr(module, "_spawn_name_status", lambda _: "available")
        for name in ("_mcp_call", "_runtime_agent_token", "_suggest_any_spawn_name",
                     "_write_annotation", "_spawn_launch_record"):
            monkeypatch.setattr(module, name, forbidden)
    monkeypatch.setattr(server.subprocess, "Popen", forbidden)
    monkeypatch.delenv("AGENTSTACK_CLAUDE_MODELS", raising=False)
    monkeypatch.delenv("AGENTSTACK_CODEX_MODELS", raising=False)
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    return tmp_path, str(script)


@pytest.mark.parametrize("module", [server, provider_server])
@pytest.mark.parametrize("standalone", [False, True])
def test_claude_preview_has_no_agent_or_filesystem_side_effects(
        monkeypatch, preview_env, module, standalone):
    directory, script = preview_env
    before = sorted(directory.rglob("*"))
    payload = {
        "parent": "Parent", "standalone": standalone, "task": "x" * 4100,
        "dir": str(directory), "dry_run": True, "async": True,
        "worktree": True, "worktree_base": "origin/master", "role": "review",
        "group": "release", "claude_chrome_device": "browser:1",
    }
    result = module.do_spawn(payload)
    assert result == {
        "ok": True, "dry_run": True, "provider": "claude",
        "model": "claude-opus-5-5", "effort": "", "dir": str(directory),
        "standalone": standalone, "worktree": True,
        "argv": [script, "--pre-registered", "<child-name>", "--child-token-file",
                 "<child-token-file>", *(["--standalone"] if standalone else []),
                 "--claude-chrome-device", "browser:1", "--model", "claude-opus-5-5",
                 "--worktree", "--worktree-base", "origin/master",
                 "x" * (4000 if standalone else 80), str(directory)],
        "launcher_env": {},
    }
    assert sorted(directory.rglob("*")) == before


@pytest.mark.parametrize("version,model", [((0, 158, 0), "gpt-6-sol"),
                                          ((0, 159, 1), "gpt-6.1-sol")])
@pytest.mark.parametrize("requested_model", ["", "sol"])
def test_codex_preview_uses_the_selected_cli_policy(
        monkeypatch, preview_env, version, model, requested_model):
    directory, script = preview_env
    monkeypatch.setattr(server.codex_models, "resolve_launcher", lambda:
                        server.codex_models.LauncherPolicy(binary="/selected/codex", version=version))
    result = server.do_spawn({
        "standalone": True, "name": "Sunny-Curie", "task": "review",
        "dir": str(directory), "provider": "codex", "model": requested_model,
        "effort": "high", "dry_run": True,
    })
    assert result["ok"] is True and result["dry_run"] is True
    assert result["provider"] == "codex" and result["model"] == model
    assert result["effort"] == "high" and result["dir"] == str(directory)
    assert result["argv"] == [script, "--pre-registered", "Sunny-Curie", "--child-token-file",
                              "<child-token-file>", "--standalone", "--codex", "--model",
                              model, "--effort", "high", "review", str(directory)]
    assert result["launcher_env"] == {"AGENTSTACK_CODEX_BIN": "/selected/codex"}
    assert not (directory / "runtime").exists()


@pytest.mark.parametrize("module", [server, provider_server])
@pytest.mark.parametrize("age", [0, 3600])
@pytest.mark.parametrize("requested", ["", "sol", "gpt-6.1-sol"])
def test_codex_preview_cache_fallback_and_notice(monkeypatch, preview_env, module, age, requested):
    directory, _ = preview_env
    cache_home = directory / "codex"
    cache_home.mkdir()
    fetched = (datetime.now(timezone.utc) - timedelta(seconds=age)).isoformat()
    (cache_home / "models_cache.json").write_text(json.dumps({
        "fetched_at": fetched, "identity": "opaque-account",
        "models": [{"slug": "gpt-6-luna"}],
    }))
    monkeypatch.setattr(module.codex_models, "resolve_launcher", lambda:
                        module.codex_models.LauncherPolicy(binary="/selected/codex", version=(0, 160, 1)))
    result = module.do_spawn({"standalone": True, "task": "review", "dir": str(directory),
                              "provider": "codex", "model": requested, "dry_run": True})
    assert result["ok"] is True
    expected = "gpt-6.1-sol" if requested.startswith("gpt-") else "gpt-6-luna"
    assert result["model"] == expected
    assert result["argv"][result["argv"].index("--model") + 1] == expected
    if requested.startswith("gpt-"):
        assert "model_note" not in result
    else:
        assert "from gpt-6.1-sol to gpt-6-luna" in result["model_note"]
        if age:
            assert fetched in result["model_note"]
            assert "Start codex once" in result["model_note"]


@pytest.fixture
def spawn_http():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), provider_server.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()

    def post(payload):
        request = urllib.request.Request(
            f"http://127.0.0.1:{httpd.server_port}/api/spawn",
            data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
        try:
            response = urllib.request.urlopen(request)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            return response.code, json.loads(response.read())

    try:
        yield post
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join()


@pytest.mark.parametrize("provider", ["claude", "codex", "gemini"])
@pytest.mark.parametrize("dry_run", [False, True])
def test_unknown_fields_are_http_400_before_model_resolution(
        monkeypatch, preview_env, spawn_http, provider, dry_run):
    directory, _ = preview_env
    monkeypatch.setattr(server.codex_models, "resolve_launcher", forbidden)
    code, result = spawn_http({"standalone": True, "task": "dry", "dir": str(directory),
                               "provider": provider, "dry_run": dry_run, "headles": True,
                               "typo": 1})
    assert code == 400
    assert result == {"ok": False, "error": "unknown spawn fields: headles, typo"}


@pytest.mark.parametrize("invalid", ["true", 1, None, []])
def test_invalid_dry_run_is_http_400(preview_env, spawn_http, invalid):
    directory, _ = preview_env
    code, result = spawn_http({"standalone": True, "task": "dry", "dir": str(directory),
                               "dry_run": invalid})
    assert code == 400
    assert result == {"ok": False, "error": "dry_run must be boolean"}


def test_preview_is_http_200(preview_env, spawn_http):
    directory, _ = preview_env
    code, result = spawn_http({"standalone": True, "task": "dry", "dir": str(directory),
                               "dry_run": True})
    assert code == 200 and result["dry_run"] is True
    assert "child_name" not in result and "pending" not in result


def test_invalid_preview_still_validates_directory(preview_env):
    directory, _ = preview_env
    result = server.do_spawn({"standalone": True, "task": "dry", "dir": str(directory / "absent"),
                              "dry_run": True})
    assert result["ok"] is False and "dir does not exist" in result["error"]


def test_preview_does_not_delete_existing_handoff(preview_env):
    directory, script = preview_env
    handoff = directory / "handoff"
    handoff.write_text("retained")
    result = server.spawn_with_launch_spec(
        {"standalone": True, "task": "dry", "dir": str(directory), "dry_run": True},
        server.SpawnLaunchSpec(provider="claude", program="claude-code", model="claude-opus-5-5",
                               script=script, handoff_paths=(str(handoff),)))
    assert result["dry_run"] is True
    assert handoff.read_text() == "retained"


@pytest.mark.parametrize("headless", [False, True])
def test_cockpit_fifteen_field_payload_is_accepted(preview_env, spawn_http, headless):
    directory, _ = preview_env
    payload = {
        "provider": "claude", "model": "claude-sonnet-5", "effort": None,
        "dir": str(directory), "task": "check cockpit", "name": None,
        "parent": None, "standalone": True, "headless": headless,
        "worktree": False, "worktree_base": "", "role": "review",
        "emoji": "🔎", "group": "release", "async": True,
    }
    assert len(payload) == 15
    code, result = spawn_http({**payload, "dry_run": True})
    assert code == 200 and result["dry_run"] is True
    assert result["model"] == "claude-sonnet-5"
    assert result["launcher_env"]["AGENTSTACK_AUTO_OPEN_CHILD"] == ("0" if headless else "1")


@pytest.mark.parametrize("field,value,message", [
    ("headless", "true", "headless must be boolean"),
    ("emoji", [], "emoji must be a string"),
])
def test_invalid_cockpit_fields_are_http_400(preview_env, spawn_http, field, value, message):
    directory, _ = preview_env
    code, result = spawn_http({"standalone": True, "task": "dry", "dir": str(directory),
                               "dry_run": True, field: value})
    assert code == 400 and result == {"ok": False, "error": message}
