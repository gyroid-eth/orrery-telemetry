"""Resume can create detached tmux without an OS window (issue #138)."""
from __future__ import annotations

import json
from http.server import ThreadingHTTPServer
import threading
from types import SimpleNamespace
import urllib.error
import urllib.request

import pytest
from dashboard import server
from test_claude_resume_mail import resume, tmux_resume, NAME
from test_codex_resume_flags import policy_env, _seed_child_identity, _invoke_resume_entry


@pytest.mark.parametrize("setting,explicit,opens", [(None, None, True), ("1", None, True),
    ("0", None, False), ("0", True, True), ("1", False, False)])
def test_claude_resume_terminal_preference_preserves_mail_order(tmux_resume, monkeypatch, setting, explicit, opens):
    _, _, calls, sessions, state = tmux_resume
    sessions.clear()
    state['expect_husk'] = False
    monkeypatch.delenv("AGENTSTACK_AUTO_OPEN_CHILD", raising=False)
    if setting is not None:
        monkeypatch.setenv("AGENTSTACK_AUTO_OPEN_CHILD", setting)
    result = server.do_resume(NAME, open_terminal=explicit)
    assert result["ok"], result
    assert bool(state['windows']) is opens
    prepared = state['commands'][0]
    assert prepared[:4] == ['env', '-u', 'TMUX', '-u']
    assert 'new-session' in prepared and '-d' in prepared and '-A' not in prepared
    assert '--resume' in prepared[-1] and 'wait-for' in prepared[-1]
    assert state['started'] and sessions == {'$2': NAME}
    assert [method for method, _ in calls] == ['register_agent', 'unretire_agent']
    if not opens:
        assert result['terminal'] == 'detached'


def test_detached_failure_cancels_child_resume_marker(resume, monkeypatch):
    from test_claude_resume_mail import child
    from hooks import child_resume
    runtime, registration, windows, _ = resume
    child(runtime, registration)
    monkeypatch.setenv("AGENTSTACK_AUTO_OPEN_CHILD", "0")
    monkeypatch.setattr(server, "_launch_claude_resume_tmux", lambda *a, **k: {"ok": False, "error": "duplicate session"})
    result = server.do_resume(NAME)
    assert not result["ok"] and "duplicate session" in result["error"]
    assert not windows
    assert child_resume.inspect_retained(runtime, NAME, agent_id=73, project_key=registration["project_key"], program="claude-code")


@pytest.mark.parametrize("setting,explicit,opens", [("0", None, False), ("1", None, True), ("0", True, True), ("1", False, False)])
def test_codex_resume_uses_the_same_terminal_preference(policy_env, monkeypatch, setting, explicit, opens):
    tmp_path, project = policy_env
    runtime = tmp_path / "runtime"
    _seed_child_identity(runtime, project)
    # Set up the existing receipt/home/bootstrap fixture without launching it.
    captured = []
    original = server._launch_resume_tmux
    monkeypatch.setattr(server, "_launch_resume_tmux", lambda args, **kw: captured.append(args) or {"ok": True})
    _invoke_resume_entry(monkeypatch, tmp_path, project, runtime)
    monkeypatch.setattr(server, "_launch_resume_tmux", original)
    windows, detached = [], []
    monkeypatch.setattr(server, "_open_terminal_tmux", lambda argv, **kw: windows.append(argv) or {"ok": True, "adapter": "fixture"})
    real_run = server.subprocess.run
    def run(argv, **kw):
        if argv[0] == "env":
            detached.append(argv)
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return real_run(argv, **kw)
    monkeypatch.setattr(server.subprocess, "run", run)
    monkeypatch.setenv("AGENTSTACK_AUTO_OPEN_CHILD", setting)
    result = server._do_resume_codex("BoundCodex", open_terminal=explicit)
    assert result["ok"], result
    assert bool(windows) is opens and bool(detached) is not opens
    if not opens:
        assert "-d" in detached[0] and "-A" not in detached[0]
        assert "source " in detached[0][-1] and "codex resume" in detached[0][-1]


@pytest.mark.parametrize("body,status,preference", [({"session": NAME}, 200, None),
    ({"session": NAME, "open": False}, 200, False), ({"session": NAME, "open": True}, 200, True),
    ({"session": NAME, "open": "false"}, 400, None), ({"session": NAME, "open": 0}, 400, None),
    ({"session": NAME, "open": None}, 400, None)])
def test_jump_http_open_is_strict_boolean(monkeypatch, body, status, preference):
    calls = []
    monkeypatch.setattr(server, "do_jump", lambda name, **kw: calls.append((name, kw)) or {"ok": True})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        request = urllib.request.Request(f"http://127.0.0.1:{httpd.server_port}/api/jump",
            data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}, method="POST")
        try:
            response = urllib.request.urlopen(request, timeout=5)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            assert response.status == status
        if status == 200:
            assert calls == [(NAME, {} if "open" not in body else {"open_terminal": preference})]
        else:
            assert not calls
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def test_explicit_detached_jump_bypasses_terminal_adapter_and_rechecks_material(tmux_resume, monkeypatch):
    _, _, calls, sessions, state = tmux_resume
    sessions.clear()
    state['expect_husk'] = False
    monkeypatch.setenv("AGENTSTACK_AUTO_OPEN_CHILD", "1")
    monkeypatch.setattr(server, "_terminal_adapter", lambda: "none")
    result = server.do_jump(NAME, open_terminal=False)
    assert result["ok"], result
    assert state['started'] and not state['windows']
    assert [method for method, _ in calls] == ["register_agent", "unretire_agent"]
