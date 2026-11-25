"""Managed macOS Antigravity child handoff, permissions, roster and resume."""

import json
from pathlib import Path
from types import SimpleNamespace
import sys

import pytest

from dashboard import server
from hooks import child_resume, gemini_interactive as agy


NAME = "FixtureAgent"
PROJECT = "fixture-project"
SID = "75cea738-579c-4531-bebb-61dca40ab59f"
IDENTITY = f"{NAME}|$8|1000|123"
REGISTRATION = {"agent_id": 52, "agent_name": NAME, "project_key": PROJECT, "program": "antigravity"}


@pytest.fixture
def managed(tmp_path, monkeypatch):
    cwd = (tmp_path / "test workspace").resolve()
    cwd.mkdir()
    history = cwd.parent / "history.py"
    (cwd.parent / "agy_fixture_sibling.py").write_text("GROUP = 'orrery-reader'\n")
    history.write_text("from agy_fixture_sibling import GROUP\nHOOKS = ('SessionStart', 'PreInvocation')\n"
                       "def binding(*args): return {}\n")
    monkeypatch.setattr(agy, "history_helper", lambda: history)
    runtime = cwd.parent / "runtime"
    source = cwd.parent / "owner.token"
    source.write_text("fixture-credential")
    source.chmod(0o600)
    child_resume._atomic_json(Path(str(source) + ".binding.json"), REGISTRATION)
    agy.prepare(runtime, NAME, PROJECT, "FixtureParent", source=source, cwd=cwd,
                binary=sys.executable, model="gemini-fixture", effort="high")
    state = child_resume._load_state(runtime / "child-agents" / f"{NAME}.json")
    return runtime, cwd, state


def test_one_workspace_is_trusted_without_changing_global_settings(managed, monkeypatch, tmp_path):
    _, cwd, state = managed
    settings = tmp_path / ".gemini/antigravity-cli/settings.json"
    settings.parent.mkdir(parents=True)
    configured = {"trustedWorkspaces": ["/fixture/other"],
                  "permissions": {"deny": ["mcp(orrery-mail/fetch_inbox)"], "ask": ["command(git)"]}}
    settings.write_text(json.dumps(configured))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    plan = state["gemini_launch"]
    assert agy.validate_plan(plan) == cwd
    args = agy.command(plan, cwd, prompt="First task")
    overlay = json.loads(args[args.index("--settings") + 1])
    assert args[-2:] == ["-i", "First task"]
    assert "--sandbox" in args and overlay["enableTerminalSandbox"]
    assert overlay["trustedWorkspaces"] == [str(cwd)]
    assert overlay["permissions"]["deny"] == configured["permissions"]["deny"]
    assert overlay["permissions"]["ask"] == configured["permissions"]["ask"]
    assert "mcp(orrery-mail/send_message)" in overlay["permissions"]["allow"]
    assert json.loads(settings.read_text()) == configured
    assert agy.command(plan, cwd, conversation=SID)[-2:] == ["--conversation", SID]
    hook = (cwd / ".agents/hooks.json")
    assert set(json.loads(hook.read_text())["orrery-reader"]) == {"SessionStart", "PreInvocation"}
    assert hook.stat().st_mode & 0o777 == 0o600
    hook.write_text("{}")
    with pytest.raises(ValueError, match="reservation hook"):
        agy.validate_plan(plan)


def test_edit_uses_shared_reservation_and_shell_stays_in_the_worktree(managed, monkeypatch):
    _, cwd, _ = managed
    calls = []

    def hook(argv, **kw):
        name = Path(argv[-1]).name
        calls.append((name, json.loads(kw["input"])))
        return SimpleNamespace(returncode=2 if name == "check-file-reservation.sh" else 0)

    monkeypatch.setattr(agy.subprocess, "run", hook)
    event = {"conversationId": SID, "toolCall": {"name": "replace_file_content",
                                                 "args": {"TargetFile": "edit.txt"}}}
    assert agy.guard(event, cwd)["decision"] == "deny"
    assert [item[0] for item in calls] == [
        "deny-llm-key-read.py", "check-agent-registered.sh", "check-file-reservation.sh"]
    assert calls[-1][1]["tool_input"]["file_path"] == str(cwd / "edit.txt")
    event["toolCall"]["args"]["TargetFile"] = "../not-in-worktree"
    assert agy.guard(event, cwd)["decision"] == "deny"
    event["toolCall"] = {"name": "run_command", "args": {"CommandLine": "printf OK"}}
    assert agy.guard(event, cwd) == {"decision": "allow", "overwrite": {"Cwd": str(cwd)}}
    event["toolCall"]["args"]["BypassSandbox"] = True
    assert agy.guard(event, cwd)["decision"] == "deny"


def test_roster_status_is_bound_to_this_tmux_generation(managed, monkeypatch, capsys):
    runtime, _, _ = managed
    monkeypatch.setenv("AGENTSTACK_AGY_HISTORY_LAUNCH", "fixture")
    monkeypatch.setenv("AGENTSTACK_GEMINI_TMUX_IDENTITY", IDENTITY)
    agy.status(runtime, NAME, {"agent_state": "idle", "cycle_mode": "auto", "model": {
        "id": "gemini-current", "display_name": "Gemini", "effort": "medium"},
        "email": "unrelated-fixture"})
    assert capsys.readouterr().out.strip() == "auto · Gemini · medium"
    assert agy.read_status(runtime, NAME, IDENTITY)["activity"] == "wait"
    assert agy.read_status(runtime, NAME, IDENTITY + "0") == {}
    monkeypatch.setattr(server, "RUNTIME_DIR", str(runtime))
    row = {"name": NAME, "program": "antigravity", "running": True}
    session = {"session_id": "$8", "created": 1000, "tmux_pid": 123}
    observed = server._agent_card_fields(row, session, {}, REGISTRATION, ())
    assert (observed["model_id"], observed["effort"], observed["act_state"]) == ("gemini-current", "medium", "wait")
    session["created"] += 1
    observed = server._agent_card_fields(row, session, {}, REGISTRATION, ())
    assert (observed["model_id"], observed["effort"], observed["act_state"]) == ("gemini-fixture", "high", None)


def test_dashboard_resumes_exact_id_and_owner(managed, monkeypatch):
    runtime, cwd, state = managed
    monkeypatch.setattr(server, "RUNTIME_DIR", str(runtime))
    child_resume.mark_retired(runtime, NAME, retention_days=30)
    monkeypatch.setattr(server, "_gemini_resume_material",
                        lambda name: (REGISTRATION, state, Path("/fixture/helper.py"), cwd, SID))
    calls = []

    def mail(method, payload):
        calls.append((method, payload))
        if method == "register_agent":
            return {"ok": True, "data": {"id": 52, "name": NAME, "program": "antigravity", "retired_at": "now"}}
        raise AssertionError(method)

    monkeypatch.setattr(server, "_mcp_call", mail)
    monkeypatch.setattr(server, "_set_claude_mail_retired", lambda *a, **kw: calls.append(("retired", kw)))

    def launch(argv, **kwargs):
        assert argv[-3:-1] == ["/bin/bash", "-c"]
        assert "AGENTSTACK_GEMINI_INTERACTIVE=1" in argv
        assert f"AGENTSTACK_RUNTIME_DIR={runtime}" in argv
        assert argv[-1].split("; ", 1)[0].endswith(" || exit $?")
        assert " wait-for agentstack-resume-" in argv[-1]
        assert f"--conversation {SID}" in argv[-1]
        assert "fixture-credential" not in str(argv)
        return SimpleNamespace(returncode=0, stdout="$1\n")

    monkeypatch.setattr(server.subprocess, "run", launch)
    monkeypatch.setattr(server, "_tmux_run", lambda *a, **kw: SimpleNamespace(returncode=0, stdout=""))
    assert server._do_resume_gemini(NAME, open_terminal=False)["ok"]
    assert calls[0][1]["existing_agent_id"] == 52
    assert calls[1] == ("retired", {"retired": False})
