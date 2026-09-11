"""Regression coverage for Dashboard NEW AGENT Antigravity integration."""
from __future__ import annotations

import pathlib

import dashboard.provider_server as server
import dashboard.gemini_provider_runtime as gemini_runtime
import dashboard.service_runner as service_runner


ROOT = pathlib.Path(__file__).resolve().parent.parent


def test_spawn_catalog_exposes_gemini_provider(monkeypatch):
    monkeypatch.setattr(
        server,
        "_spawn_scientist_statuses",
        lambda _adjectives, scientists: {name: "unknown" for name in scientists},
    )
    data = server.spawn_names_payload()
    gemini = next(provider for provider in data["providers"] if provider["id"] == "gemini")
    assert gemini == {
        "id": "gemini",
        "label": "Antigravity",
        "program": "antigravity",
        "models": ["gemini-3.8-flash-high", "gemini-3.8-flash-medium"],
        "default_model": "gemini-3.8-flash-high",
        "efforts": ["low", "medium", "high"],
        "effort_default": "high",
        "provider_key": "google",
        "capabilities": {
            "effort": True,
            "mcp": True,
            "resume": False,
            "runtime": True,
            "transcript": False,
            "standalone": False,
            "worktree_required": True,
            "resources_required": True,
        },
    }


def test_spawn_catalog_honors_gemini_model_override(monkeypatch):
    monkeypatch.setenv("AGENTSTACK_GEMINI_MODELS", "gemini-test-a, gemini-test-b")
    gemini = next(
        provider for provider in server.spawn_names_payload()["providers"]
        if provider["id"] == "gemini"
    )
    assert gemini["models"] == ["gemini-test-a", "gemini-test-b"]
    assert gemini["default_model"] == "gemini-test-a"


def test_rendered_dashboard_adds_capability_driven_resource_controls():
    source = (ROOT / "dashboard" / "index.html").read_bytes()
    rendered = server._render_dashboard_index(source).decode("utf-8")
    assert 'id="spm-resources-row"' in rendered
    assert 'id="spm-resources"' in rendered
    assert "providerCaps.resources_required" in rendered
    assert "providerCaps.worktree_required" in rendered
    assert "payload.resources=resources" in rendered
    assert "const resourcesReady=!providerCaps.resources_required" in rendered
    assert "spmSelectedProvider==='gemini'" not in rendered


def test_gemini_spawn_requires_declared_resources(monkeypatch, tmp_path):
    adapter = tmp_path / "spawn_gemini_preregistered.sh"
    adapter.write_text("#!/bin/bash\n", encoding="utf-8")
    monkeypatch.setattr(server, "HOOKS_DIR", str(tmp_path))
    result = server.do_spawn({
        "parent": "Parent-Curie",
        "task": "inspect the dashboard",
        "dir": str(tmp_path),
        "provider": "gemini",
        "model": "gemini-3.8-flash-high",
        "effort": "high",
    })
    assert result == {"ok": False, "error": "resources required for provider gemini"}


def test_gemini_spawn_rejects_standalone_and_invalid_engine_options(tmp_path):
    common = {
        "parent": "Parent-Curie",
        "task": "inspect the dashboard",
        "dir": str(tmp_path),
        "provider": "gemini",
        "resources": "src/**",
    }
    assert server.do_spawn({**common, "standalone": True}) == {
        "ok": False,
        "error": "standalone not supported for provider gemini",
    }
    bad_model = server.do_spawn({**common, "model": "gemini-not-allowed"})
    assert bad_model["error"] == "model not allowed for provider gemini: gemini-not-allowed"
    bad_effort = server.do_spawn({**common, "effort": "xhigh"})
    assert bad_effort["error"] == "effort not allowed for provider gemini: xhigh"


def test_gemini_spawn_dispatches_existing_preregistered_adapter(monkeypatch, tmp_path):
    adapter = tmp_path / "spawn_gemini_preregistered.sh"
    adapter.write_text("#!/bin/bash\n", encoding="utf-8")
    adapter.chmod(0o755)
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "agent_token_Parent").write_text("parent-owner-token", encoding="utf-8")
    launched: list[tuple[list[str], dict]] = []
    calls = []

    def mcp(method, args, timeout=15):
        calls.append((method, args))
        return {
            "ok": True,
            "data": {
                "name": "SunnyCurie",
                "registration_token": "server-child-token",
            } if method == "register_agent" else {},
        }

    monkeypatch.setattr(server, "HOOKS_DIR", str(tmp_path))
    monkeypatch.setattr(server, "RUNTIME_DIR", str(runtime))
    monkeypatch.setattr(server, "HERE", str(tmp_path))
    monkeypatch.setattr(server, "_project_key", lambda: "/project")
    monkeypatch.setattr(server, "_spawn_name_status", lambda _: "available")
    monkeypatch.setattr(server, "_mcp_call", mcp)
    monkeypatch.setattr(server.time, "sleep", lambda _: None)
    monkeypatch.setattr(
        server.subprocess,
        "Popen",
        lambda args, **kwargs: launched.append((list(args), kwargs)),
    )
    monkeypatch.setattr(
        server.subprocess,
        "run",
        lambda *a, **k: type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})(),
    )

    result = server.do_spawn({
        "parent": "Parent",
        "name": "Sunny-Curie",
        "task": "implement the requested change",
        "dir": str(tmp_path),
        "provider": "gemini",
        "model": "gemini-3.8-flash-high",
        "effort": "medium",
        "resources": "src/**, tests/**",
    })

    assert result["ok"] is True
    assert result["provider"] == "gemini"
    assert result["model"] == "gemini-3.8-flash-high"
    assert result["effort"] == "medium"
    assert result["worktree"] is True
    assert calls[0][1]["program"] == "antigravity"
    assert calls[0][1]["model"] == "gemini-3.8-flash-high"

    args, kwargs = launched[0]
    wrapper = pathlib.Path(args[0])
    wrapper_text = wrapper.read_text(encoding="utf-8")
    assert str(adapter) in wrapper_text
    assert "AGENTSTACK_GEMINI_MODEL=gemini-3.8-flash-high" in wrapper_text
    assert "AGENTSTACK_GEMINI_EFFORT=medium" in wrapper_text
    assert "AGENTSTACK_GEMINI_RESOURCES='src/**,tests/**'" in wrapper_text
    assert "AGENTSTACK_GEMINI_TASK_FILE=" in wrapper_text
    assert "--worktree" in args
    assert "--codex" not in args
    assert kwargs["env"]["PARENT_AGENT"] == "Parent"

    # The asynchronous adapter owns these files after successful handoff.
    for line in wrapper_text.splitlines():
        if line.startswith("export AGENTSTACK_GEMINI_TASK_FILE="):
            task_file = pathlib.Path(line.split("=", 1)[1].strip("'"))
            task_file.unlink(missing_ok=True)
    wrapper.unlink(missing_ok=True)


def test_service_runner_prefers_optional_provider_entrypoint(monkeypatch, tmp_path):
    core = tmp_path / "server.py"
    provider = tmp_path / "provider_server.py"
    core.write_text("# core\n", encoding="utf-8")
    monkeypatch.setattr(service_runner, "HERE", tmp_path)
    assert service_runner._default_server_path() == core
    provider.write_text("# provider\n", encoding="utf-8")
    assert service_runner._default_server_path() == provider


def test_provider_installer_ships_dashboard_extension():
    text = (ROOT / "scripts" / "install-gemini-provider.sh").read_text(encoding="utf-8")
    assert '"dashboard/provider_server.py"' in text
    assert '"dashboard/gemini_provider_runtime.py"' in text
    assert "service runner cannot load optional" in text


def test_gemini_dashboard_adapter_is_shell_parseable():
    import subprocess

    adapter = ROOT / "hooks" / "spawn_gemini_preregistered.sh"
    result = subprocess.run(
        ["bash", "-n", str(adapter)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
