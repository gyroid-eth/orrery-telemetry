"""Claude resume restores the existing Mail identity before creating a terminal."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import subprocess
import threading
import tempfile
import hashlib

import pytest
from fastmcp import Client
from agentstack_mail import app, config, db
from dashboard import server
from hooks import child_resume

ROOT = Path(__file__).resolve().parents[1]
NAME = "BlueCurie"
TOKEN = "fixture-owner-token"
SID = "01d13f58-8e1a-7777-a3d8-e2ba243cdb49"


def private(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(0o600)


@pytest.fixture
def resume(monkeypatch, tmp_path):
    runtime = tmp_path / "runtime"
    transcript = tmp_path / f"{SID}.jsonl"
    transcript.write_text(json.dumps({"cwd": str(tmp_path)}), encoding="utf-8")
    cli = tmp_path / "claude"
    cli.write_text("#!/bin/bash\nexit 0\n", encoding="utf-8")
    cli.chmod(0o700)
    monkeypatch.setenv("AGENTSTACK_MCP_PROXY", str(cli))
    monkeypatch.setenv("AGENTSTACK_CLAUDE_JSON", str(tmp_path / "missing-claude.json"))
    registration = {"agent_id": 73, "agent_name": NAME, "project_key": str(tmp_path),
                    "program": "claude-code", "model": "fixture-model", "task_description": "fixture"}
    monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
    monkeypatch.setattr(server, "RUNTIME_DIR", str(runtime))
    monkeypatch.setattr(server, "HOOKS_DIR", str(ROOT / "hooks"))
    monkeypatch.setattr(server, "ABS_CLAUDE", str(cli))
    monkeypatch.setattr(server, "_agent_program", lambda _n: "claude-code")
    monkeypatch.setattr(server, "_claude_registration", lambda _n: registration)
    monkeypatch.setattr(server, "_transcript_path", lambda _n: str(transcript))
    monkeypatch.setattr(server, "_transcript_cwd", lambda _p: str(tmp_path))
    monkeypatch.setattr(server, "_terminal_adapter", lambda: "fixture")
    launches, calls = [], []
    monkeypatch.setattr(server, "_open_terminal_tmux", lambda argv, **kw: launches.append(argv) or {"ok": True, "adapter": "fixture"})

    def call(method, args):
        calls.append((method, args))
        data = {"id": registration["agent_id"], "name": NAME} if method == "register_agent" else {
            "status": "active", "agent_name": NAME, "project_key": registration["project_key"],
        }
        return {"ok": True, "data": data}

    monkeypatch.setattr(server, "_mcp_call", call)
    private(runtime / f"agent_token_{NAME}", TOKEN)
    return runtime, registration, launches, calls


def child(runtime, registration):
    state = {**registration, "registration_token": TOKEN}
    private(runtime / "child-agents" / f"{NAME}.json", json.dumps(state))
    child_resume.prepare_active_state(runtime, NAME, project_key=registration["project_key"])
    assert child_resume.mark_retired(runtime, NAME, retention_days=30)


@pytest.mark.parametrize("is_child", [False, True])
def test_resume_restores_mail_before_terminal_and_keeps_token_private(resume, is_child):
    runtime, registration, launches, calls = resume
    if is_child:
        child(runtime, registration)
    result = server.do_resume(NAME)
    assert result["ok"], result
    assert [method for method, _ in calls] == ["register_agent", "unretire_agent"]
    assert calls[0][1]["registration_token"] == TOKEN
    assert calls[0][1]["model"] == "fixture-model"
    assert TOKEN not in json.dumps(launches) + json.dumps(result)
    assert "AGENTSTACK_RESERVED_IDENTITY=1" in launches[0][-1]
    assert ("cleanup-child-agent.sh" in launches[0][-1]) is is_child
    if is_child:
        config_path = runtime / "child-agents" / f"{NAME}.mcp.json"
        config = json.loads(config_path.read_text())
        assert config_path.stat().st_mode & 0o777 == 0o600
        assert config["mcpServers"]["orrery-mail"]["env"]["AGENTSTACK_PROXY_TOKEN_FILE"] == str(runtime / f"agent_token_{NAME}")
        assert "--strict-mcp-config" in launches[0][-1]
        state = json.loads((runtime / "child-agents" / f"{NAME}.json").read_text())
        assert state["resume_in_progress_at"]
        assert child_resume.purge_expired(runtime) == []


@pytest.mark.parametrize("failure,code", [("missing", "credential_missing"), ("expired", "retention_expired"),
                                            ("purged", "purged"), ("mismatch", "identity_mismatch"),
                                            ("permissions", "credential_permission")])
def test_unavailable_credentials_never_launch(resume, failure, code):
    runtime, registration, launches, calls = resume
    child(runtime, registration)
    token = runtime / f"agent_token_{NAME}"
    if failure == "missing":
        token.unlink()
    elif failure == "expired":
        state_path = runtime / "child-agents" / f"{NAME}.json"
        state = json.loads(state_path.read_text())
        state["resume_expires_at"] = "2000-01-01T00:00:00Z"
        private(state_path, json.dumps(state))
    elif failure == "purged":
        assert child_resume.purge_one(runtime, NAME, reason="purged")
    elif failure == "mismatch":
        private(token, "another-owner")
    else:
        token.chmod(0o644)
    assert server._resume_capability(NAME, "claude-code", category="retired") == code
    result = server.do_resume(NAME)
    assert result["resume_capability"] == code
    assert not launches and not calls


@pytest.mark.parametrize("method", ["register_agent", "unretire_agent"])
def test_mail_failure_does_not_launch_and_restores_retention_marker(resume, monkeypatch, method):
    runtime, registration, launches, _ = resume
    child(runtime, registration)
    original = server._mcp_call
    monkeypatch.setattr(server, "_mcp_call", lambda name, args: {"ok": False, "error": TOKEN} if name == method else original(name, args))
    result = server.do_resume(NAME)
    assert not result["ok"] and TOKEN not in json.dumps(result)
    assert not launches
    state = child_resume.inspect_retained(runtime, NAME, agent_id=73,
                                         project_key=registration["project_key"], program="claude-code")
    assert "resume_in_progress_at" not in state


def test_top_level_missing_token_does_not_launch(resume):
    runtime, _, launches, calls = resume
    (runtime / f"agent_token_{NAME}").unlink()
    assert server._resume_capability(NAME, "claude", category="retired", verify_transcript=False) == "credential_missing"
    assert server.do_resume(NAME)["resume_capability"] == "credential_missing"
    assert not launches and not calls


@pytest.mark.parametrize("is_child", [False, True])
def test_real_mail_cleanup_resume_receive_and_reply(resume, monkeypatch, tmp_path, is_child):
    """Use the bundled Mail service in memory, never the installed service."""
    runtime, registration, launches, _ = resume
    monkeypatch.setenv("AGENTSTACK_MAIL_ENV_FILE", str(tmp_path / "missing.env"))
    monkeypatch.setenv("AGENTSTACK_MAIL_DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'fixture.sqlite3'}")
    monkeypatch.setenv("AGENTSTACK_MAIL_STORAGE_ROOT", str(tmp_path / "archive"))
    monkeypatch.setenv("AGENTSTACK_MAIL_NOTIFICATIONS_SIGNALS_DIR", str(tmp_path / "signals"))
    management = Path(tempfile.gettempdir()) / f"claude-resume-tests-{os.getpid()}"
    management.mkdir(mode=0o700, exist_ok=True)
    socket_id = hashlib.sha256(os.fsencode(tmp_path)).hexdigest()[:12]
    monkeypatch.setenv("AGENTSTACK_MAIL_MANAGEMENT_SOCKET", str(management / f"{socket_id}.sock"))
    monkeypatch.setenv("AGENTSTACK_MAIL_AGENT_NAME_ENFORCEMENT_MODE", "passthrough")
    monkeypatch.setenv("AGENTSTACK_MAIL_TOOLS_LOG_ENABLED", "false")
    monkeypatch.setenv("AGENTSTACK_MAIL_NOTIFICATIONS_ENABLED", "false")
    db.reset_database_state()
    config.clear_settings_cache()
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    client = Client(app.build_mcp_server())

    def run(awaitable):
        return asyncio.run_coroutine_threadsafe(awaitable, loop).result(timeout=30)

    def call(method, args):
        result = run(client.call_tool(method, args, raise_on_error=False))
        data = result.structured_content or result.data
        while isinstance(data, dict) and set(data) == {"result"}:
            data = data["result"]
        return {"ok": not result.is_error, "data": data, "error": str(result.content) if result.is_error else ""}

    connected = False
    try:
        run(client.__aenter__())
        connected = True
        project = registration["project_key"]
        assert call("ensure_project", {"human_key": project})["ok"]
        for name, token in [("GreenBohr", "parent-owner-token"), (NAME, TOKEN)]:
            response = call("register_agent", {"project_key": project, "name": name,
                "program": "claude-code", "model": "fixture-model", "registration_token": token})
            assert response["ok"], response
            if name == NAME:
                registration["agent_id"] = response["data"]["id"]
            assert call("set_contact_policy", {"project_key": project, "agent_name": name, "policy": "open"})["ok"]
        assert call("retire_agent", {"project_key": project, "agent_name": NAME})["ok"]
        if is_child:
            state = {**registration, "registration_token": TOKEN}
            private(runtime / "child-agents" / f"{NAME}.json", json.dumps(state))
            child_resume.prepare_active_state(runtime, NAME, project_key=project)
            # Exercise the actual normal cleanup chain with disposable files.
            cleaned = subprocess.run(["/bin/bash", str(ROOT / "hooks" / "cleanup-child-agent.sh"), NAME],
                env={**os.environ, "AGENTSTACK_RUNTIME_DIR": str(runtime), "AGENTSTACK_HOOKS_DIR": str(ROOT / "hooks"),
                     "AGENTSTACK_MCP_URL": "http://127.0.0.1:1/mcp", "AGENTSTACK_MAIL_HTTP_BEARER_MODE": "disabled",
                     "AGENTSTACK_CHILD_RESUME_RETENTION_DAYS": "30"}, capture_output=True, text=True, timeout=15)
            assert cleaned.returncode == 0, cleaned.stderr
            assert (runtime / f"agent_token_{NAME}").stat().st_mode & 0o777 == 0o600
            assert json.loads((runtime / "child-agents" / f"{NAME}.json").read_text())["retired_at"]
        monkeypatch.setattr(server, "_mcp_call", call)
        result = server.do_resume(NAME)
        assert result["ok"], result

        async def retired_at():
            agent = await app._get_agent(await app._get_project_by_identifier(project), NAME)
            return agent.retired_at

        assert run(retired_at()) is None
        question = call("send_message", {"project_key": project, "sender_name": "GreenBohr", "sender_token": "parent-owner-token",
                                         "to": [NAME], "subject": "question", "body_md": "test question"})
        assert question["ok"], question
        inbox = call("fetch_inbox", {"project_key": project, "agent_name": NAME})
        assert inbox["ok"] and inbox["data"], inbox
        reply = call("send_message", {"project_key": project, "sender_name": NAME, "sender_token": TOKEN,
                                      "to": ["GreenBohr"], "subject": "reply", "body_md": "test reply"})
        assert reply["ok"], reply
        assert launches
    finally:
        if connected:
            run(client.__aexit__(None, None, None))
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=5)
        loop.close()
        db.reset_database_state()
        config.clear_settings_cache()


@pytest.mark.parametrize("unretire_ok", [True, False])
def test_finished_claude_shell_is_replaced_only_after_mail_restore(resume, monkeypatch, unretire_ok):
    _, _, launches, calls = resume
    monkeypatch.setattr(server, "_has_session", lambda _n: True)
    monkeypatch.setattr(server, "build_agents", lambda _days: [{"name": NAME, "category": "finished"}])
    original = server._mcp_call
    if not unretire_ok:
        monkeypatch.setattr(server, "_mcp_call", lambda method, args: {"ok": False} if method == "unretire_agent" else original(method, args))
    kills = []
    def run(argv, **kw):
        assert calls[0][0] == "register_agent"
        assert calls[1][0] == "unretire_agent"
        kills.append(argv)
        return type("Result", (), {"returncode": 0})()
    monkeypatch.setattr(server.subprocess, "run", run)
    result = server.do_jump(NAME)
    assert result["ok"] is unretire_ok
    assert bool(kills) is unretire_ok
    assert bool(launches) is unretire_ok


def test_explicit_purge_can_remove_a_child_with_an_unrestorable_codex_profile(tmp_path):
    runtime = tmp_path / "runtime"
    state = {"schema_version": 1, "launch_origin": "child", "provider": "codex",
             "agent_id": 73, "agent_name": NAME, "project_key": str(tmp_path),
             "program": "codex", "codex_mcp_profile": "obsolete", "registration_token": TOKEN}
    private(runtime / "child-agents" / f"{NAME}.json", json.dumps(state))
    private(runtime / f"agent_token_{NAME}", TOKEN)
    assert child_resume.purge_one(runtime, NAME, reason="purged")
    assert not (runtime / f"agent_token_{NAME}").exists()
