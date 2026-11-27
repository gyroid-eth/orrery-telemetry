import json
import os
from pathlib import Path
import runpy
import shlex
import shutil
import subprocess
import sys
import uuid

import pytest

from dashboard import server


LIB = Path(__file__).resolve().parents[1] / "bin" / "lib"
CLAUDE = runpy.run_path(str(LIB / "claude_context.py"))
CODEX = runpy.run_path(str(LIB / "codex_context.py"))


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    workdir = tmp_path / "work folder"
    workdir.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.chdir(workdir)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home / ".claude"))
    monkeypatch.setenv("CODEX_HOME", str(home / ".codex"))
    return workdir, home


def test_claude_unconfigured_settings_adds_quiet_observer(workspace):
    command = CLAUDE["launch_args"](["claude", "--model", "sonnet", "prompt"], "%2", 75)
    assert command[3:] == ["--model", "sonnet", "prompt"]
    status = json.loads(command[2])["statusLine"]
    assert status["type"] == "command"
    assert shlex.split(status["command"])[2:] == ["observe", "%2", "75"]


def test_claude_wraps_local_status_without_putting_command_in_argv(workspace):
    workdir, home = workspace
    user_config = home / ".claude/settings.json"
    user_config.parent.mkdir()
    user_config.write_text(json.dumps({"statusLine": {"type": "command", "command": "user"}}))
    local_config = workdir / ".claude/settings.local.json"
    local_config.parent.mkdir()
    local_config.write_text(json.dumps({
        "statusLine": {"type": "command", "command": "local", "padding": 2},
    }))
    inline = '{"permissions":{"deny":["mcp__computer"]}}'
    command = CLAUDE["launch_args"](["claude", "--settings", inline, "prompt"], "%3", 85)
    merged = json.loads(command[2])
    assert merged["permissions"] == json.loads(inline)["permissions"]
    status = merged["statusLine"]
    assert status["padding"] == 2
    assert shlex.split(status["command"])[2:] == ["observe", "%3", "85", "--file", str(local_config)]
    assert command[-1] == "prompt"


def test_explicit_safe_mode_and_file_override_are_preserved(workspace):
    for command in (
        ["claude", "--safe-mode"],
        ["claude", "--settings", "custom.json"],
        ["claude", "--settings", '{"disableAllHooks":true}'],
    ):
        assert CLAUDE["launch_args"](command, "%1", 75) == command


@pytest.mark.skipif(shutil.which("tmux") is None, reason="requires isolated tmux")
def test_statusline_forwards_bytes_and_records_current_context(workspace, monkeypatch):
    cwd, _ = workspace
    socket = ["tmux", "-L", f"orrery-ctx-{uuid.uuid4().hex[:10]}", "-f", "/dev/null"]
    tmux_env = {k: v for k, v in os.environ.items() if k not in ("TMUX", "TMUX_PANE")}
    def run(args):
        return subprocess.run([*socket, *args], env=tmux_env, check=True, capture_output=True)

    try:
        isolated = run(["new-session", "-d", "-P", "-F", "#{socket_path} #{pane_id}", "sleep 30"])
        socket_path, pane = isolated.stdout.decode().strip().split()
        status = cwd / "status line.json"
        status.write_text(json.dumps({"statusLine": {"command": "cat"}}))
        helper = [sys.executable, str(LIB / "claude_context.py"), "observe", pane, "75",
                  "--file", str(status)]
        env = {**tmux_env, "TMUX": f"{socket_path},0,0"}
        payload = b'{"context_window":{"used_percentage":26.4,"context_window_size":262144}}\n'
        check = subprocess.run(helper, env=env, input=payload, capture_output=True, check=True)
        assert check.stdout == payload
        option = ["show-options", "-p", "-v", "-t", pane, "@orrery-claude-context"]
        assert run(option).stdout == b"75:26:262144\n"
        monkeypatch.setattr(server, "_tmux", lambda args: run(args).stdout.decode())
        row, = server.tmux_state().values()
        assert row["claude_context"] == "75:26:262144"
        payload = b'{"context_window":{"used_percentage":null,"context_window_size":262144}}\n'
        subprocess.run(helper, env=env, input=payload, capture_output=True, check=True)
        assert run(option).stdout == b"75::262144\n"
    finally:
        subprocess.run([*socket, "kill-server"], env=tmux_env, capture_output=True)


def test_dashboard_requires_status_sample_from_current_claude_process():
    session = {"pane_pid": 100, "claude_context": "75:0:262144"}
    snapshot = ({100: "bash", 75: "claude"}, {100: [75]})
    read = server._claude_context_runtime
    assert read(session, "claude-code", snapshot) == {"ctx_used": 0, "ctx_window": "262144"}
    assert read(session, "codex-cli", snapshot) == {}
    assert read({**session, "claude_context": "76:92:262144"}, "claude-code", snapshot) == {}
    assert read({**session, "claude_context": "75::262144"}, "claude-code", snapshot) == {
        "ctx_used": None, "ctx_window": "262144",
    }


def test_codex_default_used_only_without_explicit_footer(workspace):
    workdir, home = workspace
    default = CODEX["default_flag"]
    assert default(workdir) == 'tui.status_line=["model-with-reasoning","context-remaining","current-dir"]'
    config = home / ".codex/config.toml"
    config.parent.mkdir()
    config.write_text("[tui]\nstatus_line = []\n")
    assert default(workdir) == ""
    config.write_text("[tui]\ntheme = 'default'\n")
    assert default(workdir)
    project = workdir / ".codex/config.toml"
    project.parent.mkdir()
    project.write_text("[tui]\nstatus_line = ['current-dir']\n")
    assert default(workdir) == ""
