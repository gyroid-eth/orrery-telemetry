"""The dashboard saves permissions for agents without a launch record and resumes them with it."""
from __future__ import annotations

import json
import sys

import pytest

from dashboard import agent_process, server
from hooks import agent_permissions as permissions, screen_state
from test_claude_resume_mail import NAME, SID, child, resume  # noqa: F401  (fixture)


@pytest.fixture
def saved(resume, monkeypatch):
    runtime, registration, launches, calls = resume
    monkeypatch.setenv("AGENTSTACK_SCREEN_PYTHON", sys.executable)
    monkeypatch.setattr(server, "_persistent_profiles", lambda: {})

    def save(**changes):
        return permissions.save(runtime, NAME, agent_id=registration["agent_id"],
                                project_key=registration["project_key"], program="claude-code", **changes)
    return runtime, registration, launches, save


def _screen_launches(runtime):
    return [item for item in screen_state.Store(str(runtime)).list() if item["agent"] == NAME]


def test_resume_adds_the_saved_permissions_to_the_same_conversation(saved):
    runtime, _registration, launches, save = saved
    save(computer_use="view")
    save(browser_use=False)
    result = server.do_resume(NAME)
    assert result["ok"], result
    inner = launches[0][-1]
    command = inner[inner.index(f"--resume {SID} -n {NAME}"):]
    assert " --no-chrome" in command and " --disallowed-tools mcp__computer-use" in command
    config = runtime / "agent-permissions" / f"{NAME}.mcp.json"
    assert f"--mcp-config {config}" in command
    assert list(json.loads(config.read_text())["mcpServers"]) == ["orrery-screen"]
    (launch,) = _screen_launches(runtime)
    applied = permissions.read(runtime, NAME)["applied"]
    assert (applied["launcher"], applied["generation"], launch["computer_use"]) == ("resume", launch["generation"], "view")


def test_a_failed_resume_gives_back_the_screen_generation(saved, monkeypatch):
    runtime, _registration, _launches, save = saved
    save(computer_use="control")
    monkeypatch.setattr(server, "_launch_claude_resume_tmux", lambda *a, **k: {"ok": False, "error": "fixture startup failed"})
    assert not server.do_resume(NAME)["ok"]
    assert _screen_launches(runtime) == []


def test_without_a_record_and_for_children_the_resume_is_unchanged(saved):
    runtime, registration, launches, save = saved
    assert server.do_resume(NAME)["ok"]
    assert "--chrome" not in launches[0][-1] and "agent-permissions" not in launches[0][-1]
    save(browser_use=True)
    child(runtime, registration)
    assert server.do_resume(NAME)["ok"]
    assert " --chrome" not in launches[1][-1]


def test_save_endpoint_accepts_one_choice_for_agents_without_a_launch_record(saved):
    runtime, registration, _launches, _save = saved
    assert server.save_agent_permission({"session": NAME, "computer_use": "control"}) == {
        "ok": True, "action": "saved", "saved": {"computer_use": "control"}}
    assert server.save_agent_permission({"session": NAME, "browser_use": True})["saved"] == {
        "computer_use": "control", "browser_use": True}
    for body in ({"session": NAME}, {"session": NAME, "computer_use": "full"},
                 {"session": NAME, "computer_use": "off", "browser_use": True},
                 {"session": NAME, "browser_use": 1}, {"session": "../x", "browser_use": True},
                 {"session": NAME, "browser_use": True, "extra": 1}):
        assert not server.save_agent_permission(body)["ok"], body
    child(runtime, registration)
    refused = server.save_agent_permission({"session": NAME, "browser_use": False})
    assert not refused["ok"] and "without a launch record" in refused["error"]


def _bound(monkeypatch, *, receipt=True):
    monkeypatch.setattr(server, "_claude_history_binding", lambda *_a, **_k: {
        "history_binding": "bound", "history_session_id": SID, "transcript_path": "unused",
        **({"history_receipt_ts": 1} if receipt else {})})


def test_permission_jump_saves_then_resumes_a_stopped_agent(saved, monkeypatch):
    runtime, _registration, launches, _save = saved
    _bound(monkeypatch)
    monkeypatch.setattr(server, "_child_session_live", lambda *_a: False)
    monkeypatch.setattr(server, "_resume_capability", lambda *_a, **_k: "ready")
    result = server.do_jump(NAME, open_terminal=False, permission={"history_session_id": SID, "browser_use": True})
    assert result["ok"] and result["action"] == "resumed" and result["tools_changed"] is True, result
    assert permissions.read(runtime, NAME)["browser_use"] is True
    assert " --chrome" in launches[0][-1]
    other = server.do_jump(NAME, open_terminal=False,
                           permission={"history_session_id": "0" * 36, "browser_use": False})
    assert not other["ok"] and permissions.read(runtime, NAME)["browser_use"] is True


@pytest.mark.parametrize(("argv", "allowed"), [
    (["claude", "--resume", SID, "-n", NAME], True),
    (["claude", "--resume", SID, "-n", NAME, "--chrome", "--mcp-config", "x.json"], True),
    (["claude", "--model", "sample", "--resume", SID, "--channels", "plugin:x"], False),
])
def test_a_running_agent_is_restarted_only_when_resume_started_it(saved, monkeypatch, argv, allowed):
    runtime, _registration, _launches, _save = saved
    _bound(monkeypatch)
    live = {"session_id": "$1", "created": 5, "pane_pid": 10}
    monkeypatch.setattr(server, "_child_session_live", lambda *_a: True)
    monkeypatch.setattr(server, "tmux_state", lambda: {NAME: live})
    monkeypatch.setattr(server, "_process_tree_snapshot", lambda: ({10: "zsh", 11: "claude"}, {10: [11]}))
    monkeypatch.setattr(agent_process, "argv", lambda pid: argv if pid == 11 else None)
    checked = server.do_jump(NAME, open_terminal=False, check_only=True,
                             permission={"history_session_id": SID, "computer_use": "control"})
    assert checked.get("action") == ("validated" if allowed else None), checked
    if not allowed:
        assert checked["error"] == "Saved permissions apply at the agent's next start"
    assert permissions.read(runtime, NAME) is None  # A check saves nothing.
    monkeypatch.setattr(server, "_do_exit", lambda session, **kw: {"ok": True, "session": session})
    exited = server.do_exit(NAME, history_session_id=SID)
    assert exited["ok"] is allowed, exited


@pytest.mark.parametrize(("row", "resume_launch", "apply"), [
    ({"running": True}, True, "restart"),
    ({"running": True}, False, "next_start"),
    ({"running": False}, False, "resume"),
    ({"running": True, "lifecycle": "persistent"}, True, "next_start"),
])
def test_card_says_how_a_saved_choice_reaches_the_agent(saved, row, resume_launch, apply):
    _runtime, registration, _launches, save = saved
    save(computer_use="control")
    card = server._card_saved_permissions({"name": NAME, "program": "claude-code", **row}, {}, registration, resume_launch)
    assert card == {"permission_save_supported": True, "permission_apply": apply, "computer_use_next": "control"}
    assert server._card_saved_permissions({"name": NAME, "program": "claude-code", **row},
                                          {"mail_disabled": "1"}, registration, resume_launch) == {}
    assert server._card_saved_permissions({"name": NAME, "program": "codex", **row}, {}, registration,
                                          resume_launch) == {}
