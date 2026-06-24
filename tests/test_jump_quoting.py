#!/usr/bin/env python3
"""Regression tests for dashboard terminal-jump shell quoting.

Background
----------
The dashboard builds a shell command string for the iTerm/Terminal jump
adapters and hands it to the user's shell (zsh on macOS) via AppleScript
`do script`. tmux exact-match targets look like `=cx-001`. `shlex.quote`
treats a leading `=` (and `~`) as "safe" and leaves it bare, but zsh applies
EQUALS expansion to a leading `=` (`=foo` -> path of command `foo`) and tilde
expansion to a leading `~`. A bare `=cx-001` therefore failed with
`zsh: cx-001 not found` instead of attaching the session.

`_zsh_safe_quote` / `_shell_join` force-quote such tokens. These tests pin
that behaviour so a future refactor cannot silently reintroduce the bug.

Runnable two ways (no third-party dependency required):
    python3 tests/test_jump_quoting.py      # plain script, prints PASS/FAIL
    pytest tests/test_jump_quoting.py        # under pytest if available
"""
from __future__ import annotations

import importlib.util
import pathlib
import sys

_SERVER = pathlib.Path(__file__).resolve().parent.parent / "dashboard" / "server.py"


def _load_server():
    spec = importlib.util.spec_from_file_location("agentstack_server", _SERVER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


srv = _load_server()


def test_equals_target_is_quoted():
    # The tmux exact-match target must be quoted so zsh does not EQUALS-expand it.
    out = srv._shell_join(["env", "-u", "TMUX", "tmux", "attach", "-d", "-t", "=cx-001"])
    assert "'=cx-001'" in out, out          # quoted form present
    assert " =cx-001" not in out, out       # bare token must not survive


def test_tilde_target_is_quoted():
    # A leading '~' would otherwise be tilde-expanded by the shell.
    assert srv._zsh_safe_quote("~/foo") == "'~/foo'"


def test_normal_args_unchanged():
    # Ordinary args must not be needlessly quoted (keeps commands readable).
    assert srv._shell_join(["tmux", "attach", "-t", "cx-001"]) == "tmux attach -t cx-001"


def test_embedded_equals_not_quoted():
    # Only a *leading* '=' triggers EQUALS expansion; mid-token '=' is fine.
    assert srv._zsh_safe_quote("AGENT_NAME=cx-001") == "AGENT_NAME=cx-001"


def _main() -> int:
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as e:
                failures += 1
                print(f"FAIL {name}: {e}")
    print(f"\n{'ALL PASSED' if not failures else f'{failures} FAILED'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(_main())
