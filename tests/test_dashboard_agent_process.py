"""Tools of agents without saved launch settings, read from fake processes."""

import json
import os
import subprocess
import sys
import time

import pytest

from dashboard import agent_process, server


_RELAY = ["/usr/bin/python3", "/fixture/cua_relay.py", "--", "/fixture/CuaDriver.app/Contents/MacOS/cua-driver", "mcp"]
_SCREEN = ["/fixture/python", "-I", "-m", "agentstack_mail.screen_mcp"]
_DEVTOOLS = ["npm", "exec", "chrome-devtools-mcp@latest"]
_SHELL = ["/bin/zsh", "-c", "ps | grep 'cua-driver mcp' && npx chrome-devtools-mcp@latest"]


def _tree(processes):
    """processes: {pid: (ppid, argv)} -> (names, children), argv reader"""
    names = {pid: os.path.basename(values[0]) for pid, (_ppid, values) in processes.items()}
    children = {}
    for pid, (ppid, _values) in processes.items():
        children.setdefault(ppid, []).append(pid)
    return (names, children), lambda pid: processes[pid][1] if pid in processes else None


def _claude_json(tmp_path, **data):
    path = tmp_path / "claude.json"
    path.write_text(json.dumps(data))
    return str(path)


def test_matchers_use_exact_arguments_only():
    assert agent_process.is_cua(["cua-driver", "mcp"])
    assert agent_process.is_cua(_RELAY)
    assert not agent_process.is_cua(["/fixture/cua-driver", "mcp", "--extra"])
    assert not agent_process.is_cua(_SHELL)
    assert agent_process.is_screen(_SCREEN)
    assert not agent_process.is_screen(["python", "-m", "agentstack_mail.other"])
    assert agent_process.is_devtools(_DEVTOOLS)
    assert agent_process.is_devtools(["node", "/cache/node_modules/chrome-devtools-mcp/build/src/index.js"])
    assert not agent_process.is_devtools(_SHELL)
    assert agent_process.chrome_flag(["claude", "--chrome", "--no-chrome"]) is False
    assert agent_process.chrome_flag(["claude", "--append-system-prompt", "use --chrome"]) is None
    assert agent_process.chrome_flag(["claude", "--chrome"]) is True


def test_claude_with_a_relay_child_is_control(tmp_path):
    tree, read = _tree({10: (1, ["claude", "--resume", "sid", "-n", "Worker"]), 11: (10, _RELAY),
                        12: (10, _SHELL)})
    settings = _claude_json(tmp_path, claudeInChromeDefaultEnabled=True,
                            mcpServers={"screen": {"command": _RELAY[0], "args": _RELAY[1:]}})
    assert agent_process.observe(10, tree, "claude", claude_json=settings, read=read) == {
        "computer_use": "control", "browser_use": True,
        "computer_use_next": "control", "browser_use_next": True}


def test_claude_started_before_the_user_setting_waits_for_a_restart(tmp_path):
    tree, read = _tree({10: (1, ["claude", "--channels", "plugin:example"]), 12: (10, _SHELL)})
    settings = _claude_json(tmp_path, mcpServers={"screen": {"command": "cua-driver", "args": ["mcp"]}})
    assert agent_process.observe(10, tree, "claude", claude_json=settings, read=read) == {
        "computer_use": "off", "browser_use": False,
        "computer_use_next": "control", "browser_use_next": False}


def test_claude_project_settings_and_explicit_flags(tmp_path):
    work = str(tmp_path / "work")
    settings = _claude_json(tmp_path, claudeInChromeDefaultEnabled=True, projects={
        work: {"mcpServers": {"screen": {"command": "cua-driver", "args": ["mcp"]}}},
        str(tmp_path / "off"): {"mcpServers": {"screen": {"command": "cua-driver", "args": ["mcp"]}},
                                "disabledMcpServers": ["screen"]},
    })
    tree, read = _tree({10: (1, ["claude", "--no-chrome"])})
    assert agent_process.observe(10, tree, "claude", cwd=work, claude_json=settings, read=read) == {
        "computer_use": "off", "browser_use": False,
        "computer_use_next": "control", "browser_use_next": False}
    off = agent_process.observe(10, tree, "claude", cwd=str(tmp_path / "off"), claude_json=settings, read=read)
    assert off["computer_use_next"] == "off"
    # A strict launch does not read the user's settings on its next start either.
    tree, read = _tree({10: (1, ["claude", "--strict-mcp-config", "--mcp-config", "x.json"])})
    strict = agent_process.observe(10, tree, "claude", cwd=work, claude_json=settings, read=read)
    assert (strict["computer_use"], strict["computer_use_next"], strict["browser_use"]) == ("off", "off", True)


def test_claude_screen_relay_uses_the_launch_permission(tmp_path):
    tree, read = _tree({10: (1, ["claude"]), 11: (10, _SCREEN)})
    settings = _claude_json(tmp_path)
    assert agent_process.observe(10, tree, "claude", claude_json=settings, screen_mode="view", read=read)[
        "computer_use"] == "view"
    unknown = agent_process.observe(10, tree, "claude", claude_json=settings, read=read)
    assert unknown["computer_use"] is unknown["computer_use_next"] is None


def test_unreadable_process_or_settings_stay_unknown(tmp_path):
    tree, read = _tree({11: (10, _RELAY)})
    assert agent_process.observe(10, tree, "claude", read=read) == {}
    tree, read = _tree({10: (1, ["claude"])})
    assert agent_process.observe(10, tree, "claude", claude_json=str(tmp_path / "absent.json"), read=read) == {
        "computer_use": "off", "browser_use": None, "computer_use_next": None, "browser_use_next": None}
    assert agent_process.observe(10, None, "claude", read=read) == {}
    assert agent_process.observe(10, tree, "dsh", read=read) == {}


def test_codex_reads_devtools_and_screen_children():
    tree, read = _tree({20: (1, ["codex", "resume", "sid"]), 21: (20, _DEVTOOLS), 22: (20, _RELAY)})
    assert agent_process.observe(20, tree, "codex", read=read) == {
        "computer_use": "control", "browser_use": True, "computer_use_next": None, "browser_use_next": None}
    tree, read = _tree({20: (1, ["codex"]), 23: (20, _SHELL)})
    assert agent_process.observe(20, tree, "codex", read=read)["browser_use"] is False


@pytest.mark.skipif(not (sys.platform == "darwin" or sys.platform.startswith("linux")),
                    reason="exact argv is read on macOS and Linux")
def test_reads_the_exact_argv_of_a_real_process():
    values = ["-c", "import time; time.sleep(30)", "--chrome", "two words", "", "last"]
    process = subprocess.Popen([sys.executable, *values])
    try:
        for _ in range(50):
            observed = agent_process.argv(process.pid)
            if observed and observed[1:] == values:
                break
            time.sleep(0.05)
        assert observed[1:] == values
    finally:
        process.kill()
        process.wait()
    assert agent_process.argv(0) is None
    assert agent_process.argv(True) is None


def test_card_reads_the_process_only_without_saved_settings(tmp_path, monkeypatch):
    processes = {10: (1, ["zsh", "-c", "claude"]), 11: (10, ["claude", "--channels", "plugin:example"]),
                 12: (11, _RELAY)}
    tree, read = _tree(processes)
    tree[0][11] = "claude"
    monkeypatch.setattr(agent_process, "argv", read)
    monkeypatch.setenv("AGENTSTACK_CLAUDE_JSON", _claude_json(tmp_path, claudeInChromeDefaultEnabled=True))
    row = {"name": "Worker", "program": "claude-code", "running": True}
    card = server._agent_card_fields(row, {"pane_pid": 10}, {}, None, (), process_tree=tree)
    assert {key: card.get(key) for key in (
        "computer_use", "browser_use", "computer_use_next", "browser_use_next", "permission_source",
        "permission_resume_supported", "permission_resume_reason")} == {
        "computer_use": "control", "browser_use": True, "computer_use_next": "off",
        "browser_use_next": True, "permission_source": "process",
        "permission_resume_supported": False, "permission_resume_reason": "No saved launch settings; use Resume"}
    stopped = server._agent_card_fields(dict(row, running=False), {"pane_pid": 10}, {}, None, (), process_tree=tree)
    assert stopped["computer_use"] is stopped["browser_use"] is None
    assert "permission_source" not in stopped
    persistent = server._agent_card_fields(dict(row, lifecycle="persistent"), {"pane_pid": 10}, {}, None, (),
                                           process_tree=tree)
    assert (persistent["computer_use"], persistent["permission_resume_reason"]) == (
        "control", "Persistent agents cannot change permissions on resume")
    conversation = server._agent_card_fields(row, {"pane_pid": 10, "mail_disabled": "1"}, {}, None, (),
                                             process_tree=tree)
    assert "permission_source" not in conversation
