"""Tools of agents without saved launch settings, read from fake processes."""

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


@pytest.mark.parametrize("args, channels", [
    (["claude", "--channels", "plugin:fixture"], True),
    (["node", "/fixture/@anthropic-ai/claude-code/cli.js", "--channels=plugin:fixture"], True),
    (["claude", "--", "--channels"], False),
    (["claude", "--append-system-prompt", "mention --channels"], False),
    (["zsh", "-c", "claude --channels plugin:fixture"], False),
])
def test_claude_channels_flag(args, channels):
    assert agent_process.claude_channels(args) is channels


def test_claude_observes_the_relay_under_a_wrapper():
    tree, read = _tree({10: (1, ["claude", "--chrome"]), 11: (10, ["supervisor"]),
                        12: (11, _RELAY), 13: (10, _SHELL)})
    assert agent_process.observe(10, tree, "claude", read=read) == {
        "permission_source": "process", "computer_use": "control", "browser_use": True}


def test_unknown_flags_and_unbound_screen_relay_stay_unknown():
    tree, read = _tree({10: (1, ["claude", "--", "--chrome"]), 11: (10, _SCREEN), 12: (11, _RELAY)})
    assert agent_process.observe(10, tree, "claude", read=read) == {
        "permission_source": "process", "computer_use": None, "browser_use": None,
        "computer_use_reason": agent_process.RELAY_UNVERIFIED,
        "browser_use_reason": agent_process.BROWSER_UNVERIFIED}


def test_unreadable_grandchild_is_not_off():
    tree, read = _tree({10: (1, ["codex"]), 11: (10, ["supervisor"]), 12: (11, _RELAY)})
    def without_grandchild(pid):
        return None if pid == 12 else read(pid)

    assert agent_process.observe(10, tree, "codex", read=without_grandchild) == {
        "permission_source": "process", "computer_use": None, "browser_use": None,
        "computer_use_reason": agent_process.PROCESS_INCOMPLETE,
        "browser_use_reason": agent_process.PROCESS_INCOMPLETE}
    assert agent_process.observe(10, tree, "codex", read=read)["computer_use"] == "control"


@pytest.mark.parametrize("tree,pid", [(None, 10), (({10: "node"}, {}), 10), (({}, {}), None)])
def test_unverified_root_has_a_reason(tree, pid):
    result = agent_process.observe(pid, tree, "claude", read=lambda _pid: ["node", "unrelated.js"])
    assert result["computer_use"] is result["browser_use"] is None
    assert result["computer_use_reason"] == result["browser_use_reason"] == agent_process.PROCESS_UNVERIFIED


def test_complete_observation_and_explicit_chrome_off():
    tree, read = _tree({10: (1, ["claude", "--no-chrome"]), 11: (10, _SHELL)})
    assert agent_process.observe(10, tree, "claude", read=read) == {
        "permission_source": "process", "computer_use": "off", "browser_use": False}


def test_codex_reads_devtools_under_a_wrapper():
    tree, read = _tree({20: (1, ["codex", "resume", "sid"]), 21: (20, ["supervisor"]),
                        22: (21, _DEVTOOLS), 23: (20, _RELAY)})
    assert agent_process.observe(20, tree, "codex", read=read) == {
        "permission_source": "process", "computer_use": "control", "browser_use": True}


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


def test_card_observes_only_running_cli_without_launch_settings(monkeypatch):
    processes = {10: (1, ["node", "unrelated.js"]), 11: (10, ["claude", "--chrome"]),
                 12: (11, _RELAY)}
    tree, read = _tree(processes)
    monkeypatch.setattr(agent_process, "argv", read)
    row = {"name": "Worker", "program": "claude-code", "running": True}
    card = server._agent_card_fields(row, {"pane_pid": 10}, {}, None, (), process_tree=tree)
    assert {key: card[key] for key in (
        "computer_use", "browser_use", "permission_source", "permission_resume_supported")} == {
        "computer_use": "control", "browser_use": True, "permission_source": "process",
        "permission_resume_supported": False}
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
