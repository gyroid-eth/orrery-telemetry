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
    monkeypatch.setattr(server, "_persistent_profiles", lambda: {})
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
    monkeypatch.setattr(server, "_claude_registration", lambda name: registration)
    monkeypatch.setattr(server, "_claude_history_binding", lambda *a, **k: {
        "history_binding": "bound", "history_session_id": SID, "transcript_path": str(transcript),
        "history_receipt_ts": "2026-10-01T10:00:00"})
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


def save_selection(record, provider, base, tools):
    selection = child_tools.normalize(base, tools)
    if provider == "codex":
        child_tools.write_codex_record(str(record), selection)
    else:
        saved = json.loads(record.read_text())
        chrome = selection["tools"].get("browser")
        saved.update(selection, claude_chrome=chrome is not None, chrome_device=(chrome or {}).get("device", ""))
        claude_chrome_policy._write(str(record), saved)


def configure_fixture_browser(provider):
    if provider == "codex":
        config = Path(os.environ["CODEX_HOME"]) / "config.toml"
        with config.open("a") as handle:
            handle.write('[mcp_servers.chrome-devtools]\ncommand="/usr/bin/npx"\n'
                         'args=["chrome-devtools-mcp@1.10.1"]\n[mcp_servers.unselected]\ncommand="/bin/true"\n')


def test_permission_browser_on_keeps_mail_only_and_the_other_tools(stopped, monkeypatch):
    runtime, record, provider, state = stopped
    configure_fixture_browser(provider)
    # Model the reviewed case: the child has an explicitly selected screen
    # and a separate MCP and was launched with an isolated base.
    screen = {"server": "orrery-screen", "access": "read"}
    save_selection(record, provider, "mail-only", {"screen": screen, "mcp": ["reader"]})
    calls = []

    def resume(name, **options):
        calls.append((name, options))
        if provider == "codex":
            current = child_resume.inspect_retained(runtime, name, agent_id=7,
                project_key=state["project_key"], program=provider)
            assert current["codex_mcp_profile"] == "orrery-only"
        return {"ok": True, "action": "resumed"}

    monkeypatch.setattr(server, "do_resume", resume)
    result = server.do_jump(NAME, open_terminal=False, permission={"history_session_id": SID, "browser_use": True})
    assert result["ok"] and result["tools_changed"], result
    assert result["base"] == "mail-only"
    assert result["tools"] == {"browser": {"device": ""}, "screen": screen, "mcp": ["reader"]}
    assert calls == [(NAME, {"open_terminal": False, "expected_history_session_id": SID})]


def test_permission_computer_view_preserves_browser_settings(stopped, monkeypatch):
    _, record, provider, _ = stopped
    configure_fixture_browser(provider)
    browser = {"device": "fixture-browser" if provider == "claude" else ""}
    save_selection(record, provider, "default", {"browser": browser, "mcp": ["reader"]})
    monkeypatch.setattr(server, "do_resume", lambda *a, **k: {"ok": True, "action": "resumed"})
    result = server.do_jump(NAME, permission={"history_session_id": SID, "computer_use": "view"})
    assert result["ok"] and result["base"] == "default", result
    assert result["tools"] == {"browser": browser, "mcp": ["reader"],
                               "screen": {"server": "orrery-screen", "access": "read"}}


@pytest.mark.parametrize("mode", ["off", "temporary", "personal"])
def test_permission_resumes_same_conversation_with_explicit_browser_mode(stopped, monkeypatch, mode):
    _, record, provider, _ = stopped
    monkeypatch.setattr(child_tools.shutil, "which", lambda name: "/usr/bin/npx" if name == "npx" else None)
    screen = {"server": "orrery-screen", "access": "read"}
    old = {"screen": screen, "mcp": ["reader"], "browser_mode": "off" if mode == "personal" else "temporary"}
    save_selection(record, provider, "mail-only", old)
    calls = []

    def resume(name, **options):
        calls.append((name, options))
        return {"ok": True, "action": "resumed"}

    monkeypatch.setattr(server, "do_resume", resume)
    permission = {"history_session_id": SID, "browser_mode": mode}
    result = server.do_jump(NAME, permission=permission, open_terminal=False)
    assert result["ok"] and result["tools_changed"], result
    assert result["base"] == "mail-only"
    assert result["tools"] == {**old, "browser_mode": mode}
    assert calls == [(NAME, {"open_terminal": False, "expected_history_session_id": SID})]
    saved = json.loads(record.read_text())
    assert saved["tools"] == result["tools"]
    if provider == "claude":
        assert saved["claude_chrome"] is (mode == "personal")
        assert saved.get("browser_disabled", False) is (mode == "off")


def test_claude_explicit_browser_off_survives_a_computer_change(stopped, monkeypatch):
    _, record, provider, _ = stopped
    if provider != "claude":
        pytest.skip("Claude Chrome can inherit through the default base")
    save_selection(record, provider, "default", {"browser": {}, "mcp": ["reader"]})
    monkeypatch.setattr(server, "do_resume", lambda *a, **k: {"ok": True, "action": "resumed"})
    assert server.do_jump(NAME, permission={"history_session_id": SID, "browser_use": False})["ok"]
    assert server.do_jump(NAME, permission={"history_session_id": SID, "computer_use": "view"})["ok"]
    spec, plan, error = server._claude_resume_tools(NAME, SID, "/workspace", False)
    assert not error and spec["base"] == "default" and spec["browser_disabled"] is True
    assert "--no-chrome" in plan["flags"] and "browser" not in spec["tools"]


@pytest.mark.parametrize("condition", ["live", "unknown", "stale", "raced", "unavailable", "browser_missing"])
def test_permission_refuses_unverified_or_unavailable_resume(stopped, monkeypatch, condition):
    runtime, record, provider, _ = stopped
    before = record.read_bytes()
    permission = {"history_session_id": SID, "computer_use": "off"}
    if condition in {"live", "unknown"}:
        monkeypatch.setattr(server, "_child_session_live", lambda *a: True if condition == "live" else None)
    if condition == "stale":
        permission["history_session_id"] = "11234567-1234-1234-1234-012345678901"
    if condition == "raced":
        probes = iter((False, True))
        monkeypatch.setattr(server, "_child_session_live", lambda *a: next(probes))
    if condition == "unavailable":
        # Without the macOS managed default, Control still needs a named Cua Driver definition.
        monkeypatch.setattr(child_tools.sys, "platform", "linux")
        permission["computer_use"] = "control"
    if condition == "browser_missing":
        if provider != "codex":
            pytest.skip("Only Codex requires a Chrome MCP definition")
        permission = {"history_session_id": SID, "browser_use": True}
    monkeypatch.setattr(server, "lookup_agent", lambda *a: pytest.fail("permission must not trust a husk category"))
    monkeypatch.setattr(server, "do_resume", lambda *a, **k: pytest.fail("must not launch"))
    result = server.do_jump(NAME, permission=permission)
    assert not result["ok"] and result["error"]
    assert record.read_bytes() == before
    assert not child_resume._tools_journal(runtime / "child-agents" / f"{NAME}.json").exists()


def test_permission_codex_resume_creates_a_session_before_trying_to_open_it(stopped, monkeypatch):
    runtime, _, provider, _ = stopped
    if provider != "codex":
        pytest.skip("Codex uses a different launch path than Claude")
    monkeypatch.setenv("AGENTSTACK_HOME", str(ROOT))
    monkeypatch.setattr(server, "_codex_resume_child_home", lambda *a: (str(runtime), "orrery-only", False))
    monkeypatch.setattr(server, "_rebuild_codex_child_home", lambda *a: None)
    monkeypatch.setattr(server, "_codex_child_launch_flags", lambda *a, **k: "")
    monkeypatch.setattr(server, "_open_terminal_tmux", lambda *a, **k: pytest.fail("must not attach to a replacement"))

    def duplicate(args, *, title, open_terminal):
        assert args[:8] == ["tmux", "new-session", "-d", "-P", "-F",
                            "#{session_id}|#{session_created}|#{pid}", "-s", NAME]
        assert open_terminal is False
        return {"ok": False, "error": "duplicate session"}

    monkeypatch.setattr(server, "_launch_resume_tmux", duplicate)
    result = server.do_resume(NAME, expected_history_session_id=SID, open_terminal=True)
    assert not result["ok"] and "duplicate session" in result["error"]


def test_permission_rejects_whole_selection_and_invalid_patch_values(monkeypatch):
    monkeypatch.setattr(server, "_child_resume_module", lambda: pytest.fail("must not acquire a lock"))
    browser = {"history_session_id": SID, "browser_use": True}
    for options in ({"permission": browser, "base": "mail-only"}, {"permission": browser, "tools": {}},
                    {"permission": {**browser, "computer_use": "off"}},
                    {"permission": {**browser, "browser_mode": "temporary"}},
                    {"permission": {"history_session_id": "../fixture", "browser_mode": "personal"}},
                    {"permission": {"history_session_id": SID, "browser_mode": "on"}},
                    {"permission": {"history_session_id": SID, "browser_use": 1}},
                    {"permission": {"computer_use": "view"}},
                    {"check_only": True},
                    {"permission": browser, "check_only": "true"}):
        assert not server.do_jump(NAME, **options)["ok"]


def test_permission_request_accepts_bound_native_history_id():
    assert server._valid_permission_request({"history_session_id": "native-session.2", "browser_mode": "temporary"})


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
    ({"session": NAME, "permission": {"history_session_id": SID, "browser_use": False}},
     {"permission": {"history_session_id": SID, "browser_use": False}}),
    ({"session": NAME, "check_only": True, "permission": {"history_session_id": SID, "browser_use": False}},
     {"check_only": True, "permission": {"history_session_id": SID, "browser_use": False}}),
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
    home, profile, _ = server._codex_resume_child_home(NAME, registration)
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
    from _legacy_dashboard_transport import install_legacy_transport
    install_legacy_transport(monkeypatch, runtime.parent, server, run)
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


def mark_generated_standalone(stopped):
    runtime, record, provider, state = stopped
    if provider == "codex":
        state["codex_standalone"] = True
        child_resume._atomic_json(runtime / "child-agents" / f"{NAME}.json", state)
    else:
        saved = json.loads(record.read_text())
        claude_chrome_policy._write(str(record), {**saved, "standalone": True})


def test_permission_resume_preserves_generated_standalone(stopped, monkeypatch):
    runtime, record, provider, state = stopped
    mark_generated_standalone(stopped)
    configure_fixture_browser(provider)

    def resume(name, **options):
        assert (name, options) == (NAME, {"open_terminal": False, "expected_history_session_id": SID})
        assert json.loads(record.read_text())["tools"] == {"browser": {"device": ""}, "mcp": ["reader"]}
        if provider == "claude":
            assert json.loads(record.read_text())["standalone"] is True
        else:
            updated = json.loads((runtime / "child-agents" / f"{NAME}.json").read_text())
            assert updated["codex_standalone"] is True
            assert updated["codex_mcp_profile"] == state["codex_mcp_profile"]
            assert "parent_agent" not in updated
        return {"ok": True, "action": "resumed"}

    monkeypatch.setattr(server, "do_resume", resume)
    result = server.do_jump(NAME, open_terminal=False, permission={"history_session_id": SID, "browser_use": True})
    assert result["ok"] and result["tools_changed"], result


@pytest.mark.parametrize("change", [{"browser_use": True}, {"browser_mode": "temporary"}])
def test_preflight_and_exit_verify_same_active_standalone(stopped, monkeypatch, change):
    runtime, record, provider, state = stopped
    mark_generated_standalone(stopped)
    configure_fixture_browser(provider)
    state_file = runtime / "child-agents" / f"{NAME}.json"
    child_resume._atomic_json(state_file, {**state, "retired_at": None, "resume_expires_at": None})
    live = {"session_id": "$17", "created": 25, "server_pid": "101"}
    monkeypatch.setattr(server, "tmux_state", lambda: {NAME: live})
    monkeypatch.setattr(server, "_child_session_live", lambda *a: True)
    monkeypatch.setattr(server, "_card_codex_provenance", lambda *a: {"launch_origin": "child"})
    monkeypatch.setattr(server, "_codex_resume_child_home", lambda *a, **k: (str(runtime), "orrery-only", True))
    monkeypatch.setattr(server.shutil, "which", lambda command: "/bin/true")
    monkeypatch.setenv("AGENTSTACK_HOME", str(ROOT))
    monkeypatch.setattr(server, "ABS_CLAUDE", sys.executable)
    monkeypatch.setattr(server, "HOOKS_DIR", str(ROOT / "hooks"))
    monkeypatch.setattr(server, "_claude_resume_proxy_runner", lambda: sys.executable)
    monkeypatch.setattr(server, "_tmux_session_env", lambda *args: "old-launch-id")
    monkeypatch.setattr(server, "_mcp_tool_parameters", lambda tool: {"existing_agent_id"})
    monkeypatch.setattr(server, "_mcp_call", lambda *a, **k: pytest.fail("preflight must not change Mail"))
    monkeypatch.setattr(server, "do_resume", lambda *a, **k: pytest.fail("preflight must not launch"))
    calls = []
    monkeypatch.setattr(server, "lookup_agent", lambda name, **kw: {
        "name": name, "category": "agent", "program": provider, "running": True, "attached": False})
    monkeypatch.setattr(server, "_tmux", lambda args: f"{NAME}|25|101")
    monkeypatch.setattr(server, "_tmux_run", lambda args, **kw:
        calls.append(args) or subprocess.CompletedProcess(args, 0, provider, ""))
    monkeypatch.setattr(server.time, "sleep", lambda seconds: None)
    permission = {"history_session_id": SID, **change}
    before = (state_file.read_bytes(), record.read_bytes())
    assert server.do_jump(NAME, check_only=True, permission=permission) == {"ok": True, "action": "validated"}
    assert (state_file.read_bytes(), record.read_bytes()) == before
    assert calls == []
    stale = {"history_session_id": "11234567-1234-1234-1234-012345678901"}
    assert not server.do_jump(NAME, check_only=True, permission={**permission, **stale})["ok"]
    assert not server.do_exit(NAME, **stale)["ok"]
    assert calls == []
    active_state = json.loads(state_file.read_text())
    child_resume._atomic_json(state_file, {**active_state, "agent_id": 999})
    assert not server.do_jump(NAME, check_only=True, permission=permission)["ok"]
    assert not server.do_exit(NAME, history_session_id=SID)["ok"]
    assert calls == []
    child_resume._atomic_json(state_file, active_state)
    assert server.do_exit(NAME, history_session_id=SID)["ok"]
    assert any(args[0] == "send-keys" for args in calls)
    assert all(args[2] == "$17" for args in calls)
    calls.clear()
    monkeypatch.setattr(server, "_tmux", lambda args: f"{NAME}|26|101")
    assert not server.do_exit(NAME, history_session_id=SID)["ok"]
    assert calls == []


def test_permission_uses_generated_codex_default_and_requires_claude_launch_record(stopped, monkeypatch):
    runtime, record, provider, state = stopped
    mark_generated_standalone(stopped)
    row = {"name": NAME, "program": provider, "history_binding": "bound", "history_session_id": SID,
           "transcript_path": str(runtime.parent / "project" / f"{SID}.jsonl")}
    assert server._agent_card_fields(row, {}, {}, state, ())["permission_resume_supported"] is True
    record.unlink()
    fields = server._agent_card_fields(row, {}, {}, state, ())
    permission = {"history_session_id": SID, "browser_mode": "off"}
    if provider == "codex":
        assert fields["permission_resume_supported"] is True
        assert fields["computer_use"] is None
        monkeypatch.setattr(server, "do_resume", lambda *a, **kw: {"ok": True, "action": "resumed"})
        result = server.do_jump(NAME, permission=permission, open_terminal=False)
        assert result["tools_changed"] and result["base"] == "mail-only"
        assert child_tools.read_codex_record(str(record)) == {
            "base": "mail-only", "tools": {"browser_mode": "off"}}
        return
    assert fields["permission_resume_supported"] is False
    assert fields["permission_resume_reason"] == "No saved launch settings; use Resume"
    permission = {"history_session_id": SID, "browser_use": True}
    assert server.do_jump(NAME, check_only=True, permission=permission)["error"] == fields["permission_resume_reason"]


def test_guarded_exit_records_a_verified_live_claude_default(stopped, monkeypatch):
    runtime, record, provider, state = stopped
    if provider != "claude":
        pytest.skip("Claude has a launch marker separate from its tools record")
    record.unlink()
    live = {"session_id": "$41", "created": 17}
    monkeypatch.setattr(server, "_tmux_session_env", lambda *a: "")
    monkeypatch.setattr(server, "tmux_state", lambda: {NAME: live})
    monkeypatch.setattr(server, "_do_exit", lambda *args, **kwargs: {"ok": True})
    monkeypatch.setattr(server, "_tmux_run", lambda args, **kwargs: subprocess.CompletedProcess(
        args, 0, stdout="AGENTSTACK_CLAUDE_LAUNCH_ID=missing-record", stderr=""))
    assert not server.do_exit(NAME, history_session_id=SID)["ok"]
    assert not record.exists()
    monkeypatch.setattr(server, "_tmux_run", lambda args, **kwargs: subprocess.CompletedProcess(
        args, 0, stdout="-AGENTSTACK_CLAUDE_LAUNCH_ID", stderr=""))
    assert server.do_exit(NAME, history_session_id=SID)["ok"]
    saved = claude_chrome_policy.session_record(str(record.parent), NAME, SID)
    assert saved["base"] == "default" and saved["tools"] == {}
    assert saved["launch_id"].startswith(server._CLAUDE_DEFAULT_LAUNCH)


@pytest.mark.parametrize("resume_ok", [True, False])
def test_verified_direct_permission_requires_adoption_and_keeps_the_same_identity(stopped, monkeypatch, resume_ok):
    runtime, record, provider, old = stopped
    state_path = runtime / "child-agents" / f"{NAME}.json"
    state_path.unlink()
    record.unlink()
    owner = {"agent_name": NAME, **{key: old[key] for key in ("agent_id", "project_key", "program")}}
    if provider == "codex":
        monkeypatch.setattr(server, "_codex_registration", lambda _: owner)
        monkeypatch.setattr(server, "_codex_resume_provenance",
                            lambda *a, **kw: ({"launch_origin": "standalone"}, None))
    else:
        monkeypatch.setattr(server, "_claude_registration", lambda _: owner)
    cookie = runtime / f"agent_token_{NAME}"
    original = cookie.read_bytes()
    permission = {"history_session_id": SID, "browser_mode": "off"}
    assert "Confirm the transition" in server.do_jump(NAME, permission=permission)["error"]
    assert not state_path.exists() and not record.exists()
    calls = []

    def launch(name, *, open_terminal, expected_history_session_id):
        calls.append((name, open_terminal, expected_history_session_id))
        adopted = child_resume.inspect_retained(runtime, name, agent_id=7,
            project_key=owner["project_key"], program=provider)
        assert adopted["permission_adoption"] == SID
        assert adopted["registration_token"].encode() == original
        if provider == "codex":
            assert adopted["codex_standalone"] and adopted["codex_mcp_profile"] == "inherit"
        else:
            assert json.loads(record.read_bytes())["standalone"] is True
        return {"ok": resume_ok, "action": "resumed" if resume_ok else "failed"}

    monkeypatch.setattr(server, "do_resume", launch)
    result = server.do_jump(NAME, permission={**permission, "adopt": True}, open_terminal=False)
    assert result["ok"] is resume_ok
    assert calls == [(NAME, False, SID)]
    assert record.exists() is resume_ok and state_path.exists() is resume_ok
    assert cookie.read_bytes() == original
    assert not child_resume._tools_journal(state_path).exists()


def test_permission_stopped_terminal_accepts_only_the_single_idle_shell(monkeypatch):
    sessions = {NAME: {"session_id": "$41", "created": 17, "server_pid": 19}}
    monkeypatch.setattr(server, "lookup_agent", lambda *a, **kw: {"category": "finished", "running": False})
    monkeypatch.setattr(server, "_tmux", lambda _: "101|zsh")
    def ps(lines):
        monkeypatch.setattr(server.subprocess, "run",
            lambda args, **kwargs: subprocess.CompletedProcess(args, 0, stdout=lines))
    ps("101 50 /bin/zsh\n")
    assert server._permission_stopped_terminal(NAME, sessions) == "$41|17|19"
    ps("101 50 /bin/zsh\n102 101 /usr/bin/sleep\n")
    assert server._permission_stopped_terminal(NAME, sessions) is None
    monkeypatch.setattr(server, "_tmux", lambda _: "101|zsh\n314|zsh")
    assert server._permission_stopped_terminal(NAME, sessions) is None


def test_codex_permission_readiness_waits_for_own_binding_and_prompt(stopped, monkeypatch):
    runtime, _, provider, state = stopped
    if provider != "codex":
        pytest.skip("Codex has a separate readiness path")
    monkeypatch.setattr(server, "CODEX_LAUNCH_DIR", str(runtime))
    launch = {"launch_kind": "resume", "launch_origin": "child", "resume_session_id": SID, "expected_at": 30,
              **{key: state[key] for key in ("agent_id", "agent_name", "project_key", "program")}}
    child_resume._atomic_json(runtime / "7.json", launch)
    times = iter([0, 0, 0, 0, 1, 1, 1])
    monkeypatch.setattr(server.time, "monotonic", lambda: next(times))
    monkeypatch.setattr(server.time, "sleep", lambda _: None)
    screens = iter(["Use existing model", "› Ask Codex to do anything\n  gpt-5.5 high\n  ? for shortcuts"])
    calls = []

    def tmux(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, next(screens) if args[0] == "capture-pane" else "", "")

    monkeypatch.setattr(server, "_tmux_run", tmux)
    assert server._await_codex_permission_resume(NAME, state, SID, "$17|30|101", timeout=2)
    assert [args[0] for args in calls] == ["has-session", "capture-pane", "has-session", "capture-pane"]
    assert all(args[-1] == "$17" for args in calls)
    monkeypatch.setattr(server.time, "monotonic", lambda: 0)
    assert not server._await_codex_permission_resume(NAME, state, SID, "$17|30|101", timeout=0)


def test_unconfirmed_codex_resume_releases_only_the_new_session(stopped, monkeypatch):
    runtime, _, provider, state = stopped
    if provider != "codex":
        pytest.skip("Codex has a separate readiness path")
    state_file = runtime / "child-agents" / f"{NAME}.json"
    child_resume._atomic_json(state_file, {**state, "resume_in_progress_at": "2026-11-24T00:00:00Z"})
    monkeypatch.setattr(server, "_tmux", lambda args: f"{NAME}|$17|30|101")
    commands = []
    monkeypatch.setattr(server, "_tmux_run", lambda args, **kw: commands.append(args))
    restored = []
    monkeypatch.setattr(server, "_set_claude_mail_retired", lambda name, row, *, retired: restored.append(name))
    result = server._unconfirmed_codex_permission_resume(NAME, state, "$17|30|101")
    assert result == {"ok": False, "error": "Could not confirm Codex resumed"}
    assert commands == [["kill-session", "-t", "$17"]]
    assert "resume_in_progress_at" not in json.loads(state_file.read_text())
    assert restored == [NAME]

    monkeypatch.setattr(server, "_tmux", lambda args: f"{NAME}|$17|30|102")
    monkeypatch.setattr(server, "_child_session_live", lambda *a: None)
    commands.clear()
    result = server._unconfirmed_codex_permission_resume(NAME, state, "$17|30|101")
    assert result["resume_confirmation_pending"] is True
    assert commands == []


def test_http_forwards_history_sid_only_when_exit_is_guarded(monkeypatch):
    calls = []
    monkeypatch.setattr(server, "do_exit", lambda name, **kwargs:
        calls.append((name, kwargs)) or {"ok": True})
    http = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    thread = threading.Thread(target=http.serve_forever, daemon=True)
    thread.start()
    try:
        for fields in ({}, {"history_session_id": SID}):
            req = urllib.request.Request(f"http://127.0.0.1:{http.server_port}/api/exit",
                data=json.dumps({"session": NAME, **fields}).encode(),
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=5) as response:
                assert response.status == 200
        assert calls == [(NAME, {}), (NAME, {"history_session_id": SID})]
    finally:
        http.shutdown()
        http.server_close()
        thread.join(timeout=5)


def test_empty_tmux_server_can_confirm_stop(tmp_path):
    def empty_tmux(args, **kwargs):
        return subprocess.CompletedProcess(args, 1, "", "no sessions\n")

    assert child_resume.child_session_live(tmp_path, NAME, boot=None, tmux_run=empty_tmux) is False
