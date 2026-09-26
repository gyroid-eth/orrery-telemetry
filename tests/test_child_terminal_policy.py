"""Automatic child observation must not disable tmux or manual Deck opening."""
from __future__ import annotations

import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import time

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_spawn_child_embed_task import _codex_handoff, _fake_launch_env
from test_codex_resume_flags import policy_env, _seed_child_identity, _invoke_resume_entry

ROOT = Path(__file__).resolve().parent.parent
SPAWN = ROOT / "hooks/spawn_child.sh"
INSTALL = ROOT / "scripts/install.sh"


def _function(path: Path, name: str) -> str:
    text = path.read_text()
    start = text.index(f"\n{name}() {{") + 1
    end = text.index("\n}\n", start) + 3
    return text[start:end]


def _assignment(path: Path, name: str) -> str:
    return next(line for line in path.read_text().splitlines()
                if line.startswith(f"{name}="))


def _shell(script: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["/bin/bash", "-euc", script], cwd=ROOT, env=env,
        text=True, capture_output=True, timeout=20,
    )


def _fake_terminals(bindir: Path, env: dict[str, str], log: Path) -> None:
    bindir.mkdir(exist_ok=True)
    for name in ("open", "osascript"):
        command = bindir / name
        command.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$OPEN_LOG"\n')
        command.chmod(0o755)
    env.update(PATH=f"{bindir}:{env['PATH']}", OPEN_LOG=str(log))


@pytest.mark.parametrize("adapter", ["auto", "ghostty", "iterm", "terminal", "none"])
@pytest.mark.parametrize("setting,focus,opens", [
    (None, "", True), ("", "", True), ("0", "1", False), ("0", "", False),
    (None, "1", True), ("1", "", True), ("1", "1", True),
    ("yes", "", False),
])
def test_automatic_open_is_default_on_and_focus_is_independent(
    tmp_path, adapter, setting, focus, opens,
):
    env = {**os.environ, "HOME": str(tmp_path), "AGENTSTACK_TERMINAL": adapter,
           "AGENTSTACK_FOCUS_CHILD": focus}
    env.pop("AGENTSTACK_AUTO_OPEN_CHILD", None)
    if setting is not None:
        env["AGENTSTACK_AUTO_OPEN_CHILD"] = setting
    log = tmp_path / "open.log"
    _fake_terminals(tmp_path / "bin", env, log)
    script = "\n".join([
        _assignment(SPAWN, "TERMINAL_SETTING"),
        _assignment(SPAWN, "AUTO_OPEN_CHILD"),
        "uname() { printf 'Darwin\\n'; }", "mac_app_exists() { return 0; }",
        _function(SPAWN, "terminal_adapter"),
        _function(SPAWN, "_open_child_terminal"),
        _function(SPAWN, "open_child_terminal"),
        "open_child_terminal QuietCurie", "wait",
    ])
    result = _shell(script, env)
    assert result.returncode == 0, result.stderr
    assert log.exists() == (opens and adapter != "none")
    if log.exists():
        command = log.read_text()
        assert "QuietCurie" in command
        if adapter in {"auto", "ghostty"}:
            assert ("-g " in command) == (focus != "1")
        else:
            assert ("activate" in command) == (focus == "1")


@pytest.mark.parametrize("codex", [False, True], ids=["claude", "codex"])
@pytest.mark.parametrize("setting", [None, "0", "1"])
def test_spawn_keeps_detached_session_and_propagates_observer_policy(
    tmp_path, codex, setting,
):
    env, workdir = _fake_launch_env(tmp_path, codex=codex)
    env.update(AGENTSTACK_TERMINAL="ghostty", AGENTSTACK_PYTHON=sys.executable)
    env.pop("AGENTSTACK_AUTO_OPEN_CHILD", None)
    if setting is not None:
        env["AGENTSTACK_AUTO_OPEN_CHILD"] = setting
    env["PATH"] = f"{Path(sys.executable).parent}:{env['PATH']}"
    log = tmp_path / "open.log"
    _fake_terminals(tmp_path / "terminal-bin", env, log)
    child = "QuietCurie"
    handoff = _codex_handoff(tmp_path, child)
    task = tmp_path / "task.md"
    task.write_text("Report completion without changing files.")
    args = ["/bin/bash", str(SPAWN), "--pre-registered", child,
            "--child-token-file", str(handoff), "--embed-task", "--task-file", str(task)]
    if codex:
        args.append("--codex")
    result = subprocess.run(args + ["ignored", str(workdir)], cwd=ROOT, env=env,
                            text=True, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert child in result.stdout
    assert Path(env["FAKE_TMUX_ALIVE"]).exists()
    calls = Path(env["FAKE_TMUX_LOG"]).read_text().replace("\x1c", " ")
    assert f"new-session -d -s {child}" in calls
    assert f"AGENTSTACK_AUTO_OPEN_CHILD={setting or '1'}" in calls
    if setting != "0":
        deadline = time.monotonic() + 3
        while not log.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
    assert log.exists() == (setting != "0")


@pytest.mark.parametrize("setting", ["0", "1"])
def test_manual_deck_terminal_open_ignores_automatic_policy(monkeypatch, setting):
    from dashboard import server

    monkeypatch.setenv("AGENTSTACK_AUTO_OPEN_CHILD", setting)
    monkeypatch.setattr(server, "TERMINAL_SETTING", "ghostty")
    commands = []

    def run(argv, **kwargs):
        commands.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(server.subprocess, "run", run)
    result = server._open_terminal_tmux(["tmux", "attach", "-t", "=QuietCurie"], "QuietCurie")
    assert result == {"ok": True, "adapter": "ghostty"}
    assert commands[0][-4:] == ["tmux", "attach", "-t", "=QuietCurie"]


@pytest.mark.parametrize("explicit,saved,expected", [
    (None, None, "1"), (None, "1", "1"), (None, "0", "0"),
    ("0", "1", "0"), ("1", "0", "1"), ("bad", None, None),
    (None, "bad", None),
])
def test_installer_preserves_valid_saved_policy_and_explicit_override(
    tmp_path, explicit, saved, expected,
):
    text = INSTALL.read_text()
    start = text.index('if [[ -z "$AUTO_OPEN_CHILD_SETTING" ]]; then')
    saved_resolution = text[start:text.index("\nfi", start) + 3]
    start = text.index('case "$AUTO_OPEN_CHILD_SETTING" in')
    validation = text[start:text.index("\nesac", start) + 5]
    env_file = tmp_path / "env.sh"
    if saved is not None:
        env_file.write_text(f"export AGENTSTACK_AUTO_OPEN_CHILD={shlex.quote(saved)}\n")
    env = {**os.environ, "HOME": str(tmp_path), "INSTALL_DIR": str(tmp_path),
           "AGENTSTACK_PYTHON": sys.executable}
    env.pop("AGENTSTACK_AUTO_OPEN_CHILD", None)
    if explicit is not None:
        env["AGENTSTACK_AUTO_OPEN_CHILD"] = explicit
    default = 'AUTO_OPEN_CHILD_SETTING="${AUTO_OPEN_CHILD_SETTING:-1}"'
    assert default in text
    result = _shell("\n".join([
        f". {shlex.quote(str(ROOT / 'hooks/project-context.sh'))}",
        _assignment(INSTALL, "AUTO_OPEN_CHILD_SETTING"), saved_resolution,
        default, validation, 'printf "%s" "$AUTO_OPEN_CHILD_SETTING"',
    ]), env)
    if expected is None:
        assert result.returncode == 2
        assert "must be 0 or 1" in result.stderr
    else:
        assert result.returncode == 0, result.stderr
        assert result.stdout == expected


@pytest.mark.parametrize("setting", ["0", "1"])
def test_agentctl_renders_and_exports_observer_policy(tmp_path, setting):
    import plistlib

    path = ROOT / "dashboard/agentctl.sh"
    render = _function(path, "render_plist")
    background = _function(path, "export_background_env")
    variables = set(re.findall(r'\$([A-Z][A-Z_0-9]*)', render + background))
    env = {**os.environ, **{name: "" for name in variables}}
    env.update(HOME=str(tmp_path), AUTO_OPEN_CHILD_SETTING=setting, TERMINAL="ghostty",
               PLIST_TEMPLATE=str(ROOT / "dashboard/agentdashboard.plist.template"),
               PLIST_DST=str(tmp_path / "dashboard.plist"))
    result = _shell("\n".join([
        _function(path, "sed_escape"), render, background,
        'render_plist', 'export_background_env',
        'printf "%s:%s" "$AGENTSTACK_AUTO_OPEN_CHILD" "$AGENTSTACK_TERMINAL"',
    ]), env)
    assert result.returncode == 0, result.stderr
    assert result.stdout == f"{setting}:ghostty"
    plist = plistlib.loads((tmp_path / "dashboard.plist").read_bytes())
    assert plist["EnvironmentVariables"]["AGENTSTACK_AUTO_OPEN_CHILD"] == setting
    assert plist["EnvironmentVariables"]["AGENTSTACK_TERMINAL"] == "ghostty"


@pytest.mark.parametrize("setting", ["0", "1"])
def test_installer_writes_same_policy_for_shell_launchd_and_systemd(tmp_path, setting):
    import plistlib

    source = INSTALL.read_text()
    functions = []
    for name in ("write_env_file", "render_launchd_plist", "render_systemd_unit"):
        start = source.index(f"\n{name}() {{") + 1
        heredoc_end = source.index("\nPY\n", start)
        functions.append(source[start:source.index("\n}\n", heredoc_end) + 3])
    script = "\n".join(functions)
    variables = set(re.findall(r'\$([A-Z][A-Z_0-9]*)', script))
    env = {**os.environ, **{name: "" for name in variables}}
    env.update(HOME=str(tmp_path), AUTO_OPEN_CHILD_SETTING=setting, TERMINAL="auto",
               PYTHON_BIN=sys.executable, REPO_ROOT=str(ROOT), DRY_RUN="false",
               ENV_FILE=str(tmp_path / "env.sh"), LABEL="org.agentstack.test.observer")
    result = _shell(script + "\nplan() { :; }\nwrite_env_file\n"
                    "render_launchd_plist\nrender_systemd_unit\n", env)
    assert result.returncode == 0, result.stderr
    shell_env = (tmp_path / "env.sh").read_text()
    assert f"export AGENTSTACK_AUTO_OPEN_CHILD={setting}\n" in shell_env
    assert "export AGENTSTACK_TERMINAL=auto\n" in shell_env
    plist_path = tmp_path / "Library/LaunchAgents/org.agentstack.test.observer.plist"
    plist = plistlib.loads(plist_path.read_bytes())
    assert plist["EnvironmentVariables"]["AGENTSTACK_AUTO_OPEN_CHILD"] == setting
    assert plist["EnvironmentVariables"]["AGENTSTACK_TERMINAL"] == "auto"
    unit = (tmp_path / ".config/systemd/user/org.agentstack.test.observer.service").read_text()
    assert f'Environment="AGENTSTACK_AUTO_OPEN_CHILD={setting}"' in unit
    assert 'Environment="AGENTSTACK_TERMINAL=auto"' in unit


@pytest.mark.parametrize("setting", [None, "0", "1"])
def test_resumed_agent_keeps_observer_policy_for_its_children(
    policy_env, monkeypatch, setting,
):
    from dashboard import server

    tmp_path, project = policy_env
    runtime = tmp_path / "runtime"
    _seed_child_identity(runtime, project)
    monkeypatch.setattr(server, "SESSION_INDEX_DIR", str(runtime / "session_index"))
    monkeypatch.delenv("AGENTSTACK_AUTO_OPEN_CHILD", raising=False)
    if setting is not None:
        monkeypatch.setenv("AGENTSTACK_AUTO_OPEN_CHILD", setting)
    result, launched = _invoke_resume_entry(monkeypatch, tmp_path, project, runtime)
    assert result["ok"] is True, result
    assert f"AGENTSTACK_AUTO_OPEN_CHILD={setting or '1'}" in launched[0]
