"""Managed macOS Antigravity child handoff, permissions, roster and resume."""

import json
from pathlib import Path
from types import SimpleNamespace
import sys

import pytest

from dashboard import mobile_projection, server
from hooks import child_resume, gemini_interactive as agy, screen_state


NAME = "FixtureAgent"
PROJECT = "fixture-project"
SID = "75cea738-579c-4531-bebb-61dca40ab59f"
IDENTITY = f"{NAME}|$8|1000|123"
REGISTRATION = {"agent_id": 52, "agent_name": NAME, "project_key": PROJECT, "program": "antigravity"}


@pytest.fixture
def managed(tmp_path, monkeypatch, request):
    standalone = getattr(request, "param", False)
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
    agy.prepare(runtime, NAME, PROJECT, "" if standalone else "FixtureParent", source=source, cwd=cwd,
                binary=sys.executable, model="gemini-fixture", effort="high", standalone=standalone)
    state = child_resume._load_state(runtime / "child-agents" / f"{NAME}.json")
    return runtime, cwd, state


def test_orchestrator_command_omits_the_effort_option(managed, monkeypatch, tmp_path):
    _, cwd, state = managed
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    plan = {**state["gemini_launch"], "model": "gemini-pro-orchestrator-neon", "effort": ""}
    for entry in [{"prompt": "Fixture prompt"}, {"conversation": SID}]:
        args = agy.command(plan, cwd, **entry)
        assert args[args.index("--model") + 1] == plan["model"]
        assert "--effort" not in args


@pytest.mark.parametrize(("access", "mode"), [("read", "view"), ("operate", "control")])
def test_wsl_antigravity_uses_the_same_managed_screen_gate(tmp_path, monkeypatch, access, mode):
    monkeypatch.setattr(agy.child_tools, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(agy.child_tools, "_screen_state", lambda: vars(screen_state))
    monkeypatch.setattr(screen_state, "_python", lambda _: sys.executable)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("WSL_DISTRO_NAME", "FixtureWSL")
    monkeypatch.setenv("WSL_INTEROP", "/run/WSL/fixture")
    monkeypatch.setenv("AGENTSTACK_PORT", "53163")
    tools = agy.child_tools.change_permission({"screen_off": True}, {"computer_use": mode}, {}, managed_screen=True)
    assert tools == {"screen": {"server": "orrery-screen", "access": access}}
    spec = agy.child_tools.normalize(tools=tools)
    mcp, actual = agy.child_tools.agy_launch_config("/fixture/mail", spec, runtime=str(tmp_path),
        project=PROJECT, child=NAME, generation="1"*32, mail_env="")
    assert actual == mode
    [launch] = screen_state.Store(tmp_path).list()
    assert launch["computer_use"] == mode
    binding = mcp["mcpServers"]["orrery-screen"]
    assert binding["env"]["WSL_INTEROP"] == "/run/WSL/fixture"
    plan = {"binary": "/fixture/agy", "python": sys.executable}
    argv = agy.command(plan, tmp_path, prompt="Fixture task", screen_mode=mode)
    grants = json.loads(argv[argv.index("--settings") + 1])["permissions"]["allow"]
    expected = screen_state.VIEW_TOOLS if mode == "view" else screen_state.TOOLS
    assert grants == [f"mcp(orrery-screen/{tool})" for tool in expected]


def test_one_workspace_is_trusted_without_changing_global_settings(managed, monkeypatch, tmp_path):
    _, cwd, state = managed
    settings = tmp_path / ".gemini/antigravity-cli/settings.json"
    settings.parent.mkdir(parents=True)
    configured = {"trustedWorkspaces": ["/fixture/other"],
                  "permissions": {"allow": ["read_file(.)"], "deny": ["mcp(orrery-mail/fetch_inbox)"],
                                  "ask": ["command(git)", "mcp(orrery-mail/whois)"]}}
    settings.write_text(json.dumps(configured))
    child_resume._atomic_json(cwd / ".agents" / "mcp_config.json", {"mcpServers": {
        "orrery-mail": {"command": str(agy._home() / "bin" / "agentstack-gemini-mcp"), "args": []},
        "fixture-other": {"command": "/fixture/other"},
    }})
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    plan = state["gemini_launch"]
    assert agy.validate_plan(plan) == cwd
    args = agy.command(plan, cwd, prompt="First task")
    overlay = json.loads(args[args.index("--settings") + 1])
    assert args[-2] == "-i" and args[-1].endswith("\n\nFirst task")
    assert "--sandbox" in args and overlay["enableTerminalSandbox"]
    assert overlay["trustedWorkspaces"] == [str(cwd)]
    assert overlay["permissions"]["deny"] == configured["permissions"]["deny"]
    assert overlay["permissions"]["ask"] == configured["permissions"]["ask"]
    assert overlay["permissions"]["allow"] == [
        "read_file(.)", *(f"mcp(orrery-mail/{tool})" for tool in agy.MAIL_TOOLS)]
    assert json.loads(settings.read_text()) == configured
    resume = agy.command(plan, cwd, conversation=SID)
    assert resume[-2:] == ["--conversation", SID]
    assert json.loads(resume[resume.index("--settings") + 1])["permissions"] == overlay["permissions"]
    hook = (cwd / ".agents/hooks.json")
    assert set(json.loads(hook.read_text())["orrery-reader"]) == {"SessionStart", "PreInvocation"}
    assert hook.stat().st_mode & 0o777 == 0o600
    hook.write_text("{}")
    with pytest.raises(ValueError, match="reservation hook"):
        agy.validate_plan(plan)


@pytest.mark.parametrize("scope", ["global", "workspace"])
def test_same_named_foreign_mail_server_gets_no_automatic_approval(
        managed, monkeypatch, tmp_path, scope):
    _, cwd, state = managed
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    config_dir = tmp_path / "launch"
    child_resume._atomic_json(config_dir / ".agents" / "mcp_config.json", {"mcpServers": {
        "orrery-mail": {"command": str(agy._home() / "bin" / "agentstack-gemini-mcp"), "args": []},
    }})
    config = (tmp_path / ".gemini" / "config" if scope == "global" else cwd / ".agents")
    child_resume._atomic_json(config / "mcp_config.json", {"mcpServers": {
        "orrery-mail": {"command": "/fixture/foreign"},
    }})
    args = agy.command(state["gemini_launch"], cwd, prompt="First task", config_dir=config_dir)
    assert json.loads(args[args.index("--settings") + 1])["permissions"] == {"allow": []}
    assert args[-2:] == ["-i", "First task"]


@pytest.mark.parametrize(("selection", "screen_mode"), [([], "off"), (["screen:read"], "view"), (["screen:operate"], "control")])
def test_session_settings_allow_only_the_selected_screen_tools(managed, monkeypatch, tmp_path, selection, screen_mode):
    from hooks import screen_state

    monkeypatch.setattr(agy.child_tools.sys, "platform", "darwin")
    _, cwd, state = managed
    settings = tmp_path / ".gemini/antigravity-cli/settings.json"
    settings.parent.mkdir(parents=True)
    original = {"permissions": {"allow": ["read_file(.)"], "deny": ["mcp(orrery-screen/launch_app)"],
                                "ask": ["mcp(orrery-screen/hotkey)"]}}
    settings.write_text(json.dumps(original))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    plan = {**state["gemini_launch"], "tools": selection}
    args = agy.command(plan, cwd, prompt="First task")
    permissions = json.loads(args[args.index("--settings") + 1])["permissions"]
    scoped = [value for value in permissions["allow"] if value.startswith("mcp(orrery-screen/")]
    expected = {"off": (), "view": screen_state.VIEW_TOOLS, "control": screen_state.TOOLS}[screen_mode]
    assert scoped == [f"mcp(orrery-screen/{tool})" for tool in expected]
    assert "read_file(.)" in permissions["allow"]
    assert all(permissions[key] == original["permissions"][key] for key in ("deny", "ask"))
    assert json.loads(settings.read_text()) == original


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


@pytest.mark.parametrize("managed", [False, True], indirect=True, ids=["delegated", "standalone"])
def test_roster_status_is_bound_to_this_tmux_generation(managed, monkeypatch, capsys):
    runtime, _, state = managed
    monkeypatch.setenv("AGENTSTACK_AGY_HISTORY_LAUNCH", "fixture")
    monkeypatch.setenv("AGENTSTACK_GEMINI_TMUX_IDENTITY", IDENTITY)
    agy.status(runtime, NAME, {"agent_state": "idle", "cycle_mode": "auto", "model": {
        "id": "gemini-current", "display_name": "Gemini", "effort": "medium"},
        "email": "unrelated-fixture", "context_window": {
            "context_window_size": 1048576, "used_percentage": 14.24,
            "current_usage": {"input_tokens": 140000}, "total_input_tokens": 800000,
        }})
    assert capsys.readouterr().out.strip() == "auto · Gemini · medium"
    assert agy.read_status(runtime, NAME, IDENTITY)["activity"] == "wait"
    assert agy.read_status(runtime, NAME, IDENTITY + "0") == {}
    monkeypatch.setattr(server, "RUNTIME_DIR", str(runtime))
    row = {"name": NAME, "program": "antigravity", "running": True}
    session = {"session_id": "$8", "created": 1000, "tmux_pid": 123}
    observed = server._agent_card_fields(row, session, {}, REGISTRATION, ())
    assert (observed["model_id"], observed["effort"], observed["act_state"]) == ("gemini-current", "medium", "wait")
    assert observed["parent_state"] == ("child" if agy.launch_parent(state) else "top")
    assert (observed["ctx_used"], observed["ctx_window"], observed["ctx_window_tokens"]) == (14, "1048576", 1048576)
    assert mobile_projection.project_agent({**row, **observed})["ctx_used"] == 14

    for changed_row, changed_session, registration in (
        (row, {**session, "created": 1001}, REGISTRATION),
        ({**row, "running": False}, session, REGISTRATION),
        ({**row, "ctx_used": 91, "ctx_window": "128K"}, session, None),
    ):
        hidden = server._agent_card_fields(changed_row, changed_session, {}, registration, ())
        assert (hidden["ctx_used"], hidden["ctx_window"], hidden["ctx_window_tokens"]) == (None, None, None)

    agy.status(runtime, NAME, {"model": {"id": "gemini-current"}})
    cleared = server._agent_card_fields(row, session, {}, REGISTRATION, ())
    assert cleared["ctx_used"] is None and cleared["ctx_window_tokens"] is None


@pytest.mark.parametrize(("size", "usage", "used", "expected"), [
    (0, {}, 50, {}),
    (1048576, None, 0, {"ctx_window_tokens": 1048576}),
    (1048576, {}, True, {"ctx_window_tokens": 1048576}),
    (1048576, {}, float("nan"), {"ctx_window_tokens": 1048576}),
    (1048576, {}, 101, {"ctx_window_tokens": 1048576}),
    (1048576, {}, 0, {"ctx_window_tokens": 1048576, "ctx_used": 0}),
    (1048576, {}, 100, {"ctx_window_tokens": 1048576, "ctx_used": 100}),
])
def test_status_context_uses_observed_numbers(size, usage, used, expected):
    assert agy._status_context({
        "context_window_size": size, "current_usage": usage, "used_percentage": used,
    }) == expected


@pytest.mark.parametrize(("selection", "computer", "browser"), [
    (["browser-mode:off"], "off", False),
    (["screen:read", "browser-mode:temporary"], "view", True),
    (["screen:operate", "browser-mode:personal"], "control", True),
    (["screen:operate"], "control", None),
    (None, None, None),
])
def test_antigravity_card_uses_saved_tool_selection(selection, computer, browser, monkeypatch):
    monkeypatch.setattr(agy.child_tools.sys, "platform", "darwin")
    plan = {} if selection is None else {"tools": selection}
    monkeypatch.setattr(server, "_card_child_state", lambda *_: {"gemini_launch": plan})
    row = {"name": NAME, "program": "antigravity", "running": True}
    fields = server._agent_card_fields(row, {}, {}, REGISTRATION, ())
    assert (fields["computer_use"], fields["browser_use"]) == (computer, browser)
    assert ("computer_use_reason" in fields) == (computer is None)
    assert ("browser_use_reason" in fields) == (browser is None)


@pytest.mark.parametrize("platform", ["darwin", "linux"])
@pytest.mark.parametrize("browser_mode", ["off", "temporary", "personal"])
def test_delegated_first_task_names_the_configured_mcp_tools(managed, monkeypatch, tmp_path, platform, browser_mode):
    runtime, cwd, state = managed
    state["gemini_launch"]["tools"] = [f"browser-mode:{browser_mode}"]
    child_resume._atomic_json(child_resume._paths(runtime, NAME)[0], state)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(agy.child_tools, "sys", SimpleNamespace(platform=platform))
    monkeypatch.setattr(agy.child_tools, "running_in_wsl", lambda: platform == "linux")
    monkeypatch.setattr(agy.child_tools, "_browser_mcp", lambda mode: (
        "chrome-devtools", {"command": "/fixture/chrome", "args": [mode]}))
    monkeypatch.setenv("AGENTSTACK_PROJECT_KEY", PROJECT)
    monkeypatch.setenv("TMUX_PANE", "%4")
    monkeypatch.chdir(cwd)
    monkeypatch.setattr(agy.subprocess, "check_output",
                        lambda argv, **_: IDENTITY if argv[0] == "tmux" else "history-fixture")
    launches = []

    def launch(argv, *, env, **_):
        if argv[0] == "/bin/bash":
            return SimpleNamespace(returncode=0)
        assert env["PARENT_AGENT"] == "FixtureParent"
        config = json.loads((cwd / ".agents/mcp_config.json").read_text())
        assert ("chrome-devtools" in config["mcpServers"]) == (browser_mode != "off")
        assert config["mcpServers"]["orrery-mail"] == agy._mail_server()
        rules = json.loads(argv[argv.index("--settings") + 1])["permissions"]["allow"]
        assert "mcp(orrery-mail/send_message)" in rules
        assert all("chrome-devtools" not in rule for rule in rules)
        launches.append(argv[-2:])
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(agy.subprocess, "run", launch)
    task = tmp_path / "handoff"
    original = "Canonical task:\nReport the result.\n"
    child_resume._atomic_json(task, {"message": {"content": original}})
    assert agy.run(runtime, NAME, task_file=task) == 0
    [(flag, prompt)] = launches
    assert flag == "-i" and prompt.endswith("\n\n" + original)
    assert "`call_mcp_tool` with `ServerName: orrery-mail`" in prompt
    assert "`ToolName: send_message`" in prompt
    assert 'Arguments: {"to": ["FixtureParent"], "subject": "<subject>", "body_md": "<message>"}' in prompt
    assert ("`ServerName: chrome-devtools`" in prompt) == (browser_mode != "off")
    assert ("`ToolName: new_page`" in prompt) == (browser_mode != "off")


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
        assert settings["permissions"]["allow"] == [f"mcp(orrery-mail/{tool})" for tool in agy.MAIL_TOOLS]
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
    assert len(launches) == 2
    assert launches[0][0] == "-i" and launches[0][1].endswith("\n\nOne initial task.")
    assert '"to": ["<recipient agent name>"]' in launches[0][1]
    assert "ServerName: chrome-devtools" not in launches[0][1]
    assert launches[1] == ["--conversation", SID]
    assert not state_path.with_suffix(".agy-config").exists()
    assert sorted(path.relative_to(cwd).as_posix() for path in cwd.rglob("*")) == [
        ".agents", ".agents/hooks.json"]


@pytest.mark.parametrize(("lowered", "expected"), [("view", screen_state.VIEW_TOOLS), ("off", ())])
def test_standalone_resume_rotates_the_screen_binding_and_keeps_a_lower_operator_mode(
        tmp_path, monkeypatch, lowered, expected):
    cwd = (tmp_path / "workspace").resolve()
    cwd.mkdir()
    runtime = tmp_path / "runtime"
    source = tmp_path / "owner.token"
    source.write_text("fixture-credential")
    source.chmod(0o600)
    child_resume._atomic_json(Path(str(source) + ".binding.json"), REGISTRATION)
    monkeypatch.chdir(cwd)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(agy.child_tools, "sys", SimpleNamespace(platform="darwin"))
    monkeypatch.setattr(agy.child_tools, "_screen_state", lambda: vars(screen_state))
    monkeypatch.setattr(screen_state, "_python", lambda _: sys.executable)
    monkeypatch.setenv("AGENTSTACK_PROJECT_KEY", PROJECT)
    monkeypatch.setenv("AGENTSTACK_PORT", "+53162")
    monkeypatch.setenv("TMUX_PANE", "%4")
    monkeypatch.setattr(server, "RUNTIME_DIR", str(runtime))
    agy.prepare(runtime, NAME, PROJECT, "",
                source=source, cwd=cwd, binary=sys.executable, model="gemini-fixture",
                effort="high", standalone=True, tools=["screen:operate"])
    monkeypatch.setattr(agy.subprocess, "check_output",
                        lambda argv, **_: IDENTITY if argv[0] == "tmux" else "history-fixture")
    launches = []

    def launch(argv, **_):
        if argv[0] == "/bin/bash":
            return SimpleNamespace(returncode=0)
        config_dir = Path(argv[argv.index("--add-dir") + 1])
        config = json.loads((config_dir / ".agents/mcp_config.json").read_text())
        bind_path = Path(config["mcpServers"]["orrery-screen"]["env"]["AGENTSTACK_SCREEN_BINDING_FILE"])
        settings = json.loads(argv[argv.index("--settings") + 1])
        rules = [rule for rule in settings["permissions"]["allow"] if rule.startswith("mcp(orrery-screen/")]
        launches.append((bind_path, json.loads(bind_path.read_text())["origin"], rules))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(agy.subprocess, "run", launch)
    task = tmp_path / "handoff"
    child_resume._atomic_json(task, {"message": {"content": "Fixture task"}})
    assert agy.run(runtime, NAME, task_file=task) == 0
    store = screen_state.Store(runtime)
    [initial] = store.list()
    target = {key: initial[key] for key in screen_state.TARGET}
    assert initial["computer_use"] == "control"
    assert launches[0][1:] == ("http://127.0.0.1:53162",
                               [f"mcp(orrery-screen/{tool})" for tool in screen_state.TOOLS])

    store.change(target, lowered)
    row = {"name": NAME, "program": "antigravity", "running": True}
    session = {"session_id": "$8", "created": 1000, "tmux_pid": 123}
    live = server._card_screen_launches()[NAME]
    card = server._agent_card_fields(row, session, {}, REGISTRATION, (), screen_launch=live)
    assert (card["computer_use"], card["computer_use_source"]) == (lowered, "relay")
    stale = server._agent_card_fields(row, {**session, "created": 2000}, {}, REGISTRATION, (), screen_launch=live)
    assert (stale["computer_use"], stale["computer_use_source"]) == ("control", "launch")

    child_resume.mark_retired(runtime, NAME, retention_days=30)
    child_resume.begin_resume(runtime, NAME, agent_id=52, project_key=PROJECT, program="antigravity")
    monkeypatch.setenv("AGENTSTACK_PORT", "53163")
    assert agy.run(runtime, NAME, conversation=SID) == 0
    [resumed] = store.list()
    assert resumed["computer_use"] == lowered
    assert resumed["generation"] != initial["generation"]
    assert not launches[0][0].exists()
    assert launches[1][1:] == ("http://127.0.0.1:53163", [f"mcp(orrery-screen/{tool})" for tool in expected])


@pytest.mark.parametrize("policy", ["disabled", "filtered", "legacy"])
def test_delegated_resume_preserves_the_workspace_mcp_and_adds_no_screen_grants(managed, monkeypatch, policy):
    runtime, cwd, state = managed
    state_path = child_resume._paths(runtime, NAME)[0]
    plan = state["gemini_launch"]
    generation = "a" * 32
    if policy == "legacy":
        plan.pop("tools")
    else:
        plan.update(tools=["screen:operate"], screen_generation=generation)
    child_resume._atomic_json(state_path, state)
    monkeypatch.setattr(screen_state, "_python", lambda _: sys.executable)
    launch_mode = "view" if policy == "legacy" else "control"
    screen, _ = screen_state.launch_definition(runtime, PROJECT, NAME, generation, launch_mode,
                                               origin="http://127.0.0.1:53162")
    if policy == "disabled":
        screen["disabled"] = True
    if policy == "filtered":
        screen.update(enabledTools=["list_windows"], disabledTools=["click"])
    mcp = cwd / ".agents/mcp_config.json"
    child_resume._atomic_json(mcp, {"mcpServers": {
        "orrery-mail": {"command": str(agy._home() / "bin/agentstack-gemini-mcp"), "args": []},
        "orrery-screen": screen,
        "chrome-devtools": {"command": "npx", "args": ["-y", "chrome-devtools-mcp@latest", "--isolated"]},
        "fixture-extra": {"command": "/fixture/extra"},
    }})
    before, inode = mcp.read_bytes(), mcp.stat().st_ino
    store = screen_state.Store(runtime)
    retained_launches = store.list()
    assert retained_launches[0]["computer_use"] == launch_mode
    monkeypatch.setattr(agy.child_tools, "agy_launch_config", lambda *a, **kw: pytest.fail("regenerated MCP"))
    monkeypatch.setattr(Path, "home", lambda: runtime.parent)
    monkeypatch.chdir(cwd)
    monkeypatch.setenv("AGENTSTACK_PROJECT_KEY", PROJECT)
    monkeypatch.setenv("AGENTSTACK_PORT", "53163")
    monkeypatch.setenv("TMUX_PANE", "%4")
    monkeypatch.setattr(agy.subprocess, "check_output",
                        lambda argv, **_: IDENTITY if argv[0] == "tmux" else "history-fixture")
    invocations = []

    def launch(argv, **_):
        if argv[0] != "/bin/bash":
            invocations.append(argv[-2:])
            settings = json.loads(argv[argv.index("--settings") + 1])
            assert all(not rule.startswith("mcp(orrery-screen/)")
                       for rule in settings["permissions"]["allow"])
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(agy.subprocess, "run", launch)
    child_resume.mark_retired(runtime, NAME, retention_days=30)
    child_resume.begin_resume(runtime, NAME, agent_id=52, project_key=PROJECT, program="antigravity")
    assert agy.run(runtime, NAME, conversation=SID) == 0
    assert invocations == [["--conversation", SID]]
    assert (mcp.read_bytes(), mcp.stat().st_ino) == (before, inode)
    assert store.list() == retained_launches


@pytest.mark.parametrize(("resume_from", "expected"), [("", "view"), ("e" * 32, "off")])
def test_resume_accepts_legacy_relay_record_and_fails_closed_on_generation_mismatch(
        tmp_path, monkeypatch, resume_from, expected):
    monkeypatch.setattr(screen_state, "_python", lambda _: sys.executable)
    store = screen_state.Store(tmp_path)
    store.launch(PROJECT, screen_state.launch_runtime(tmp_path), NAME, "a" * 32, "view")
    _, mode = screen_state.launch_definition(
        tmp_path, PROJECT, NAME, "b" * 32, "control",
        origin="http://127.0.0.1:53163", resume_from=resume_from)
    assert mode == store.list()[0]["computer_use"] == expected


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

    def prepare(*, tools=()):
        agy.prepare(runtime, NAME, PROJECT, "", source=source, cwd=cwd, binary=sys.executable,
                    model="gemini-fixture", effort="high", standalone=True, tools=tools)

    return runtime, config, prepare


def test_standalone_prepare_rejects_incompatible_mcp_before_creating_state(standalone_preflight):
    runtime, config, prepare = standalone_preflight
    config.write_text('{"mcpServers":{"cua-driver":{"command":"/usr/bin/true"}}}')
    with pytest.raises(ValueError, match="Disable the global Antigravity MCP server 'cua-driver'"):
        prepare()
    assert not runtime.exists()


@pytest.mark.parametrize(("server_name", "enabled", "message"), [
    ("desktop", {"command": "/fixture/cua-driver"}, "workspace Antigravity MCP server 'desktop'"),
    ("browser", {"command": "npx", "args": ["-y", "chrome-devtools-mcp@latest"]},
     "workspace Antigravity browser MCP"),
    ("browser", {"command": "/fixture/bin/chrome-devtools-mcp"}, "workspace Antigravity browser MCP"),
])
def test_standalone_off_checks_workspace_mcp(standalone_preflight, server_name, enabled, message):
    runtime, _, prepare = standalone_preflight
    config = runtime.parent / "selected/.agents/mcp_config.json"
    config.parent.mkdir()
    config.write_text(json.dumps({"mcpServers": {server_name: enabled}}))
    with pytest.raises(ValueError, match=message):
        prepare(tools=["browser-mode:off"])
    assert not runtime.exists()
    config.write_text(json.dumps({"mcpServers": {server_name: {**enabled, "disabled": True}}}))
    prepare(tools=["browser-mode:off"])


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


@pytest.mark.parametrize("standalone", [True, False])
def test_dashboard_resumes_exact_id_and_owner(managed, monkeypatch, standalone):
    runtime, cwd, state = managed
    if standalone:
        state["gemini_launch"]["standalone"] = True
        state.pop("parent_agent")
    monkeypatch.setattr(server, "RUNTIME_DIR", str(runtime))
    monkeypatch.setattr(server, "PORT", 53163)
    monkeypatch.setenv("AGENTSTACK_PORT", "+9999")
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
        ports = [arg for arg in argv if arg.startswith("AGENTSTACK_PORT=")]
        assert ports == (["AGENTSTACK_PORT=53163"] if standalone else [])
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


@pytest.mark.parametrize("standalone", [True, False], ids=["standalone", "delegated"])
@pytest.mark.parametrize("change", [{"computer_use": "view"}, {"browser_mode": "temporary"}])
def test_permission_restart_uses_bound_conversation_and_projects_only_selected_tools(
        managed, monkeypatch, standalone, change):
    runtime, cwd, state = managed
    state_path = child_resume._paths(runtime, NAME)[0]
    mcp_path = cwd / ".agents/mcp_config.json"
    plan = state["gemini_launch"]
    plan["tools"] = ["browser-mode:off"]
    if standalone:
        plan["standalone"] = True
        state.pop("parent_agent")
    else:
        child_resume._atomic_json(mcp_path, {"mcpServers": {"orrery-mail": agy._mail_server()}})
    original = mcp_path.read_bytes() if mcp_path.exists() else None
    child_resume._atomic_json(state_path, state)
    monkeypatch.setattr(Path, "home", lambda: runtime.parent)
    monkeypatch.setattr(agy.child_tools, "_screen_state", lambda: vars(screen_state))
    monkeypatch.setattr(screen_state, "_python", lambda _: sys.executable)
    monkeypatch.setattr(agy.child_tools.shutil, "which", lambda _: "/fixture/npx")
    monkeypatch.chdir(cwd)
    monkeypatch.setenv("AGENTSTACK_PROJECT_KEY", PROJECT)
    monkeypatch.setenv("AGENTSTACK_PORT", "53163")
    monkeypatch.setenv("TMUX_PANE", "%4")
    monkeypatch.setattr(server, "RUNTIME_DIR", str(runtime))
    monkeypatch.setattr(server, "PORT", 53163)
    monkeypatch.setattr(server, "_child_resume_module", lambda: child_resume)
    monkeypatch.setattr(server, "_child_tools_module", lambda: agy.child_tools)
    monkeypatch.setattr(server, "_gemini_interactive_module", lambda _: vars(agy))
    monkeypatch.setattr(server, "_agent_program", lambda _: "antigravity")
    monkeypatch.setattr(server, "_resume_registration", lambda *args: REGISTRATION)
    monkeypatch.setattr(server, "_persistent_profiles", lambda: {})
    monkeypatch.setattr(server, "_mcp_tool_parameters", lambda _: {"existing_agent_id"})
    monkeypatch.setattr(agy, "saved_conversation", lambda *args: SID)
    live = {"session_id": "$8", "created": 1000, "server_pid": 123, "tmux_pid": 123}
    monkeypatch.setattr(server, "tmux_state", lambda: {NAME: live})
    monkeypatch.setattr(server, "_child_session_live", lambda *args: True)

    def bound(runtime_dir, name, registration, session):
        assert (Path(runtime_dir), name, registration) == (runtime, NAME, REGISTRATION)
        assert session["session_id"] == live["session_id"]
        return {"history_binding": "bound", "history_session_id": SID}

    monkeypatch.setattr(server.agy_history, "binding", bound)
    permission = {"history_session_id": SID, **change}
    assert server.do_jump(NAME, permission=permission, check_only=True) == {"ok": True, "action": "validated"}
    assert not child_resume.tools_record_path(runtime, NAME).exists()
    assert (mcp_path.read_bytes() if mcp_path.exists() else None) == original
    assert not server.do_jump(NAME, permission={**permission, "history_session_id": "foreign"},
                              check_only=True)["ok"]
    if not standalone:
        child_resume._atomic_json(mcp_path, {"mcpServers": {"custom": {"command": "/fixture/custom"}}})
        rejected = server.do_jump(NAME, permission=permission, check_only=True)
        assert not rejected["ok"] and "workspace MCP configuration changed" in rejected["error"]
        child_resume._atomic_bytes(mcp_path, original)

    child_resume.mark_retired(runtime, NAME, retention_days=30)
    monkeypatch.setattr(server, "_child_session_live", lambda *args: False)
    monkeypatch.setattr(server, "_resume_capability", lambda *args, **kwargs: "ready")
    monkeypatch.setattr(server, "_mcp_call", lambda method, payload: {"ok": True,
        "data": {"id": payload["existing_agent_id"], "name": NAME, "program": "antigravity"}})
    monkeypatch.setattr(server, "_set_claude_mail_retired", lambda *args, **kwargs: None)
    monkeypatch.setattr(agy.subprocess, "check_output",
                        lambda argv, **_: IDENTITY if argv[0] == "tmux" else "history-fixture")
    observed = []

    def run(argv, *, env, **kwargs):
        if argv[0] == "/bin/bash":
            return SimpleNamespace(returncode=0)
        assert argv[-2:] == ["--conversation", SID]
        actual_path = (Path(argv[argv.index("--add-dir") + 1]) / ".agents/mcp_config.json"
                       if standalone else mcp_path)
        servers = json.loads(actual_path.read_bytes())["mcpServers"]
        assert "orrery-mail" in servers
        assert env.get("PARENT_AGENT") == (None if standalone else "FixtureParent")
        settings = json.loads(argv[argv.index("--settings") + 1])
        grants = [item for item in settings["permissions"]["allow"] if item.startswith("mcp(orrery-screen/")]
        if "computer_use" in change:
            assert set(servers) == {"orrery-mail", "orrery-screen"}
            assert grants == [f"mcp(orrery-screen/{tool})" for tool in screen_state.VIEW_TOOLS]
        else:
            assert set(servers) == {"orrery-mail", "chrome-devtools"}
            assert "--isolated" in servers["chrome-devtools"]["args"]
            assert grants == []
        observed.append(argv[-1])
        return SimpleNamespace(returncode=0)

    def launch(argv, **kwargs):
        assert kwargs["include_identity"] and f"--conversation {SID}" in argv[-1]
        assert any(arg.startswith("AGENTSTACK_TOOLS_CHANGE_GENERATION=") for arg in argv)
        assert agy.run(runtime, NAME, conversation=SID) == 0
        child_resume._atomic_json(runtime / "child-agents" / f"{NAME}.agy-status.json",
                                  {"tmux": IDENTITY, "activity": "wait"})
        return {"ok": True, "adapter": "detached", "tmux_identity": IDENTITY.split("|", 1)[1]}

    fixture_subprocess = SimpleNamespace(run=run, check_output=agy.subprocess.check_output)
    monkeypatch.setattr(agy, "subprocess", fixture_subprocess)
    monkeypatch.setattr(server, "_launch_claude_resume_tmux", launch)
    monkeypatch.setattr(server, "_tmux", lambda *args: IDENTITY)
    result = server.do_jump(NAME, open_terminal=False, permission=permission)
    assert result["ok"] and result["tools_changed"], result
    assert observed == [SID]
    assert (mcp_path.read_bytes() if mcp_path.exists() else None) == original
    assert not child_resume._tools_journal(state_path).exists()
