#!/usr/bin/env python3
"""Regression tests for fail-closed name availability and tmux name collisions.

Covers the 2026-07-24 tester report:
  - defect A, section 4.4: whois errors were read as "name is free"
  - defect B, section 5.4: a same-named tmux session was killed as "stale"

Runnable two ways (no third-party dependency required):
    python3 tests/test_name_availability.py
    pytest tests/test_name_availability.py
"""
from __future__ import annotations

import os
import pathlib
import stat
import subprocess
import sys
import tempfile

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_LIB = _ROOT / "bin" / "lib" / "agentstack-register.sh"

# Real responses measured against mcp-agent-mail (stock whois reports a missing
# agent through the error channel, so the error text is what disambiguates).
_FOUND = (
    '{"jsonrpc":"2.0","id":"1","result":{"content":[{"type":"text",'
    '"text":"{\\"id\\":323,\\"name\\":\\"Live-Bohr\\",\\"program\\":\\"claude-code\\"}"}]}}'
)
_NOT_FOUND = (
    '{"jsonrpc":"2.0","id":"1","result":{"content":[{"type":"text",'
    "\"text\":\"Error calling tool 'whois': Agent 'Free-Bohr' not found in project '/p'.\"}],"
    '"isError":true}}'
)
_AUTH_WALL = (
    '{"jsonrpc":"2.0","id":"1","result":{"content":[{"type":"text",'
    "\"text\":\"Error calling tool 'whois': whois requires registration_token for agent "
    "'Live-Bohr', unless this MCP session has already authenticated as that agent.\"}],"
    '"isError":true}}'
)
_SERVER_ERROR = (
    '{"jsonrpc":"2.0","id":"1","result":{"content":[{"type":"text",'
    "\"text\":\"Error calling tool 'whois': internal server error\"}],\"isError\":true}}"
)


def _run_bash(script: str, env: dict[str, str] | None = None,
              check: bool = True) -> subprocess.CompletedProcess[str]:
    run_env = os.environ.copy()
    if env:
        run_env.update(env)
    return subprocess.run(
        ["bash", "-c", script],
        cwd=_ROOT,
        env=run_env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=check,
    )


# The stubbed response travels through the environment: embedding a JSON blob
# containing single quotes into a bash -c string is a quoting minefield.
_STUB = 'ags_mcp_call() { printf \'%s\' "$STUB_RESPONSE"; }; '


def _status(response: str) -> str:
    """Classify one stubbed whois response via the shipped helper."""
    script = (
        f'source "{_LIB}" >/dev/null 2>&1; '
        + _STUB +
        'ags_agent_name_status "/p" "Some-Bohr"'
    )
    return _run_bash(script, {"STUB_RESPONSE": response}).stdout.strip()


def test_status_classifies_every_whois_outcome():
    assert _status(_FOUND) == "occupied"
    assert _status(_NOT_FOUND) == "available"
    # The token-strict server refusing an unauthenticated read PROVES the agent
    # exists. This is the case that used to read as "free".
    assert _status(_AUTH_WALL) == "occupied"
    # Anything we cannot classify is unknown, never available.
    assert _status(_SERVER_ERROR) == "unknown"
    assert _status("") == "unknown"


def test_picker_refuses_to_hand_out_an_unverifiable_name():
    """Repeated 'unknown' must abort instead of claiming a possibly-live name."""
    script = (
        f'source "{_LIB}" >/dev/null 2>&1; '
        + _STUB +
        'ags_pick_available_agent_name "/p" "" && echo UNEXPECTED_SUCCESS'
    )
    result = _run_bash(
        script,
        {"STUB_RESPONSE": _SERVER_ERROR, "AGENTSTACK_NAME_UNKNOWN_LIMIT": "3"},
        check=False,
    )
    assert result.returncode != 0, result.stdout
    assert "UNEXPECTED_SUCCESS" not in result.stdout
    assert "refusing to pick a name" in result.stderr

    # A preferred name that cannot be verified is likewise not claimed.
    preferred = (
        f'source "{_LIB}" >/dev/null 2>&1; '
        + _STUB +
        'ags_pick_available_agent_name "/p" "" "Live-Bohr" && echo UNEXPECTED_SUCCESS'
    )
    result = _run_bash(preferred, {"STUB_RESPONSE": _SERVER_ERROR}, check=False)
    assert result.returncode != 0
    assert "Live-Bohr" not in result.stdout
    assert "refusing to claim it" in result.stderr


def test_picker_still_returns_a_name_when_the_server_answers():
    """Fail-closed must not brick the normal path: 'not found' means free."""
    script = (
        f'source "{_LIB}" >/dev/null 2>&1; '
        + _STUB +
        'ags_pick_available_agent_name "/p" ""'
    )
    name = _run_bash(script, {"STUB_RESPONSE": _NOT_FOUND}).stdout.strip()
    assert name and "-" in name, name


def _tmux_stub(tmpdir: pathlib.Path, *, collision: bool) -> pathlib.Path:
    """A fake tmux that logs calls and fails rename-session on collision."""
    log = tmpdir / "tmux.log"
    stub = tmpdir / "tmux"
    stub.write_text(
        "#!/bin/bash\n"
        f'echo "$@" >> "{log}"\n'
        'case "$1" in\n'
        '  display-message) echo "pending-4242" ;;\n'
        f'  rename-session) exit {1 if collision else 0} ;;\n'
        f'  has-session) exit {0 if collision else 1} ;;\n'
        "  *) exit 0 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    return log


def test_tmux_name_collision_never_kills_the_existing_session():
    hook = _ROOT / "hooks" / "set-ghostty-title.sh"
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = pathlib.Path(tmp)
        log = _tmux_stub(tmpdir, collision=True)
        runtime = tmpdir / "runtime"
        result = _run_bash(
            f'"{hook}" Live-Bohr',
            {
                "PATH": f"{tmpdir}:{os.environ['PATH']}",
                "TMUX": "/tmp/fake-tmux,1,0",
                "TMUX_PANE": "%9",
                "AGENTSTACK_RUNTIME_DIR": str(runtime),
                "AGENTSTACK_MANAGED_AGENTS_FILE": str(tmpdir / "managed.txt"),
                "AGENTSTACK_TERMINAL": "none",
            },
            check=False,
        )
        calls = log.read_text(encoding="utf-8") if log.exists() else ""

        # The whole point: the live session keeping the name survives.
        assert "kill-session" not in calls, calls
        # And the collision is not reported as a successful identity claim.
        assert result.returncode != 0, result.stdout
        assert "already exists" in result.stderr
        # Identity metadata must not be published for an unfinalized name.
        assert not (runtime / "agent_name_%9".replace("%", "_")).exists()


def test_successful_rename_still_records_identity():
    hook = _ROOT / "hooks" / "set-ghostty-title.sh"
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = pathlib.Path(tmp)
        log = _tmux_stub(tmpdir, collision=False)
        runtime = tmpdir / "runtime"
        result = _run_bash(
            f'"{hook}" Fresh-Bohr',
            {
                "PATH": f"{tmpdir}:{os.environ['PATH']}",
                "TMUX": "/tmp/fake-tmux,1,0",
                "TMUX_PANE": "%9",
                "AGENTSTACK_RUNTIME_DIR": str(runtime),
                "AGENTSTACK_MANAGED_AGENTS_FILE": str(tmpdir / "managed.txt"),
                "AGENTSTACK_TERMINAL": "none",
            },
            check=False,
        )
        assert result.returncode == 0, result.stderr
        assert "rename-session Fresh-Bohr" in log.read_text(encoding="utf-8")
        assert (runtime / "agent_name__9").read_text(encoding="utf-8") == "Fresh-Bohr"


def _main() -> int:
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as exc:
                failures += 1
                print(f"FAIL {name}: {exc}")
    print("\n" + ("ALL PASSED" if not failures else f"{failures} FAILED"))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(_main())
