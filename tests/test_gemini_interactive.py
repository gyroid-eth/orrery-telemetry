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
    runtime, cwd, _ = managed
    calls = []

    def hook(argv, **kw):
        name = Path(argv[-1]).name
        calls.append((name, json.loads(kw["input"])))
        return SimpleNamespace(returncode=2 if name == "check-file-reservation.sh" else 0)

    monkeypatch.setattr(agy.subprocess, "run", hook)
    event = {"conversationId": SID, "toolCall": {"name": "replace_file_content",
                                                 "args": {"TargetFile": "edit.txt"}}}
    assert agy.guard(event, cwd, runtime=runtime)["decision"] == "deny"
    assert [item[0] for item in calls] == [
        "deny-llm-key-read.py", "check-agent-registered.sh", "check-file-reservation.sh"]
    assert calls[-1][1]["tool_input"]["file_path"] == str(cwd / "edit.txt")
    event["toolCall"]["args"]["TargetFile"] = "../not-in-worktree"
    assert agy.guard(event, cwd, runtime=runtime)["decision"] == "deny"
    event["toolCall"] = {"name": "run_command", "args": {"CommandLine": "printf OK"}}
    assert agy.guard(event, cwd, runtime=runtime) == {"decision": "allow", "overwrite": {"Cwd": str(cwd)}}
    event["toolCall"]["args"]["BypassSandbox"] = True
    assert agy.guard(event, cwd, runtime=runtime)["decision"] == "deny"


@pytest.mark.parametrize("shared_inside", [False, True])
def test_guard_allows_reserved_edits_with_a_shared_agents_symlink(tmp_path, monkeypatch, shared_inside):
    cwd = tmp_path / "selected"
    cwd.mkdir()
    shared = (cwd if shared_inside else tmp_path) / "shared"
    shared.mkdir()
    (shared / "hooks.json").write_text("{}")
    (cwd / ".agents").symlink_to(shared, target_is_directory=True)
    checked = []

    def hook(argv, **kwargs):
        if Path(argv[-1]).name == "check-file-reservation.sh":
            checked.append(json.loads(kwargs["input"])["tool_input"]["file_path"])
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(agy.subprocess, "run", hook)
    event = {"toolCall": {"name": "write_to_file", "args": {"TargetFile": "notes.md"}}}
    runtime = tmp_path / "runtime"
    assert agy.guard(event, cwd, runtime=runtime)["decision"] == "allow"
    event["toolCall"]["args"]["TargetFile"] = ".agents/hooks.json"
    assert agy.guard(event, cwd, runtime=runtime)["decision"] == "deny"
    assert checked == [str(cwd / "notes.md")]


def test_guard_protects_installation_and_runtime_and_allows_the_managed_worktree(tmp_path, monkeypatch):
    install = tmp_path / "stack"
    runtime = tmp_path / "separate runtime"
    worktrees = install / "workspaces"
    selected_worktree = worktrees / NAME
    selected_worktree.mkdir(parents=True)
    monkeypatch.setenv("AGENTSTACK_HOME", str(install))
    monkeypatch.setenv("AGENTSTACK_WORKTREE_ROOT", str(worktrees))
    checked = []

    def hook(argv, **kwargs):
        checked.append(Path(argv[-1]).name)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(agy.subprocess, "run", hook)
    for folder in (runtime / "child-agents" / f"{NAME}.agy-config", install / "hooks"):
        edit = {"toolCall": {"name": "write_to_file", "args": {"TargetFile": str(folder / "fixture.json")}}}
        shell = {"toolCall": {"name": "run_command", "args": {"CommandLine": "pwd", "Cwd": str(folder)}}}
        assert agy.guard(edit, tmp_path, runtime=runtime)["decision"] == "deny"
        assert agy.guard(shell, tmp_path, runtime=runtime)["decision"] == "deny"
    assert not checked
    edit = {"toolCall": {"name": "write_to_file", "args": {"TargetFile": "notes.md"}}}
    assert agy.guard(edit, selected_worktree, runtime=runtime)["decision"] == "allow"
    assert checked[-1] == "check-file-reservation.sh"


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


def test_standalone_runs_and_rebuilds_configuration_without_touching_selected_folder(tmp_path, monkeypatch):
    cwd = tmp_path / "vault"
    own_hooks = cwd / ".agents" / "hooks.json"
    own_hooks.parent.mkdir(parents=True)
    own_hooks.write_text('{"existing":{}}')
    runtime = tmp_path / "runtime"
    source = tmp_path / "temporary-token"
    source.write_text("fixture-credential")
    source.chmod(0o600)
    child_resume._atomic_json(Path(str(source) + ".binding.json"), REGISTRATION)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("AGENTSTACK_PROJECT_KEY", PROJECT)
    monkeypatch.setenv("PARENT_AGENT", "Unrelated")
    monkeypatch.setenv("AGENTSTACK_PROXY_PARENT_AGENT", "Unrelated")
    monkeypatch.setenv("TMUX_PANE", "%4")
    monkeypatch.setattr(server, "RUNTIME_DIR", str(runtime))
    monkeypatch.chdir(cwd)
    agy.prepare(runtime, NAME, PROJECT, "", source=source, cwd=cwd,
                binary=sys.executable, model="gemini-fixture", effort="high", standalone=True)
    state_path = runtime / "child-agents" / f"{NAME}.json"
    state = child_resume._load_state(state_path)
    assert agy.launch_parent(state) == "" and "parent_agent" not in state
    assert state["gemini_launch"]["cwd"] == str(cwd.resolve())
    card = server._agent_card_fields({"name": NAME, "program": "antigravity"}, {}, {}, REGISTRATION, ())
    assert card["parent_state"] == "top" and card["parent_agent"] is None
    assert own_hooks.read_text() == '{"existing":{}}'

    def check_output(argv, **_kwargs):
        if argv[0] == "tmux":
            return IDENTITY
        assert argv[-3:] == ["prepare", "52", IDENTITY]
        return "fixture-nonce"

    launches = []

    def launch(argv, *, env, **_kwargs):
        if argv[0] == "/bin/bash":
            return SimpleNamespace(returncode=0)
        added = Path(argv[argv.index("--add-dir") + 1])
        assert added.parent == state_path.parent and added != cwd
        assert "PARENT_AGENT" not in env and "AGENTSTACK_PROXY_PARENT_AGENT" not in env
        assert env["AGENTSTACK_AGY_HISTORY_LAUNCH"] == "fixture-nonce"
        assert Path.cwd() == cwd.resolve()
        settings = json.loads(argv[argv.index("--settings") + 1])
        assert settings["trustedWorkspaces"] == [str(cwd.resolve()), str(added)]
        config = json.loads((added / ".agents" / "mcp_config.json").read_text())
        assert set(config["mcpServers"]) == {"orrery-mail"}
        hooks = json.loads((added / ".agents" / "hooks.json").read_text())
        assert any("PreToolUse" in hook for hook in hooks.values())
        assert any("SessionStart" in hook for hook in hooks.values())
        assert sorted(path.name for path in added.rglob("*.json")) == ["hooks.json", "mcp_config.json"]
        assert "fixture-credential" not in str(hooks) + str(config)
        launches.append(argv[-2:])
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(agy.subprocess, "check_output", check_output)
    monkeypatch.setattr(agy.subprocess, "run", launch)
    task = tmp_path / "handoff"
    child_resume._atomic_json(task, {"message": {"content": "One initial task."}})
    assert agy.run(runtime, NAME, task_file=task) == 0
    assert not task.exists()
    assert not state_path.with_suffix(".agy-config").exists()

    child_resume.mark_retired(runtime, NAME, retention_days=30)
    child_resume.begin_resume(runtime, NAME, agent_id=52, project_key=PROJECT, program="antigravity")
    assert agy.run(runtime, NAME, conversation=SID) == 0
    assert launches == [["-i", "One initial task."], ["--conversation", SID]]
    assert not state_path.with_suffix(".agy-config").exists()
    assert sorted(path.relative_to(cwd).as_posix() for path in cwd.rglob("*")) == [
        ".agents", ".agents/hooks.json"]


@pytest.fixture
def standalone_preflight(tmp_path, monkeypatch):
    cwd = tmp_path / "selected"
    cwd.mkdir()
    runtime = tmp_path / "runtime"
    source = tmp_path / "owner.token"
    source.write_text("fixture-credential")
    source.chmod(0o600)
    child_resume._atomic_json(Path(str(source) + ".binding.json"), REGISTRATION)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(agy.child_tools, "sys", SimpleNamespace(platform="darwin"))
    monkeypatch.setenv("AGENTSTACK_PROJECT_KEY", PROJECT)
    config = tmp_path / ".gemini/config/mcp_config.json"
    config.parent.mkdir(parents=True)

    def prepare():
        agy.prepare(runtime, NAME, PROJECT, "", source=source, cwd=cwd, binary=sys.executable,
                    model="gemini-fixture", effort="high", standalone=True)

    return runtime, config, prepare


def test_standalone_prepare_rejects_incompatible_mcp_before_creating_state(standalone_preflight):
    runtime, config, prepare = standalone_preflight
    config.write_text('{"mcpServers":{"cua-driver":{"command":"/usr/bin/true"}}}')
    with pytest.raises(ValueError, match="Disable the global Antigravity MCP server 'cua-driver'"):
        prepare()
    assert not runtime.exists()


def test_standalone_resume_reports_new_incompatible_mcp_before_start(standalone_preflight, monkeypatch):
    runtime, config, prepare = standalone_preflight
    prepare()
    child_resume.mark_retired(runtime, NAME, retention_days=30)
    config.write_text('{"mcpServers":{"cua-driver":{"command":"/usr/bin/true"}}}')
    monkeypatch.setattr(server, "RUNTIME_DIR", str(runtime))
    monkeypatch.setattr(server, "HOOKS_DIR", str(Path(agy.__file__).parent))
    monkeypatch.setattr(server, "sys", SimpleNamespace(platform="darwin"))
    monkeypatch.setattr(server, "_resume_registration", lambda *_: REGISTRATION)
    monkeypatch.setattr(server, "_child_tools_module", lambda: agy.child_tools)
    monkeypatch.setattr(server, "_mcp_call", lambda *_: pytest.fail("resume touched Mail"))
    monkeypatch.setattr(server, "_launch_claude_resume_tmux", lambda *_a, **_k: pytest.fail("resume started tmux"))
    server._gemini_interactive_module.cache_clear()
    try:
        result = server._do_resume_gemini(NAME, open_terminal=False)
        assert result["ok"] is False and result["resume_capability"] == "config_unrestorable"
        assert "Disable the global Antigravity MCP server 'cua-driver'" in result["error"]
    finally:
        server._gemini_interactive_module.cache_clear()


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
