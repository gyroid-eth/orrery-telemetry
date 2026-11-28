#!/usr/bin/env python3
"""EXIT must reach the agent even when a shell wrapper is the pane's leader.

`#{pane_current_command}` names the foreground process-group leader. Under a
`bash -lc '… codex …'` wrapper (Linux / WSL2, no job control) that leader is
"bash" while Codex is alive, and do_exit took its zombie-shell branch: it typed
a bare `exit` into Codex, which ignored it (SandyTuring, WSL2, 2026-09-07). The
fix walks the pane's descendants; these pin both the walk and the branch.
"""
from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import shlex
import shutil
import subprocess
import sys
import time

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
SERVER = ROOT / "dashboard" / "server.py"


def _load_server():
    backup = dict(os.environ)
    os.environ.setdefault("AGENTSTACK_TERMINAL", "none")
    try:
        spec = importlib.util.spec_from_file_location(
            f"agentstack_server_exit_test_{id(SERVER)}", SERVER
        )
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        module_name = spec.name
        previous = sys.modules.get(module_name)
        sys.modules[module_name] = module
        try:
            spec.loader.exec_module(module)
            return module
        finally:
            if previous is None:
                sys.modules.pop(module_name, None)
            else:
                sys.modules[module_name] = previous
    finally:
        os.environ.clear()
        os.environ.update(backup)


# pane pid 4237 is a bash wrapper; node (codex) is its child, codex a grandchild.
_WSL_TREE = (
    "4237 913 bash\n"
    "4249 4237 node\n"
    "4263 4249 codex\n"
    "5000 1 unrelated\n"
)
_ZOMBIE_TREE = (
    "4237 913 bash\n"
    "5000 1 node\n"      # a node that is NOT under the pane must not count
)
_MAC_TREE = (
    "800 1 zsh\n"
    "801 800 /Applications/Claude.app/Contents/MacOS/claude\n"
)


def test_descendant_walk_finds_the_agent_under_a_wrapper_shell():
    server = _load_server()
    assert server._pane_agent_process("x", ps_output=_WSL_TREE) == "node"
    assert server._pane_agent_process("x", ps_output=_MAC_TREE) == "claude"
    assert server._pane_agent_process("x", ps_output=_ZOMBIE_TREE) == ""


def _run_exit(monkeypatch, server, pane_cmd, tree, category, **terminal_options):
    from _exit_terminal import ExitTerminal
    terminal = ExitTerminal(monkeypatch, server, "SandyTuring", pane_command=pane_cmd, **terminal_options)
    original = server._pane_agent_process
    monkeypatch.setattr(server, "_pane_agent_process", lambda pane: original(pane, tree))
    monkeypatch.setattr(server, "lookup_agent", lambda _: {"category": category, "attached": False})
    return server.do_exit("SandyTuring"), terminal.mutations


@pytest.mark.parametrize("legacy_metadata", [False, True])
def test_exit_sends_slash_exit_to_codex_behind_a_bash_wrapper(monkeypatch, legacy_metadata):
    server = _load_server()
    result, sent = _run_exit(monkeypatch, server, "bash", _WSL_TREE, "agent", legacy_metadata=legacy_metadata)
    assert result["ok"], result
    assert "exit-sent" in result["actions"] and "shell-exit-sent" not in result["actions"], result
    assert sent == [("paste", "/exit"), ("send-keys", "-t", "%41", "C-m")]


def test_unavailable_exit_capture_is_reported_without_sending_keys(monkeypatch, tmp_path, caplog):
    server = _load_server()
    monkeypatch.setattr(server, "RUNTIME_DIR", str(tmp_path))
    result, sent = _run_exit(monkeypatch, server, "bash", _WSL_TREE, "agent", fail_capture=True)
    assert not result["ok"] and not sent
    assert "Exit refused session=SandyTuring reason=input_not_found frame unavailable" in caplog.text
    assert not list(tmp_path.rglob("*.ansi"))


def test_exit_keeps_slash_exit_for_a_live_agent_the_classifier_called_finished(monkeypatch):
    # #19 made the "finished" category authoritative for Codex, but Claude is
    # still classified from pane_current_command + title glyph, and every
    # Claude session on macOS is `zsh > claude`. A glyph-less live Claude
    # reaches do_exit as "finished"; the descendant walk must still see the
    # agent below the shell and send /exit instead of typing `exit` into it.
    server = _load_server()
    live_mac_tree = "4237 913 zsh\n4250 4237 claude\n5000 1 unrelated\n"
    result, sent = _run_exit(monkeypatch, server, "zsh", live_mac_tree, "finished", editor="claude")
    assert result["ok"], result
    assert "wrapper-shell:zsh>claude" in result["actions"], result
    assert "exit-sent" in result["actions"] and "shell-exit-sent" not in result["actions"], result
    assert sent == [("paste", "/exit"), ("send-keys", "-t", "%41", "C-m")]


def test_exit_leaves_a_finished_shell_for_the_operator(monkeypatch):
    server = _load_server()
    result, sent = _run_exit(monkeypatch, server, "bash", _ZOMBIE_TREE, "finished")
    assert result["ok"] is False
    assert "remaining shell" in result["error"]
    assert not sent


def test_refused_exit_diagnostics_are_private_and_keep_three_frames(monkeypatch, tmp_path, caplog):
    server = _load_server()
    monkeypatch.setattr(server, "RUNTIME_DIR", str(tmp_path))
    for index in range(4):
        server._write_exit_refusal_frame("ExitProbe", "completion_unverified", {
            "pane": "%41", "width": 80, "height": 24, "cursor_x": 6, "cursor_y": 17,
            "ansi": f"\x1b[31mfixture private draft {index}\x1b[0m",
        })
    directory = tmp_path / "child-agents/ExitProbe.exit-refusals"
    files = sorted(directory.glob("*.ansi"))
    assert directory.stat().st_mode & 0o777 == 0o700
    assert len(files) == 3 and all(path.stat().st_mode & 0o777 == 0o600 for path in files)
    metadata, ansi = files[-1].read_bytes().split(b"\n", 1)
    assert json.loads(metadata) == dict(pane="%41", width=80, height=24, cursor_x=6, cursor_y=17,
                                       reason="completion_unverified", truncated=False)
    assert ansi == b"\x1b[31mfixture private draft 3\x1b[0m"
    assert f"reason=completion_unverified frame={files[-1]}" in caplog.text
    assert "fixture private draft" not in caplog.text


@pytest.mark.skipif(shutil.which("tmux") is None, reason="requires tmux")
@pytest.mark.parametrize("screen", ["model", "approval", "composer", "hermes", "hermes-wrong", "hermes-edited"])
def test_exit_keys_on_a_private_tmux_server(monkeypatch, tmp_path, screen):
    # Keep the Unix socket path below the platform limit even under xdist.
    import tempfile

    server = _load_server()
    monkeypatch.setattr(server, "RUNTIME_DIR", str(tmp_path))
    received = tmp_path / "input"
    tui = r"""
import os, pathlib, sys, tty
capture, screen = pathlib.Path(sys.argv[1]), sys.argv[2]
tty.setraw(0)
options = {
    "model": "Select a model\r\n> 1. Gemini 4 Neon\r\n  2. Gemini 4 Flash\r\nEnter to select · Esc to cancel",
    "approval": "Do you want to make this edit to demo.txt?\r\n❯ 1. Yes\r\n  2. No\r\nEsc to cancel",
    "composer": "› ",
}
draft = ""
def show_hermes():
    menu = ""
    if draft.startswith("/"):
        candidate = "/exit-other" if screen == "hermes-wrong" else "/exit"
        menu = f"\r\n\x1b[48;5;234m {candidate} description...\x1b[0m"
    text = " ☤ fixture-model · 42s\r\n" + "─" * 46 + "\r\n❯ " + draft + menu
    os.write(1, (f"\x1b[2J\x1b[H{text}\x1b[3;{3 + len(draft)}H").encode())

os.write(1, b"\x1b[?2004h")
if screen.startswith("hermes"):
    show_hermes()
else:
    os.write(1, options[screen].encode())
if screen == "composer":
    os.write(1, b"\r\n? for shortcuts\x1b[1;3H")
capture.touch()
while True:
    data = os.read(0, 4096)
    if not data:
        break
    with capture.open("ab") as output:
        output.write(data)
    if screen == "composer":
        if b"\r" in data:
            os.write(1, b"\x1b[1;3H\x1b[K")
        else:
            os.write(1, data.replace(b"\x1b[200~", b"").replace(b"\x1b[201~", b""))
    elif screen.startswith("hermes"):
        if b"\r" in data:
            draft = ""
        elif b"\x7f" in data:
            draft = draft[:-data.count(b"\x7f")]
        else:
            draft += data.replace(b"\x1b[200~", b"").replace(b"\x1b[201~", b"").decode()
            if screen == "hermes-edited" and draft == "/exit":
                draft += " user draft"
        show_hermes()
"""
    with tempfile.TemporaryDirectory(prefix="exit-test-", dir=pathlib.Path("/tmp").resolve()) as directory:
        tmux = ["tmux", "-S", str(pathlib.Path(directory) / "s"), "-f", "/dev/null"]
        env = {k: v for k, v in os.environ.items() if k not in {"TMUX", "TMUX_PANE", "LC_ALL"}}
        env["LC_CTYPE"] = "C.UTF-8"
        subprocess.run([*tmux, "new-session", "-d", "-s", "ExitProbe", "-x", "46" if screen.startswith("hermes") else "80", "-y", "27",
                        shlex.join([sys.executable, "-I", "-c", tui, str(received), screen])],
                       env=env, check=True, capture_output=True, text=True, timeout=5)
        try:
            api = server._MODE_API["runtime_tmux_api"]()
            transport = api["Transport"](pathlib.Path(directory) / "s")
            consumer = api["LegacyConsumer"](pathlib.Path(directory), transport=transport,
                                              proof_reader=lambda *a, **kw: [])
            monkeypatch.setattr(server, "_legacy_tmux_consumer", lambda: consumer)
            monkeypatch.setattr(server, "lookup_agent", lambda _: {"category": "agent", "attached": False})
            monkeypatch.setattr(server, "_agent_program", lambda _: "hermes" if screen.startswith("hermes") else "codex")
            end = time.monotonic() + 5
            while not received.exists() and time.monotonic() < end:
                time.sleep(.02)
            assert received.exists()
            result = server.do_exit("ExitProbe")
            evidence = list((tmp_path / "child-agents/ExitProbe.exit-refusals").glob("*.ansi"))
            assert len(evidence) == (0 if result["ok"] else 1)
            if evidence:
                metadata, ansi = evidence[0].read_bytes().split(b"\n", 1)
                observed = json.loads(metadata)
                assert observed["height"] == 27 and observed["width"] == (46 if screen.startswith("hermes") else 80)
                if screen == "hermes-wrong":
                    assert b"/exit-other" in ansi and observed["reason"] == "completion_mismatch"
            if screen in ("composer", "hermes"):
                assert result["ok"], result
                assert received.read_bytes() == b"\x1b[200~/exit\x1b[201~\r"
            elif screen in ("hermes-wrong", "hermes-edited"):
                assert not result["ok"] and "Cannot confirm /exit" in result["error"], result
                keys = b"\x7f" * 5 if screen == "hermes-wrong" else b""
                assert received.read_bytes() == b"\x1b[200~/exit\x1b[201~" + keys
                capture = subprocess.run([*tmux, "capture-pane", "-p", "-t", "ExitProbe"],
                                         env=env, check=True, capture_output=True, text=True, timeout=5)
                assert ("❯ /exit user draft" in capture.stdout) is (screen == "hermes-edited")
                if screen == "hermes-wrong":
                    assert "/exit" not in capture.stdout
            else:
                assert "Close any selection or approval" in result["error"], result
                assert received.read_bytes() == b""
        finally:
            subprocess.run([*tmux, "kill-server"], env=env, capture_output=True, timeout=5)
