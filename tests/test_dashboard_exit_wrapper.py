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
import os
import pathlib
import sys

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


def _run_exit(server, pane_cmd: str, tree: str, category: str):
    sent: list[list[str]] = []

    class _Done:
        def __init__(self, stdout="", returncode=0):
            self.stdout, self.stderr, self.returncode = stdout, "", returncode

    def fake_run(argv, **_):
        if argv[:2] == ["tmux", "display-message"] and "#{pane_current_command}" in argv:
            return _Done(pane_cmd + "\n")
        if argv[:2] == ["tmux", "display-message"] and "#{pane_pid}" in argv:
            return _Done("4237\n")
        if argv[:1] == ["ps"]:
            return _Done(tree)
        if argv[:2] == ["tmux", "send-keys"]:
            sent.append(argv)
            return _Done()
        raise AssertionError(f"unexpected command {argv}")

    originals = (server.subprocess.run, server.build_agents, server._has_session, server.time.sleep)
    try:
        server.subprocess.run = fake_run
        server.build_agents = lambda history_days=None: [{"name": "SandyTuring", "category": category, "attached": False}]
        server._has_session = lambda _s: True
        server.time.sleep = lambda _s: None
        result = server.do_exit("SandyTuring")
    finally:
        server.subprocess.run, server.build_agents, server._has_session, server.time.sleep = originals
    return result, sent


def test_exit_sends_slash_exit_to_codex_behind_a_bash_wrapper():
    server = _load_server()
    result, sent = _run_exit(server, "bash", _WSL_TREE, "agent")
    assert result["ok"], result
    assert "exit-sent" in result["actions"] and "shell-exit-sent" not in result["actions"], result
    assert any("/exit" in argv for argv in sent), sent
    assert not any(argv[-2:] == ["exit", "Enter"] for argv in sent), sent


def test_exit_keeps_slash_exit_for_a_live_agent_the_classifier_called_finished():
    # #19 made the "finished" category authoritative for Codex, but Claude is
    # still classified from pane_current_command + title glyph, and every
    # Claude session on macOS is `zsh > claude`. A glyph-less live Claude
    # reaches do_exit as "finished"; the descendant walk must still see the
    # agent below the shell and send /exit instead of typing `exit` into it.
    server = _load_server()
    live_mac_tree = "4237 913 zsh\n4250 4237 claude\n5000 1 unrelated\n"
    result, sent = _run_exit(server, "zsh", live_mac_tree, "finished")
    assert result["ok"], result
    assert "wrapper-shell:zsh>claude" in result["actions"], result
    assert "exit-sent" in result["actions"] and "shell-exit-sent" not in result["actions"], result
    assert any("/exit" in argv for argv in sent), sent
    assert not any(argv[-2:] == ["exit", "Enter"] for argv in sent), sent


def test_exit_still_closes_a_real_zombie_shell():
    server = _load_server()
    result, sent = _run_exit(server, "bash", _ZOMBIE_TREE, "finished")
    assert result["ok"], result
    assert "shell-exit-sent" in result["actions"], result
    assert any(argv[-2:] == ["exit", "Enter"] for argv in sent), sent


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except Exception as error:  # noqa: BLE001
                failures += 1
                print(f"FAIL {name}: {error}")
    sys.exit(1 if failures else 0)
