"""Click-to-jump on WSL2 opens a Windows Terminal tab.

On macOS the dashboard's jump/resume actions open Ghostty / iTerm / Terminal.
On Linux they used to answer "terminal jump unsupported" (documented as the
WSL2 gap). Inside WSL2, Windows Terminal is reachable through interop as
`wt.exe`, so the dashboard now opens a new tab there that runs
`wsl.exe -d <distro> --exec … tmux attach …` back inside the distro.
"""
from __future__ import annotations

import subprocess

import dashboard.server as server


def _pretend_wsl(monkeypatch, *, wt_present: bool = True, distro: str = "Ubuntu"):
    monkeypatch.setattr(server.sys, "platform", "linux")
    monkeypatch.setattr(server, "_is_wsl", lambda: True)
    monkeypatch.setenv("WSL_DISTRO_NAME", distro)
    monkeypatch.setattr(
        server.shutil, "which",
        lambda name: "/mnt/c/Users/u/AppData/Local/Microsoft/WindowsApps/wt.exe"
        if (name == "wt.exe" and wt_present) else None,
    )
    monkeypatch.setattr(server, "TERMINAL_SETTING", "auto")


def test_auto_detects_windows_terminal_inside_wsl(monkeypatch):
    _pretend_wsl(monkeypatch)
    assert server._auto_terminal() == "wt"
    assert server._terminal_adapter() == "wt"


def test_plain_linux_and_wsl_without_wt_stay_unsupported(monkeypatch):
    _pretend_wsl(monkeypatch, wt_present=False)
    assert server._auto_terminal() == "none"
    monkeypatch.setattr(server, "_is_wsl", lambda: False)
    monkeypatch.setattr(server.shutil, "which", lambda name: "/usr/bin/wt.exe")
    assert server._auto_terminal() == "none", "wt.exe outside WSL is not a terminal we can drive"


def test_wt_setting_is_accepted_explicitly(monkeypatch):
    monkeypatch.setattr(server, "TERMINAL_SETTING", "wt")
    assert server._terminal_adapter() == "wt"


def test_jump_opens_a_tab_that_attaches_inside_the_distro(monkeypatch):
    _pretend_wsl(monkeypatch, distro="Ubuntu-24.04")
    seen: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        seen.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(server.subprocess, "run", fake_run)
    out = server._open_terminal_tmux(
        ["tmux", "attach", "-d", "-t", "=SwiftBohr"], title="SwiftBohr"
    )
    assert out == {"ok": True, "adapter": "wt"}
    assert seen == [[
        "wt.exe", "-w", "0", "new-tab", "--title", "SwiftBohr",
        "wsl.exe", "-d", "Ubuntu-24.04", "--exec",
        "env", "-u", "TMUX", "-u", "TMUX_PANE",
        "tmux", "attach", "-d", "-t", "=SwiftBohr",
    ]]


def test_wt_failure_is_reported_not_swallowed(monkeypatch):
    _pretend_wsl(monkeypatch)
    monkeypatch.setattr(
        server.subprocess, "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, "", "wt: no window"),
    )
    out = server._open_terminal_tmux(["tmux", "attach", "-t", "=X"], title="X")
    assert out["ok"] is False and "wt: no window" in out["error"]


def test_unsupported_message_names_the_wsl_option():
    assert "wt" in server._terminal_unsupported()["error"]
