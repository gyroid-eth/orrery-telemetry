"""Tool replacement through the existing resume path, with private fixtures."""
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from dashboard import server
from hooks import child_resume, child_tools, claude_chrome_policy

NAME = "ToolsChild"
SID = "01234567-1234-1234-1234-012345678901"
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(params=["codex", "claude"])
def stopped(request, tmp_path, monkeypatch):
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    directory = runtime / "child-agents"
    directory.mkdir(mode=0o700)
    project = tmp_path / "project"
    project.mkdir()
    state = {"schema_version": 1, "launch_origin": "child",
             "provider": request.param, "program": request.param,
             "agent_name": NAME, "agent_id": 7, "project_key": str(project),
             "registration_token": "fixture-owner", "retired_at": "2026-10-01T00:00:00Z",
             "resume_expires_at": "2099-01-01T00:00:00Z"}
    if request.param == "codex":
        state["codex_mcp_profile"] = "orrery-only"
    child_resume._atomic_json(directory / f"{NAME}.json", state)
    child_resume._atomic_bytes(runtime / f"agent_token_{NAME}", b"fixture-owner")
    monkeypatch.setattr(server, "RUNTIME_DIR", str(runtime))
    monkeypatch.setattr(server, "_child_resume_module", lambda: child_resume)
    monkeypatch.setattr(server, "_child_tools_module", lambda: child_tools)
    monkeypatch.setattr(server, "_claude_chrome_policy_module", lambda: claude_chrome_policy)
    monkeypatch.setattr(server, "_agent_program", lambda name: request.param)
    monkeypatch.setattr(server, "_has_session", lambda name: False)
    monkeypatch.setattr(server, "_child_session_live", lambda name, boot: False)
    monkeypatch.setattr(server, "_resume_capability", lambda *a, **k: "ready")
    registration = {k: state[k] for k in ["agent_id", "project_key", "program"]}
    transcript = project / f"{SID}.jsonl"
    transcript.write_text("")
    source = tmp_path / "source"
    source.mkdir()
    source.joinpath("config.toml").write_text('[mcp_servers.reader]\ncommand="/bin/true"\n')
    monkeypatch.setenv("CODEX_HOME", str(source))
    settings = tmp_path / "claude.json"
    settings.write_text(json.dumps({"mcpServers": {"reader": {"command": "/bin/true"}}}))
    monkeypatch.setenv("AGENTSTACK_CLAUDE_JSON", str(settings))
    monkeypatch.setattr(server, "_codex_transcript_path", lambda name: str(transcript))
    monkeypatch.setattr(server, "_codex_meta", lambda path: (SID, str(project)))
    monkeypatch.setattr(server, "_codex_registration", lambda name: registration)
    monkeypatch.setattr(server, "_codex_resume_provenance", lambda *a, **k: ({"launch_origin": "child"}, ""))
    monkeypatch.setattr(server, "_transcript_path", lambda name: str(transcript))
    monkeypatch.setattr(server, "_transcript_cwd", lambda path: str(project))
    monkeypatch.setattr(server, "_claude_conversation_reason", lambda name: (None, ""))
    monkeypatch.setattr(server, "_claude_resume_material", lambda name: (registration, "fixture-owner",
        child_resume.inspect_retained(runtime, NAME, **registration)))
    old = child_tools.normalize("mail-only", {"mcp": ["reader"]})
    if request.param == "codex":
        record = directory / f"{NAME}.tools.json"
        child_tools.write_codex_record(str(record), old)
    else:
        record = directory / f"{NAME}.claude-launch.{SID}.json"
        claude_chrome_policy._write(str(record), {"version": 3, "agent_name": NAME,
            "launch_id": "old-launch-id", "session_id": SID, "claude_chrome": False,
            "chrome_device": "", "standalone": False, **old})
    home = directory / f"{NAME}.codex-home"
    home.mkdir()
    home.joinpath("old-config").write_text("old")
    target = tmp_path / "never-delete"
    target.write_text("preserve")
    home.joinpath("auth-fixture").symlink_to(target)
    child_resume._atomic_bytes(directory / f"{NAME}.mcp.json", b"old-generated")
    return runtime, record, request.param, state


@pytest.mark.parametrize("selection", [{}, {"mcp": ["reader"]}])
def test_replaces_whole_selection_and_next_resume_keeps_it(stopped, monkeypatch, selection):
    runtime, record, provider, old = stopped
    calls = []
    def resume(name, **kwargs):
        calls.append((name, dict(kwargs), json.loads(record.read_text())))
        state = child_resume.inspect_retained(runtime, name,
            agent_id=7, project_key=old["project_key"], program=provider)
        assert state["tools_change_generation"]
        return {"ok": True, "action": "resumed"}
    monkeypatch.setattr(server, "do_resume", resume)
    result = server.do_jump(NAME, tools=selection, open_terminal=False)
    assert result["ok"], result
    assert result["tools_changed"]
    assert result["base"] == "mail-only"
    assert result["tools"] == selection
    assert calls[0][0] == NAME
    assert calls[0][1] == {"open_terminal": False}
    latest = json.loads(record.read_text())
    assert latest["tools"] == selection
    if provider == "claude":
        assert latest["session_id"] == SID
        assert latest["version"] == 4
        assert server._claude_child_tools(NAME, SID)[0] == {"base": "mail-only", "tools": selection}
    assert "tools_change_generation" not in child_resume._load_state(runtime / "child-agents" / f"{NAME}.json")
    # Ordinary resume bypasses the tools-changing operation and retains bytes.
    raw = record.read_bytes()
    monkeypatch.setattr(server, "do_resume", lambda *a, **k: {"ok": True, "action": "resumed"})
    assert server.do_jump(NAME)["ok"]
    assert record.read_bytes() == raw


def test_failed_launch_restores_record_profile_and_generated_settings(stopped, monkeypatch):
    runtime, record, provider, _ = stopped
    original = record.read_bytes()
    directory = runtime / "child-agents"
    def fail(*a, **k):
        (directory / f"{NAME}.codex-home").mkdir()
        child_resume._atomic_bytes(directory / f"{NAME}.mcp.json", b"new")
        return {"ok": False, "error": "fixture launch failure"}
    monkeypatch.setattr(server, "do_resume", fail)
    result = server.do_jump(NAME, base="default", tools={})
    assert not result["ok"]
    assert record.read_bytes() == original
    assert (directory / f"{NAME}.codex-home" / "old-config").read_text() == "old"
    assert (directory / f"{NAME}.mcp.json").read_bytes() == b"old-generated"
    assert (runtime.parent / "never-delete").read_text() == "preserve"
    if provider == "codex":
        assert child_resume._load_state(directory / f"{NAME}.json")["codex_mcp_profile"] == "orrery-only"


def test_codex_replacement_and_following_resume_require_new_server(stopped, monkeypatch):
    runtime, record, provider, old = stopped
    if provider != "codex":
        pytest.skip("Codex generated config only")
    source = Path(os.environ["CODEX_HOME"])
    config_text = ('[mcp_servers.reader]\ncommand="/bin/true"\n'
                   '[mcp_servers.writer]\ncommand="/bin/true"\nstartup_timeout_sec=120\n')
    source.joinpath("config.toml").write_text(config_text)
    runner = runtime.parent / "run-mcp.sh"
    runner.write_text("#!/bin/sh\nexit 0\n")
    runner.chmod(0o700)
    configs = []

    def resume(name, **kwargs):
        home = child_resume.build_home(
            home=runtime / "child-agents" / f"{name}.codex-home", source=source,
            runner=runner, child=name, project_key=old["project_key"],
            token_file=runtime / f"agent_token_{name}", mcp_url="http://localhost:1/mcp",
            mail_env="", runtime_dir=runtime, bearer_mode="auto", python_bin="",
            mcp_profile="orrery-only")
        configs.append(child_resume._toml().loads(home.joinpath("config.toml").read_text()))
        return {"ok": True, "action": "resumed"}

    monkeypatch.setattr(server, "do_resume", resume)
    assert server.do_jump(NAME, tools={"mcp": ["writer"]})["ok"]
    assert server.do_jump(NAME)["ok"]
    for config in configs:
        servers = config["mcp_servers"]
        assert servers["writer"]["required"] is True
        assert servers["writer"]["startup_timeout_sec"] == 120
        assert servers["reader"]["enabled"] is False
        assert "required" not in servers["reader"]
        assert "required" not in servers["agentstack"]
    assert source.joinpath("config.toml").read_text() == config_text


@pytest.mark.parametrize("options", [{"tools": None}, {"tools": []}, {"base": None},
    {"base": "unknown"}, {"tools": {"shell": True}}, {"tools": {"mcp": ["missing"]}}])
def test_invalid_requests_do_not_mutate_or_launch(stopped, monkeypatch, options):
    runtime, record, _, _ = stopped
    original = record.read_bytes()
    monkeypatch.setattr(server, "do_resume", lambda *a, **k: pytest.fail("must not launch"))
    assert not server.do_jump(NAME, **options)["ok"]
    assert record.read_bytes() == original
    assert not list((runtime / "child-agents").glob("*.tools-change.json"))


@pytest.mark.parametrize("category", ["working", "waiting", None])
def test_running_or_unknown_child_is_rejected(stopped, monkeypatch, category):
    _, record, _, _ = stopped
    original = record.read_bytes()
    monkeypatch.setattr(server, "_has_session", lambda name: True)
    monkeypatch.setattr(server, "_child_session_live", lambda name, boot: True)
    monkeypatch.setattr(server, "lookup_agent", lambda name: {"category": category})
    assert not server.do_jump(NAME, tools={})["ok"]
    assert record.read_bytes() == original


def test_missing_retained_state_cannot_change_tools(stopped):
    runtime, record, _, _ = stopped
    original = record.read_bytes()
    (runtime / "child-agents" / f"{NAME}.json").unlink()
    assert not server.do_jump(NAME, tools={})["ok"]
    assert record.read_bytes() == original


def test_pending_change_excludes_other_resumes_purge_and_wrong_rollback(stopped):
    runtime, record, provider, old = stopped
    candidate = json.loads(record.read_text())
    candidate["tools"] = {}
    generation = child_resume.begin_tools_change(runtime, NAME, old, record, candidate, None)
    with pytest.raises(child_resume.ResumeStateError, match="pending"):
        child_resume.inspect_retained(runtime, NAME, agent_id=7,
                                      project_key=old["project_key"], program=provider)
    assert not child_resume.purge_one(runtime, NAME, reason="purged")
    with pytest.raises(child_resume.ResumeStateError, match="generation"):
        child_resume.finish_tools_change(runtime, NAME, "0" * 32, commit=False)
    child_resume.finish_tools_change(runtime, NAME, generation, commit=False)


def test_new_registration_is_not_overwritten_by_old_change(stopped):
    runtime, record, _, old = stopped
    generation = child_resume.begin_tools_change(runtime, NAME, old, record,
                                                 json.loads(record.read_text()), None)
    state_path = runtime / "child-agents" / f"{NAME}.json"
    newer = {**old, "agent_id": 8}
    child_resume._atomic_json(state_path, newer)
    with pytest.raises(child_resume.ResumeStateError, match="generation"):
        child_resume.finish_tools_change(runtime, NAME, generation, commit=False)
    assert child_resume._load_state(state_path)["agent_id"] == 8


def test_cli_uses_api_full_replacement_and_surfaces_failure(tmp_path):
    received = []
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            received.append((self.path, json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b'{"ok":false,"error":"exit first"}')
        def log_message(self, *args):
            pass
    http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=http.serve_forever, daemon=True)
    thread.start()
    try:
        result = subprocess.run([sys.executable, str(ROOT / "bin/agentstack-resume"), NAME,
            "--base", "mail-only", "--clear-tools", "--detached"],
            env={**os.environ, "AGENTSTACK_PORT": str(http.server_port)},
            capture_output=True, text=True, timeout=15)
        assert result.returncode == 1
        assert json.loads(result.stdout)["error"] == "exit first"
        assert received == [("/api/jump", {"session": NAME, "base": "mail-only", "tools": {}, "open": False})]
    finally:
        http.shutdown()
        http.server_close()
        thread.join(timeout=5)


@pytest.mark.parametrize("body, options", [
    ({"session": NAME, "tools": {}}, {"tools": {}}),
    ({"session": NAME, "base": "mail-only", "tools": {"browser": True}, "open": False},
     {"base": "mail-only", "tools": {"browser": True}, "open_terminal": False}),
])
def test_http_forwards_only_explicit_tools_fields(monkeypatch, body, options):
    calls = []
    monkeypatch.setattr(server, "do_jump", lambda name, **kwargs:
        calls.append((name, kwargs)) or {"ok": True})
    http = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    thread = threading.Thread(target=http.serve_forever, daemon=True)
    thread.start()
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{http.server_port}/api/jump",
            data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as response:
            assert response.status == 200
        assert calls == [(NAME, options)]
    finally:
        http.shutdown()
        http.server_close()
        thread.join(timeout=5)


def test_empty_default_claude_replacement_is_restored_on_next_resume(stopped, monkeypatch):
    _, _, provider, _ = stopped
    monkeypatch.setattr(server, "do_resume", lambda *a, **k: {"ok": True, "action": "resumed"})
    result = server.do_jump(NAME, base="default", tools={})
    assert result["ok"], result
    if provider == "claude":
        assert server._claude_child_tools(NAME, SID)[0] == {"base": "default", "tools": {}}


def test_generation_lock_serializes_requests(stopped):
    runtime, _, _, _ = stopped
    entered = threading.Event()
    acquired = threading.Event()
    def other():
        entered.set()
        with child_resume.tools_change_lock(runtime, NAME):
            acquired.set()
    with child_resume.tools_change_lock(runtime, NAME):
        thread = threading.Thread(target=other)
        thread.start()
        assert entered.wait(2)
        assert not acquired.wait(0.05)
    assert acquired.wait(2)
    thread.join(timeout=2)


def test_failed_staging_restores_generated_config(stopped, monkeypatch):
    runtime, record, _, state = stopped
    original = record.read_bytes()
    real = child_resume._atomic_json
    def fail(path, data):
        if path == record:
            raise OSError("fixture write failure")
        real(path, data)
    monkeypatch.setattr(child_resume, "_atomic_json", fail)
    with pytest.raises(OSError, match="fixture"):
        child_resume.begin_tools_change(runtime, NAME, state, record, {}, None)
    assert record.read_bytes() == original
    assert (runtime / "child-agents" / f"{NAME}.codex-home" / "old-config").read_text() == "old"
    assert "tools_change_generation" not in child_resume._load_state(runtime / "child-agents" / f"{NAME}.json")



def test_oversize_selection_is_refused_before_staging(stopped):
    runtime, record, _, state = stopped
    original = record.read_bytes()
    with pytest.raises(child_resume.ResumeStateError, match="too large"):
        child_resume.begin_tools_change(runtime, NAME, state, record,
                                       {"version": 1, "tools": {"mcp": ["x" * 4096]}}, None)
    assert record.read_bytes() == original
    assert not list((runtime / "child-agents").glob("*.tools-change.json"))
    assert (runtime / "child-agents" / f"{NAME}.codex-home" / "old-config").exists()


def test_empty_replacement_does_not_require_source_config(stopped, monkeypatch):
    _, _, _, _ = stopped
    (Path(os.environ["CODEX_HOME"]) / "config.toml").unlink()
    monkeypatch.setattr(server, "do_resume", lambda *a, **k: {"ok": True, "action": "resumed"})
    assert server.do_jump(NAME, tools={})["ok"]


@pytest.mark.parametrize("stopped", ["codex"], indirect=True)
def test_codex_forecast_accepts_missing_source_config_for_empty_selection(stopped, monkeypatch):
    runtime, record, _, state = stopped
    (Path(os.environ["CODEX_HOME"]) / "config.toml").unlink()
    monkeypatch.setenv("AGENTSTACK_MCP_PROXY", sys.executable)
    child_tools.write_codex_record(str(record), child_tools.normalize("mail-only", {}))
    registration = {key: state[key] for key in ("agent_id", "project_key", "program")}
    home, profile = server._codex_resume_child_home(NAME, registration)
    assert home == str(runtime / "child-agents" / f"{NAME}.codex-home")
    assert profile == "orrery-only"


@pytest.mark.parametrize("provider", ["codex-app", "gemini", ""])
def test_unsupported_provider_never_changes_or_activates(stopped, monkeypatch, provider):
    _, record, _, _ = stopped
    raw = record.read_bytes()
    monkeypatch.setattr(server, "_agent_program", lambda name: provider)
    monkeypatch.setattr(server, "_open_codex_app", lambda name: pytest.fail("must not activate"))
    assert not server.do_jump(NAME, tools={})["ok"]
    assert record.read_bytes() == raw


class SimulatedCrash(BaseException):
    pass


@pytest.mark.parametrize("point", ["journal", "state", "home", "mcp", "record", "ready"])
def test_begin_crash_recovers_via_canonical_cli(stopped, monkeypatch, point):
    runtime, record, _, state = stopped
    original = record.read_bytes()
    directory = record.parent
    state_path = directory / f"{NAME}.json"
    journal = child_resume._tools_journal(state_path)
    atomic = child_resume._atomic_json
    raw_write = child_resume._atomic_bytes
    replace = child_resume.os.replace
    def write(path, payload):
        atomic(path, payload)
        if (point == "journal" and path == journal and payload.get("phase") == "prepared"
                or point == "ready" and path == journal and payload.get("phase") == "ready"
                or point == "state" and path == state_path):
            raise SimulatedCrash()
    def write_raw(path, raw, mode=0o600):
        raw_write(path, raw, mode)
        if point == "record" and path == record:
            raise SimulatedCrash()
    def move(source, target):
        replace(source, target)
        if (point == "home" and Path(source).name == f"{NAME}.codex-home"
                or point == "mcp" and Path(source).name == f"{NAME}.mcp.json"):
            raise SimulatedCrash()
    with monkeypatch.context() as patch:
        patch.setattr(child_resume, "_atomic_json", write)
        patch.setattr(child_resume, "_atomic_bytes", write_raw)
        patch.setattr(child_resume.os, "replace", move)
        with pytest.raises(SimulatedCrash):
            child_resume.begin_tools_change(runtime, NAME, state, record, {"base": "default", "tools": {}}, "inherit" if state["provider"] == "codex" else None)
    generation = json.loads(journal.read_text())["generation"]
    with pytest.raises(child_resume.ResumeStateError, match="pending"):
        child_resume._check_tools_change(child_resume._load_state(state_path), state_path)
    assert not child_resume.purge_one(runtime, NAME, reason="purged")
    result = subprocess.run([sys.executable, str(ROOT / "hooks/child_resume.py"), "rollback-tools-change",
        "--runtime-dir", str(runtime), "--agent-name", NAME, "--generation", generation],
        capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert record.read_bytes() == original
    assert not journal.exists()
    assert child_resume._load_state(state_path) == state
    assert (directory / f"{NAME}.codex-home" / "old-config").read_text() == "old"
    assert (directory / f"{NAME}.mcp.json").read_bytes() == b"old-generated"
    assert (runtime.parent / "never-delete").read_text() == "preserve"


@pytest.mark.parametrize("commit,point", [(False, p) for p in ["decision", "record", "home", "mcp", "state"]]
    + [(True, p) for p in ["decision", "remove", "state"]])
@pytest.mark.parametrize("absent_record", [False, True])
def test_finish_crash_retries_durable_decision(stopped, monkeypatch, commit, point, absent_record):
    runtime, record, _, state = stopped
    old = record.read_bytes() if not absent_record else None
    if absent_record:
        record.unlink()
    candidate = {"base": "default", "tools": {}}
    generation = child_resume.begin_tools_change(runtime, NAME, state, record, candidate, "inherit" if state["provider"] == "codex" else None)
    directory = record.parent
    state_path = directory / f"{NAME}.json"
    journal = child_resume._tools_journal(state_path)
    atomic = child_resume._atomic_json
    raw_write = child_resume._atomic_bytes
    replace = child_resume.os.replace
    remove = child_resume._remove_exact
    unlink = Path.unlink
    def write(path, payload):
        atomic(path, payload)
        if (point == "decision" and path == journal or point == "state" and path == state_path):
            raise SimulatedCrash()
    def write_raw(path, raw, mode=0o600):
        raw_write(path, raw, mode)
        if point == "record" and path == record:
            raise SimulatedCrash()
    def delete(path, *args, **kwargs):
        unlink(path, *args, **kwargs)
        if point == "record" and path == record:
            raise SimulatedCrash()
    def move(source, target):
        replace(source, target)
        if (point == "home" and Path(target).name == f"{NAME}.codex-home"
                or point == "mcp" and Path(target).name == f"{NAME}.mcp.json"):
            raise SimulatedCrash()
    def remove_path(path):
        remove(path)
        if point == "remove":
            raise SimulatedCrash()
    with monkeypatch.context() as patch:
        patch.setattr(child_resume, "_atomic_json", write)
        patch.setattr(child_resume, "_atomic_bytes", write_raw)
        patch.setattr(child_resume.os, "replace", move)
        patch.setattr(child_resume, "_remove_exact", remove_path)
        patch.setattr(Path, "unlink", delete)
        with pytest.raises(SimulatedCrash):
            child_resume.finish_tools_change(runtime, NAME, generation, commit=commit)
    # The rollback CLI completes a recorded commit, instead of undoing it.
    result = subprocess.run([sys.executable, str(ROOT / "hooks/child_resume.py"), "rollback-tools-change",
        "--runtime-dir", str(runtime), "--agent-name", NAME, "--generation", generation],
        capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert not journal.exists()
    if commit:
        assert json.loads(record.read_text()) == candidate
    elif old is None:
        assert not record.exists()
    else:
        assert record.read_bytes() == old
    if not commit:
        assert child_resume._load_state(state_path) == state
        assert (directory / f"{NAME}.codex-home" / "old-config").read_text() == "old"
        assert (directory / f"{NAME}.mcp.json").read_bytes() == b"old-generated"
    assert (runtime.parent / "never-delete").read_text() == "preserve"


@pytest.mark.parametrize("failure", ["permission", "timeout", "absent", "finished"])
def test_tmux_probe_unknown_refuses_before_staging(stopped, monkeypatch, failure):
    runtime, record, _, _ = stopped
    original = record.read_bytes()
    monkeypatch.setattr(server, "_child_session_live", REAL_LIVE_PROBE)
    monkeypatch.setattr(child_resume, "live_lease", lambda *a, **k: False)
    def run(*args, **kwargs):
        if failure == "timeout":
            raise subprocess.TimeoutExpired("tmux", 2)
        return subprocess.CompletedProcess(args[0], 0 if failure == "finished" else 1,
            stdout="", stderr="can't find session" if failure == "absent" else "Permission denied")
    monkeypatch.setattr(server.subprocess, "run", run)
    monkeypatch.setattr(server, "_has_session", lambda name: failure == "finished")
    monkeypatch.setattr(server, "lookup_agent", lambda name: {"category": "finished"})
    monkeypatch.setattr(server, "do_resume", lambda *a, **k: {"ok": True, "action": "resumed"})
    result = server.do_jump(NAME, tools={})
    assert result["ok"] is (failure in {"absent", "finished"})
    if failure in {"permission", "timeout"}:
        assert "Cannot confirm" in result["error"]
        assert record.read_bytes() == original
        assert not child_resume._tools_journal(record.parent / f"{NAME}.json").exists()


REAL_LIVE_PROBE = server._child_session_live


def test_accepted_launch_cleanup_error_is_pending_success(stopped, monkeypatch):
    runtime, record, _, state = stopped
    monkeypatch.setattr(server, "do_resume", lambda *a, **k: {"ok": True, "action": "resumed"})
    def fail(path):
        raise OSError("fixture cleanup failure")
    with monkeypatch.context() as patch:
        patch.setattr(child_resume, "_remove_exact", fail)
        result = server.do_jump(NAME, tools={})
    assert result["ok"] and result["tools_changed"]
    assert "cleanup is pending" in result["warning"]
    journal = child_resume._tools_journal(record.parent / f"{NAME}.json")
    saved = json.loads(journal.read_text())
    assert saved["phase"] == "commit"
    child_resume.finish_tools_change(runtime, NAME, saved["generation"], commit=False)
    assert json.loads(record.read_text())["tools"] == {}
    assert not journal.exists()
